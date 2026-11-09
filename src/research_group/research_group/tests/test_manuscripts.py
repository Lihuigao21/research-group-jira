"""Real manuscript fixtures, row/file isolation, history and explicit excerpts."""
from datetime import timedelta
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile
import zlib

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime
from pypdf import PdfWriter, get_configuration
from pypdf.errors import LimitReachedError
from pypdf.generic import DictionaryObject, EncodedStreamObject, NameObject
from werkzeug.test import EnvironBuilder

from research_group import manuscripts as manuscript_module

from research_group.files import _safe_storage_name, _validate_docx, read_stored_file_bytes, validate_manuscript
from research_group.manuscripts import (
    PDF_DECODED_BYTES, PDF_STREAM_BYTES, _PDFBudget, _extract_pdf_paragraphs, _parse_pdf_isolated,
    add_review, create_manuscript, create_selection, get_manuscript, get_selection,
    get_version_text, list_manuscripts, list_reviews, list_selections, list_versions, read_selection, reply_review, resolve_review,
    revoke_selection, save_ai_suggestion, submit_version,
)
from research_group.policy import STUDENT, TEACHER
from research_group.tests import test_core as core
from research_group.tests.test_core import create_members, pdf_bytes

FIXTURES = Path(__file__).parent / "fixtures"


def docx_bytes():
    return (FIXTURES / "sample.docx").read_bytes()


def mutate_docx(replace=None, omit=()):
    output = BytesIO()
    with ZipFile(BytesIO(docx_bytes())) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename not in omit:
                target.writestr(info.filename, (replace or {}).get(info.filename, source.read(info)))
        for name, content in (replace or {}).items():
            if name not in source.namelist():
                target.writestr(name, content)
    return output.getvalue()


def literal_docx_bytes(text):
    """A derivative of the licensed real OPC fixture with literal XML text.

    XML escaping belongs only to the DOCX container. Extraction, excerpts and
    persisted reviews must recover the exact unescaped UTF-8 paragraph.
    """
    document = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t xml:space="preserve">' + escape(text)
                + '</w:t></w:r></w:p><w:sectPr/></w:body></w:document>')
    return mutate_docx({"word/document.xml": document.encode("utf-8")})


def structured_docx_bytes():
    """Licensed real OPC package with legal transitional Word/VML text content.

    A VML rectangle's textbox lives in an outer paragraph's drawing run. Its
    w:txbxContent paragraphs are valid nested text, not direct p-in-p markup.
    XML order is deterministic; this fixture makes no visual-layout claim.
    """
    document = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                'xmlns:v="urn:schemas-microsoft-com:vml"><w:body><w:p>'
                '<w:r><w:t>Outer before</w:t><w:tab/><w:t>tab</w:t><w:br/><w:t>line</w:t>'
                '<w:cr/><w:t>return</w:t></w:r><w:r><w:pict>'
                '<v:rect id="TextBox1" style="width:200pt;height:60pt" stroked="f"><v:textbox>'
                '<w:txbxContent><w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/>'
                '</w:tabs></w:pPr><w:r><w:t>Box one</w:t><w:tab/><w:t>value</w:t>'
                '<w:br/><w:t>line</w:t><w:cr/><w:t>return</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>Box two</w:t></w:r></w:p></w:txbxContent>'
                '</v:textbox></v:rect></w:pict></w:r>'
                '<w:r><w:t>Outer after</w:t></w:r></w:p>'
                '<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
                '<w:r><w:t>Final</w:t></w:r></w:p><w:sectPr/></w:body></w:document>')
    return mutate_docx({"word/document.xml": document.encode("utf-8")})


def compressed_pdf_streams(lengths):
    """Real FlateDecode streams, with no text to contribute to a text limit."""
    output, writer = BytesIO(), PdfWriter()
    for length in lengths:
        page = writer.add_blank_page(width=200, height=200)
        stream = EncodedStreamObject()
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        stream._data = zlib.compress(b"%" + b"x" * (length - 2) + b"\n")
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    return output.getvalue()


def text_pdf():
    output, writer = BytesIO(), PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({
        NameObject("/F1"): writer._add_object(font)})})
    stream = EncodedStreamObject()
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    stream._data = zlib.compress(b"BT /F1 12 Tf 10 100 Td (Explicit authorized paragraph.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    return output.getvalue()


class TestManuscriptFormats(unittest.TestCase):
    def test_docx_separators_and_textbox_paragraphs_preserve_explicit_xml_order(self):
        expected = ["Outer before\ttab\nline\nreturn", "Box one\tvalue\nline\nreturn",
                    "Box two", "Outer after", "Final"]
        self.assertEqual(_validate_docx(structured_docx_bytes()), expected)

    def test_docx_explicit_whitespace_is_not_trimmed_and_textbox_active_import_stays_rejected(self):
        original = "  Leading and trailing spaces stay explicit.  "
        self.assertEqual(_validate_docx(literal_docx_bytes(original)), [original])
        with ZipFile(BytesIO(structured_docx_bytes())) as archive:
            document = archive.read("word/document.xml")
        active = document.replace(b"<w:txbxContent>", b"<w:txbxContent><w:altChunk/>")
        with self.assertRaisesRegex(ValueError, "Word active import"):
            _validate_docx(mutate_docx({"word/document.xml": active}))
        invalid_nested = document.replace(b"<w:txbxContent>", b"").replace(b"</w:txbxContent>", b"")
        with self.assertRaisesRegex(ValueError, "nested Word paragraph outside textbox"):
            _validate_docx(mutate_docx({"word/document.xml": invalid_nested}))

    def test_real_docx_hash_structure_and_text(self):
        provenance = json.loads((FIXTURES / "manuscript-provenance.json").read_text(encoding="utf-8"))
        content = docx_bytes()
        self.assertEqual(len(content), provenance["bytes"])
        self.assertEqual(hashlib.sha256(content).hexdigest(), provenance["sha256"])
        self.assertEqual(validate_manuscript("sample.docx", content), ".docx")
        self.assertTrue(_validate_docx(content))
        self.assertEqual(validate_manuscript("valid.pdf", pdf_bytes()), ".pdf")
        with self.assertRaises(frappe.ValidationError):
            validate_manuscript("slides.pptx", content)

    def test_docx_rejects_damaged_entry_graph_and_mismatched_content(self):
        with ZipFile(BytesIO(docx_bytes())) as source:
            root = source.read("_rels/.rels")
            document = source.read("word/document.xml")
        variants = (
            b"PKjunk", mutate_docx(omit=("_rels/.rels",)),
            mutate_docx(omit=("word/document.xml",)),
            mutate_docx({"_rels/.rels": root.replace(b"word/document.xml", b"word/missing.xml")}),
            mutate_docx({"word/document.xml": document.replace(b"<w:body>", b"<w:missing>")
                                                   .replace(b"</w:body>", b"</w:missing>")}),
        )
        for content in variants:
            with self.subTest(size=len(content)), self.assertRaises(frappe.ValidationError):
                validate_manuscript("broken.docx", content)
        with self.assertRaises(frappe.ValidationError):
            validate_manuscript("fake.docx", pdf_bytes())

    def test_docx_extracts_literal_tags_ampersands_and_inequalities(self):
        original = "Literal <tag> & a < b > c; x <= y && y > z."
        content = literal_docx_bytes(original)
        self.assertEqual(validate_manuscript("literal.docx", content), ".docx")
        self.assertEqual(_validate_docx(content), [original])

    def test_storage_name_preserves_ordinary_names_and_separates_literal_markup_names(self):
        for filename in ("ordinary.docx", "论文初稿.docx", "a" * 129 + ".docx"):
            self.assertEqual(_safe_storage_name(filename), filename)
        filenames = ("draft-<tag>-a<b>c&.docx", "draft-<other>-a<b>c&.docx", 'draft-"quoted".DOCX')
        stored = [_safe_storage_name(name) for name in filenames]
        self.assertEqual(len(set(stored)), len(filenames))
        for original, storage in zip(filenames, stored):
            self.assertEqual(_safe_storage_name(original), storage)
            self.assertEqual(_safe_storage_name(storage), storage)
            self.assertRegex(storage, r"^rg-file-[a-f0-9]{64}\.docx$")
            self.assertLessEqual(len(storage) + 6, 140)
        for invalid in ("bad%2fname<tag>.docx", "bad/name<tag>.docx", "bad\\name<tag>.docx"):
            with self.assertRaises(frappe.ValidationError):
                _safe_storage_name(invalid)

    def test_docx_rejects_macro_external_template_and_dtd(self):
        with ZipFile(BytesIO(docx_bytes())) as source:
            rels = source.read("word/_rels/document.xml.rels")
        variants = (
            mutate_docx({"word/vbaProject.bin": b"macro"}),
            mutate_docx({"word/_rels/document.xml.rels": rels.replace(
                b"</Relationships>", b'<Relationship Id="evil" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="https://example.invalid/template.dotm" TargetMode="External"/></Relationships>')}),
        )
        for content in variants:
            with self.assertRaises(frappe.ValidationError):
                validate_manuscript("active.docx", content)
        for encoding in ("utf-8", "utf-16", "utf-32"):
            xml = ('<?xml version="1.0" encoding="' + encoding + '"?>'
                   '<!DOCTYPE document [<!ENTITY x "payload">]><document>&x;</document>')
            with self.subTest(encoding=encoding), self.assertRaises(frappe.ValidationError):
                validate_manuscript("entity.docx", mutate_docx({"word/document.xml": xml.encode(encoding)}))

    def test_pdf_rejects_compressed_stream_before_allocating_its_declared_output(self):
        content = compressed_pdf_streams([PDF_STREAM_BYTES + 1])
        self.assertLess(len(content), 20000)
        budget = _PDFBudget()
        previous = get_configuration()
        with self.assertRaises((LimitReachedError, ValueError)):
            _extract_pdf_paragraphs(content, budget)
        self.assertEqual(budget.decoded_bytes, 0)
        self.assertIs(get_configuration(), previous)
        with self.assertRaises(ValueError):
            _parse_pdf_isolated(content)

    def test_pdf_cumulative_budget_counts_blank_streams_across_pages(self):
        length = 7 * 1024 * 1024
        content = compressed_pdf_streams([length] * 5)
        self.assertLess(len(content), 100000)
        budget = _PDFBudget()
        with self.assertRaises((LimitReachedError, ValueError)):
            _extract_pdf_paragraphs(content, budget)
        # Four legitimate pages consume 28 MiB although they contain no text;
        # the fifth stream is stopped by the remaining 4 MiB before it expands.
        self.assertEqual(budget.decoded_bytes, 4 * length)
        self.assertLessEqual(budget.decoded_bytes, PDF_DECODED_BYTES)

    def test_pdf_parser_worker_and_elapsed_budget(self):
        self.assertEqual(_parse_pdf_isolated(pdf_bytes()), ([], False))
        self.assertEqual(_parse_pdf_isolated(text_pdf()), (["Explicit authorized paragraph."], False))
        budget = _PDFBudget()
        budget.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(ValueError, "time budget"):
            _extract_pdf_paragraphs(pdf_bytes(), budget)


class TestManuscripts(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-ms-" + frappe.generate_hash(length=10)
        self.users = create_members(self.prefix)
        self.committed_fixture = False
        frappe.set_user(self.users["one"])
        self.manuscript = create_manuscript("虚构论文", self.users["teacher"])

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        if self.committed_fixture:
            self._delete_committed_fixture()
        frappe.clear_cache()
        super().tearDown()

    def _delete_committed_fixture(self):
        from research_group.ai.security import global_lock
        global_lock()
        names = frappe.get_all("RG Manuscript", filters={"author": ["in", list(self.users.values())]}, pluck="name")
        versions = frappe.get_all("RG Manuscript Version", filters={"manuscript": ["in", names]}, pluck="name")
        files = [frappe.get_doc("File", row.name) for row in frappe.get_all(
            "File", filters={"attached_to_doctype": "RG Manuscript Version", "attached_to_name": ["in", versions]},
            fields=["name"])] if versions else []
        # These immutable production records have no delete endpoint. Remove
        # only this test's known fictional IDs, retaining any deduplicated blob.
        for file in files:
            frappe.db.delete("File", {"name": file.name})
        for doctype in ("RG Manuscript Selection", "RG Manuscript Review", "RG Manuscript Version"):
            if names:
                frappe.db.delete(doctype, {"manuscript": ["in", names]})
        if names:
            frappe.db.delete("RG Manuscript", {"name": ["in", names]})
        for file in files:
            references = frappe.db.sql("SELECT name FROM `tabFile` WHERE file_url=%s OR content_hash=%s "
                                        "LIMIT 1 FOR UPDATE", (file.file_url, file.content_hash))
            if not references:
                Path(file.get_full_path()).unlink(missing_ok=True)
        core.TestResearchGroupCore._delete_fixture(self)
        frappe.db.commit()

    def version(self, content=None, filename="draft.docx", revision=0):
        frappe.set_user(self.users["one"])
        return submit_version(self.manuscript["name"], revision, filename, content or docx_bytes())

    def selection(self):
        version = self.version()
        paragraphs = get_version_text(version["name"])["paragraphs"]
        self.assertTrue(paragraphs)
        selected = paragraphs[0]["text"][:2000]
        return version, create_selection(version["name"], selected, "paragraph:0")

    def test_real_docx_textbox_and_run_separators_round_trip_through_authorized_selection(self):
        version = self.version(structured_docx_bytes(), "structured-textbox.docx")
        expected = ["Outer before\ttab\nline\nreturn", "Box one\tvalue\nline\nreturn",
                    "Box two", "Outer after", "Final"]
        extracted = get_version_text(version["name"])["paragraphs"]
        self.assertEqual([row["text"] for row in extracted], expected)
        for index in (0, 1, 3):
            original = expected[index]
            selected = create_selection(version["name"], original, "paragraph:" + str(index))
            self.assertEqual(selected["original_text"], original)
            self.assertEqual(get_selection(selected["name"])["original_text"], original)
            self.assertEqual(read_selection(selected["name"]).original_text, original)
            self.assertEqual(selected["sha256"], hashlib.sha256(original.encode("utf-8")).hexdigest())
        frappe.set_user(self.users["teacher"])
        with self.assertRaises(frappe.PermissionError):
            get_selection(selected["name"])

    def test_extractor_upgrade_blocks_legacy_selection_and_derived_history_until_explicit_reauthorization(self):
        from research_group import manuscripts

        version = self.version()
        original = get_version_text(version["name"])["paragraphs"][0]["text"][:2000].strip()
        frappe.set_user(self.users["teacher"])
        comment = add_review(version["name"], "提取器升级后仍应可读的人工意见")
        frappe.set_user(self.users["one"])
        with patch("research_group.manuscripts.EXTRACTOR_VERSION", "rg-excerpt-1"):
            legacy = create_selection(version["name"], original, "legacy-extractor")
            advice = save_ai_suggestion(legacy["name"], "旧提取器授权的私有AI建议", "旧依据", "legacy-extractor-op")
            reply = reply_review(advice["name"], "旧AI建议的私有派生回复")
            self.assertEqual(read_selection(legacy["name"]).original_text, original)
        self.assertEqual(manuscripts.EXTRACTOR_VERSION, "rg-excerpt-2")
        with self.assertRaises(frappe.PermissionError):
            read_selection(legacy["name"])
        self.assertEqual(frappe.db.get_value("RG Manuscript Selection", legacy["name"], "extractor_version"),
                         "rg-excerpt-1")
        for user in (self.users["one"], self.users["teacher"]):
            frappe.set_user(user)
            history = get_manuscript(self.manuscript["name"])
            reviews = {row["name"]: row for row in history["reviews"]}
            for name in (advice["name"], reply["name"]):
                self.assertTrue(reviews[name]["blocked"])
                self.assertFalse({"content", "original_text", "reason", "anchor"}.intersection(reviews[name]))
            self.assertEqual(reviews[comment["name"]]["content"], "提取器升级后仍应可读的人工意见")
            self.assertEqual(history["versions"][0]["name"], version["name"])
            self.assertEqual(history["versions"][0]["sha256"], hashlib.sha256(docx_bytes()).hexdigest())
            file = frappe.get_doc("File", frappe.db.get_value("RG Manuscript Version", version["name"], "material_file"))
            self.assertEqual(read_stored_file_bytes(file), docx_bytes())
        frappe.set_user(self.users["one"])
        fresh_text = get_version_text(version["name"])["paragraphs"][0]["text"][:2000].strip()
        fresh = create_selection(version["name"], fresh_text, "current-extractor-explicit-authorization")
        self.assertEqual(fresh["extractor_version"], "rg-excerpt-2")
        self.assertEqual(read_selection(fresh["name"]).original_text, fresh_text)
        self.assertNotEqual(fresh["name"], legacy["name"])

    def test_only_author_and_assigned_teacher_can_read_or_create(self):
        version = self.version()
        for key in ("two", "other_teacher", "secretary"):
            frappe.set_user(self.users[key])
            self.assertFalse(list_manuscripts()["items"])
            with self.assertRaises(frappe.PermissionError):
                get_manuscript(self.manuscript["name"])
            with self.assertRaises(frappe.PermissionError):
                get_version_text(version["name"])
        frappe.set_user(self.users["teacher"])
        self.assertEqual(len(list_manuscripts()["items"]), 1)
        self.assertEqual(get_manuscript(self.manuscript["name"])["versions"][0]["sha256"],
                         hashlib.sha256(docx_bytes()).hexdigest())
        with self.assertRaises(frappe.PermissionError):
            create_manuscript("不可替学生建稿", self.users["teacher"])

    def test_versions_preserve_bytes_idempotency_and_stale_rejection(self):
        first = self.version()
        retry = self.version()
        self.assertEqual(first["name"], retry["name"])
        with self.assertRaises(frappe.ValidationError):
            self.version(pdf_bytes(), "changed.pdf", revision=0)
        second = self.version(pdf_bytes(), "changed.pdf", revision=1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["parent_version"], first["name"])
        file_name = frappe.db.get_value("RG Manuscript Version", first["name"], "material_file")
        file = frappe.get_doc("File", file_name)
        self.assertEqual(read_stored_file_bytes(file), docx_bytes())
        row = frappe.get_doc("RG Manuscript Version", first["name"])
        row.sha256 = "0" * 64
        with self.assertRaises(frappe.PermissionError):
            row.save(ignore_permissions=True)
        file.is_private = 0
        with self.assertRaises(frappe.PermissionError):
            file.save(ignore_permissions=True)

    def test_teacher_comments_replies_new_draft_and_completion_keep_history(self):
        first = self.version()
        with self.assertRaises(frappe.PermissionError):
            add_review(first["name"], "学生冒充教师")
        frappe.set_user(self.users["teacher"])
        comment = add_review(first["name"], "请说明误差来源", "第1段")
        annotation = submit_version(self.manuscript["name"], 1, "annotated.pdf", pdf_bytes("annotation"),
                                    kind="teacher_annotation", parent_version=first["name"])
        frappe.set_user(self.users["one"])
        reply = reply_review(comment["name"], "已补充独立控制")
        self.assertEqual(reply["parent_review"], comment["name"])
        third = self.version(pdf_bytes("revision"), "revision.pdf", revision=2)
        self.assertEqual(third["parent_version"], annotation["name"])
        with self.assertRaises(frappe.PermissionError):
            resolve_review(comment["name"], "closed")
        frappe.set_user(self.users["teacher"])
        resolve_review(comment["name"], "closed")
        history = get_manuscript(self.manuscript["name"])
        self.assertEqual(len(history["versions"]), 3)
        self.assertEqual(history["reviews"][0]["content"], "请说明误差来源")
        self.assertEqual(history["reviews"][1]["content"], "已补充独立控制")
        first_page = list_versions(self.manuscript["name"], limit=2)
        self.assertTrue(first_page["has_more"])
        self.assertEqual([row["version"] for row in first_page["items"]], [3, 2])
        self.assertEqual(list_versions(self.manuscript["name"], offset=2)["items"][0]["name"], first["name"])
        self.assertEqual(list_reviews(self.manuscript["name"], offset=1, limit=1)["items"][0]["name"], reply["name"])

    def test_failed_upload_rolls_back_version_and_new_physical_blob(self):
        with self.assertRaises(frappe.ValidationError):
            self.version(b"PKfake")
        self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 0)
        with patch("research_group.manuscripts.read_stored_file_bytes", return_value=b"wrong"):
            with self.assertRaises(frappe.ValidationError):
                self.version()
        self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 0)
        self.assertEqual(frappe.db.get_value("RG Manuscript", self.manuscript["name"], "revision"), 0)

    def test_literal_filename_failures_compensate_new_blobs_before_outer_commit(self):
        from research_group.files import ResearchGroupFile
        from research_group.manuscripts import _save as original_save

        filename = "failed-<tag>-a<b>c&.docx"
        original_write = ResearchGroupFile.write_file
        original_read = read_stored_file_bytes
        for stage in ("write", "after_insert", "integrity", "manuscript_save"):
            paths, urls = [], []
            content = literal_docx_bytes("Rollback <tag> & a < b > c " + self.prefix + stage)

            def watched_write(file):
                result = original_write(file)
                paths.append(Path(file.get_full_path()))
                urls.append(file.file_url)
                if stage == "write":
                    raise frappe.ValidationError("literal filename failure after physical write")
                return result

            def after_insert(file):
                if stage == "after_insert":
                    raise frappe.ValidationError("literal filename failure after File insert")

            def read(file, max_read_bytes=None):
                if stage == "integrity":
                    return b"wrong"
                return original_read(file, max_read_bytes=max_read_bytes)

            def save(doc, new=False):
                if stage == "manuscript_save" and doc.doctype == "RG Manuscript":
                    raise frappe.ValidationError("literal filename failure before manuscript update")
                return original_save(doc, new=new)

            with self.subTest(stage=stage), patch.object(ResearchGroupFile, "write_file", watched_write), \
                    patch.object(ResearchGroupFile, "after_insert", after_insert), \
                    patch("research_group.manuscripts.read_stored_file_bytes", side_effect=read), \
                    patch("research_group.manuscripts._save", side_effect=save):
                with self.assertRaises(frappe.ValidationError):
                    submit_version(self.manuscript["name"], 0, filename, content)
            # Prove savepoint compensation itself removed bytes: an enclosing
            # caller catches the error and commits, so native rollback hooks
            # cannot be relied on to clean the failed upload afterwards.
            self.committed_fixture = True
            frappe.db.commit()
            self.assertTrue(paths, stage)
            self.assertTrue(all(not path.exists() for path in paths), paths)
            self.assertTrue(all(not any(character in path.name for character in '<>&"\'') for path in paths))
            self.assertFalse(any(frappe.db.exists("File", {"file_url": url}) for url in urls))
            self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 0)
            self.assertEqual(frappe.db.get_value("RG Manuscript", self.manuscript["name"], "revision"), 0)
            self.assertEqual(frappe.db.get_value("RG Manuscript", self.manuscript["name"], "current_version"), 0)

    def test_failed_literal_filename_deduplication_preserves_committed_shared_blob(self):
        from research_group.files import ResearchGroupFile

        content = literal_docx_bytes("Shared <tag> & a < b > c " + self.prefix)
        first = submit_version(self.manuscript["name"], 0, "shared-<tag>&.docx", content)
        version = frappe.get_doc("RG Manuscript Version", first["name"])
        file = frappe.get_doc("File", version.material_file)
        path, url = Path(file.get_full_path()), file.file_url
        self.committed_fixture = True
        frappe.db.commit()
        written_paths = []
        original_write = ResearchGroupFile.write_file

        def watched_write(new_file):
            result = original_write(new_file)
            written_paths.append(Path(new_file.get_full_path()))
            return result

        with patch.object(ResearchGroupFile, "write_file", watched_write), \
                patch.object(ResearchGroupFile, "after_insert", side_effect=frappe.ValidationError("literal dedup failure")):
            with self.assertRaises(frappe.ValidationError):
                submit_version(self.manuscript["name"], 1, "different-<tag>&.docx", content)
        frappe.db.commit()
        self.assertEqual(path.read_bytes(), content)
        self.assertTrue(all(not candidate.exists() for candidate in written_paths if candidate != path), written_paths)
        self.assertTrue(frappe.db.exists("File", file.name))
        self.assertEqual(frappe.db.count("File", {"file_url": url}), 1)
        self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 1)
        self.assertEqual(frappe.db.get_value("RG Manuscript", self.manuscript["name"], "revision"), 1)
        self.assertEqual(get_manuscript(self.manuscript["name"])["versions"][0]["original_filename"], "shared-<tag>&.docx")

    def test_native_file_lists_download_zip_and_aliases_remain_private(self):
        from frappe.core.doctype.file.utils import find_file_by_url
        from research_group.files import zip_files
        from research_group.permissions import file_has_permission
        version = self.version()
        file_name = frappe.db.get_value("RG Manuscript Version", version["name"], "material_file")
        file = frappe.get_doc("File", file_name)
        for key in ("two", "other_teacher", "secretary"):
            frappe.set_user(self.users[key])
            self.assertFalse(frappe.get_list("File", filters={"name": file_name}, pluck="name"))
            self.assertIsNone(find_file_by_url(file.file_url, file.name))
            with self.assertRaises(frappe.PermissionError):
                zip_files(json.dumps([file.name]))
        frappe.set_user(self.users["teacher"])
        self.assertTrue(file_has_permission(file))
        # Internal parent metadata has no native DocPerm, so even participants
        # use the dedicated version link rather than native File discovery.
        self.assertFalse(frappe.get_list("File", filters={"name": file_name}, pluck="name"))
        self.assertIsNotNone(find_file_by_url(file.file_url, file.name))
        with patch.dict(frappe.response, {}, clear=True):
            zip_files(json.dumps([file.name]))
            with ZipFile(BytesIO(frappe.response["filecontent"])) as archive:
                self.assertEqual(archive.read(file.name + "_" + file.file_name), docx_bytes())
        frappe.set_user(self.users["one"])
        self.assertIsNotNone(find_file_by_url(file.file_url, file.name))
        self.assertFalse(frappe.get_list("File", filters={"name": file_name}, pluck="name"))
        alias = frappe.get_doc({"doctype": "File", "file_name": "leak.docx", "file_url": file.file_url,
                                "is_private": 1})
        with self.assertRaises(frappe.PermissionError):
            alias.insert(ignore_permissions=True)

    def test_explicit_selection_owner_expiry_revoke_and_content_binding(self):
        version, selection = self.selection()
        self.assertEqual(get_selection(selection["name"])["original_text"], selection["original_text"])
        self.assertEqual(selection["version_sha256"], version["sha256"])
        with self.assertRaises(frappe.ValidationError):
            create_selection(version["name"], "任意站外的非论文段落")
        frappe.set_user(self.users["teacher"])
        with self.assertRaises(frappe.PermissionError):
            read_selection(selection["name"])
        frappe.set_user(self.users["one"])
        revoke_selection(selection["name"])
        with self.assertRaises(frappe.PermissionError):
            read_selection(selection["name"])
        another = create_selection(version["name"], selection["original_text"])
        frappe.db.set_value("RG Manuscript Selection", another["name"], "expires_at",
                            now_datetime() - timedelta(seconds=1))
        with self.assertRaises(frappe.PermissionError):
            read_selection(another["name"])

    def test_selection_management_pages_only_own_readable_sources_and_revoke_expired_saved_advice(self):
        version, active = self.selection()
        expired = create_selection(version["name"], active["original_text"], "expired source")
        advice = save_ai_suggestion(expired["name"], "本人保存的AI建议", "", "managed-expired-advice")
        reply = reply_review(advice["name"], "本人回复AI建议")
        frappe.db.set_value("RG Manuscript Selection", expired["name"], "expires_at",
                            now_datetime() - timedelta(seconds=1))
        frappe.set_user(self.users["teacher"])
        teacher_source = create_selection(version["name"], active["original_text"], "teacher-only source")
        comment = add_review(version["name"], "原教师意见保持可读")
        teacher_sources = list_selections(manuscript=self.manuscript["name"])["items"]
        self.assertEqual([row["name"] for row in teacher_sources], [teacher_source["name"]])
        with self.assertRaises(frappe.PermissionError):
            revoke_selection(expired["name"])
        frappe.set_user(self.users["one"])
        first = list_selections(limit=1)
        second = list_selections(offset=1, limit=1)
        rows = {row["name"]: row for row in first["items"] + second["items"]}
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(set(rows), {active["name"], expired["name"]})
        self.assertEqual(rows[expired["name"]]["effective_status"], "expired")
        self.assertEqual(rows[expired["name"]]["saved_suggestions"], 1)
        self.assertEqual(rows[active["name"]]["effective_status"], "active")
        self.assertEqual((first["active_count"], first["active_limit"]), (1, 20))
        self.assertTrue(all(row["private_user"] == self.users["one"]
                            and row["original_text"] == active["original_text"] for row in rows.values()))
        with self.assertRaises(frappe.PermissionError):
            get_selection(expired["name"])
        self.assertEqual(revoke_selection(expired["name"])["status"], "revoked")
        self.assertEqual(revoke_selection(expired["name"])["status"], "revoked")
        self.assertEqual([row["name"] for row in list_selections()["items"]], [active["name"]])
        history = {row["name"]: row for row in get_manuscript(self.manuscript["name"])["reviews"]}
        for name in (advice["name"], reply["name"]):
            self.assertTrue(history[name]["blocked"])
            self.assertNotIn("content", history[name])
            self.assertNotIn("original_text", history[name])
        self.assertEqual(history[comment["name"]]["content"], "原教师意见保持可读")
        self.assertEqual(get_manuscript(self.manuscript["name"])["versions"][0]["name"], version["name"])
        for key in ("two", "secretary"):
            frappe.set_user(self.users[key])
            self.assertEqual(list_selections()["items"], [])
            with self.assertRaises(frappe.PermissionError):
                list_selections(manuscript=self.manuscript["name"])

    def test_twenty_active_selection_limit_has_real_owner_recovery(self):
        version, first = self.selection()
        for _ in range(19):
            create_selection(version["name"], first["original_text"])
        self.assertEqual(list_selections()["active_count"], 20)
        with self.assertRaisesRegex(frappe.ValidationError, "本人授权选区"):
            create_selection(version["name"], first["original_text"])
        self.assertEqual(frappe.db.count("RG Manuscript Selection", {"private_user": self.users["one"]}), 20)
        revoke_selection(first["name"])
        self.assertEqual(list_selections()["active_count"], 19)
        recovered = create_selection(version["name"], first["original_text"])
        self.assertNotEqual(recovered["name"], first["name"])
        self.assertEqual(list_selections()["active_count"], 20)

    def test_review_state_history_preserves_actors_times_and_idempotent_transitions(self):
        version = self.version()
        frappe.set_user(self.users["teacher"])
        comment = add_review(version["name"], "不可复制到状态记录的私有教师正文")
        self.assertTrue(comment["status_history_complete"])
        self.assertEqual(comment["status_history"], [])
        frappe.set_user(self.users["one"])
        reply = reply_review(comment["name"], "不可复制到状态记录的学生正文")
        resolve_review(comment["name"], "addressed")
        frappe.set_user(self.users["teacher"])
        finished = resolve_review(comment["name"], "closed")
        repeated = resolve_review(comment["name"], "closed")
        self.assertEqual(finished["status_history"], repeated["status_history"])
        events = finished["status_history"]
        self.assertEqual([(row["actor"], row["before"], row["after"], row["action"]) for row in events], [
            (self.users["one"], "open", "addressed", "reply"),
            (self.users["teacher"], "addressed", "closed", "resolve")])
        self.assertLessEqual(events[0]["at"], events[1]["at"])
        for event in events:
            self.assertEqual(set(event), {"actor", "at", "before", "after", "action"})
            self.assertTrue(event["at"])
        stored = frappe.db.get_value("RG Manuscript Review", comment["name"], "status_history")
        self.assertNotIn(comment["content"], stored)
        self.assertNotIn(reply["content"], stored)
        self.assertFalse(frappe.db.exists("Version", {"ref_doctype": "RG Manuscript Review", "docname": comment["name"]}))
        self.assertEqual(list_reviews(self.manuscript["name"])["items"][0]["status_history"], events)

    def test_legacy_review_history_reports_missing_past_without_inventing_events(self):
        version = self.version()
        frappe.set_user(self.users["teacher"])
        comment = add_review(version["name"], "虚构旧意见")
        frappe.db.set_value("RG Manuscript Review", comment["name"], "status_history", None)
        legacy = get_manuscript(self.manuscript["name"])["reviews"][0]
        self.assertFalse(legacy["status_history_complete"])
        self.assertEqual(legacy["status_history"], [])
        self.assertIn("旧记录", legacy["status_history_notice"])
        frappe.set_user(self.users["one"])
        addressed = resolve_review(comment["name"], "addressed")
        self.assertFalse(addressed["status_history_complete"])
        self.assertEqual(len(addressed["status_history"]), 1)
        self.assertEqual(addressed["status_history"][0]["before"], "open")

    def test_failed_reply_status_audit_rolls_back_reply_and_state(self):
        version = self.version()
        frappe.set_user(self.users["teacher"])
        comment = add_review(version["name"], "状态保存失败控制")
        frappe.set_user(self.users["one"])
        with patch("research_group.manuscripts._change_review_status", side_effect=frappe.ValidationError("audit failure")):
            with self.assertRaises(frappe.ValidationError):
                reply_review(comment["name"], "此回复不得成为半记录")
        self.assertEqual(frappe.db.count("RG Manuscript Review", {"parent_review": comment["name"]}), 0)
        prior = frappe.get_doc("RG Manuscript Review", comment["name"])
        self.assertEqual(prior.status, "open")
        self.assertEqual(json.loads(prior.status_history)["events"], [])

    def test_ai_suggestion_confirmed_save_is_idempotent_and_retains_original(self):
        version, selection = self.selection()
        with self.assertRaises(frappe.PermissionError):
            save_ai_suggestion(selection["name"], "建议")
        suggestion = save_ai_suggestion(selection["name"], "建议保留不确定性边界", "说明依据", "fixture-op")
        again = save_ai_suggestion(selection["name"], "建议保留不确定性边界", "说明依据", "fixture-op")
        self.assertEqual(suggestion["name"], again["name"])
        self.assertEqual(suggestion["kind"], "ai_suggestion")
        self.assertEqual(suggestion["original_text"], selection["original_text"])
        self.assertEqual(suggestion["source_sha256"], selection["sha256"])
        self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 1)
        with self.assertRaises(frappe.PermissionError):
            save_ai_suggestion(selection["name"], "换内容重放同一操作", "说明依据", "fixture-op")
        frappe.db.set_value("RG Manuscript Selection", selection["name"], "expires_at",
                            now_datetime() - timedelta(seconds=1))
        with self.assertRaises(frappe.PermissionError):
            read_selection(selection["name"])
        # Selection expiry ends new model authorization, not the confirmed
        # history that both manuscript participants intentionally saved.
        frappe.set_user(self.users["teacher"])
        history = get_manuscript(self.manuscript["name"])["reviews"]
        self.assertEqual(history[0]["original_text"], selection["original_text"])
        self.assertEqual(history[0]["content"], suggestion["content"])
        frappe.set_user(self.users["one"])
        revoke_selection(selection["name"])
        for rows in (get_manuscript(self.manuscript["name"])["reviews"],
                     list_reviews(self.manuscript["name"])["items"]):
            self.assertTrue(rows[0]["blocked"])
            self.assertNotIn("original_text", rows[0])
            self.assertNotIn("content", rows[0])
        with self.assertRaises(frappe.PermissionError):
            save_ai_suggestion(selection["name"], "已撤销段落不得新存", "", "another-op")

    def test_literal_docx_selection_confirmed_ai_review_and_human_history_round_trip_exactly(self):
        from research_group.ai import api
        from research_group.ai.context import RunContext
        from research_group.ai.operations import decide, propose
        from research_group.ai.security import guard

        def exact_utf8(actual, expected):
            self.assertEqual(actual.encode("utf-8"), expected.encode("utf-8"))
            self.assertEqual(hashlib.sha256(actual.encode("utf-8")).hexdigest(),
                             hashlib.sha256(expected.encode("utf-8")).hexdigest())

        original = "Literal <tag> & a < b > c; x <= y && y > z."
        title = "虚构 <tag> & a < b > c 的论文"
        filename = "draft-<tag>-a<b>c&.docx"
        anchor = "paragraph<1> & a < b > c"
        suggestion = "AI建议 <tag> & 保留 a < b > c；x <= y && y > z。"
        reason = "理由 <comparison> & a < b > c 应逐字保存。"
        comment_text = "教师意见 <tag> & 请说明 a < b > c。"
        reply_text = "学生回复 <tag> & 已保留 x < y > z。"
        content = literal_docx_bytes(original)
        content_sha = hashlib.sha256(content).hexdigest()
        selection_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

        self.manuscript = create_manuscript(title, self.users["teacher"])
        version = submit_version(self.manuscript["name"], 0, filename, content)
        self.assertEqual(version["sha256"], content_sha)
        exact_utf8(version["original_filename"], filename)
        persisted_version = frappe.get_doc("RG Manuscript Version", version["name"])
        exact_utf8(persisted_version.original_filename, filename)
        self.assertEqual(get_version_text(version["name"])["paragraphs"][0]["text"], original)

        selection = create_selection(version["name"], original, anchor)
        persisted_selection = frappe.get_doc("RG Manuscript Selection", selection["name"])
        for value in (selection["original_text"], persisted_selection.original_text,
                      get_selection(selection["name"])["original_text"]):
            exact_utf8(value, original)
        self.assertEqual(persisted_selection.sha256, selection_sha)
        self.assertEqual(persisted_selection.version_sha256, content_sha)
        exact_utf8(persisted_selection.anchor, anchor)

        # Use the actual independent preview/confirmation transaction, rather
        # than calling save_ai_suggestion directly or changing stored hashes.
        conversation = api.create_conversation()["name"]
        state = guard(conversation=conversation)
        context = RunContext(self.users["one"], conversation, "", state["versions"])
        pending = propose(context, "manuscript_suggestion", {
            "selection": selection["name"], "suggestion": suggestion, "reason": reason,
        })
        self.assertFalse(frappe.db.exists("RG Manuscript Review", {"manuscript": self.manuscript["name"]}))
        exact_utf8(pending["preview"]["original_text"], original)
        exact_utf8(pending["preview"]["parameters"]["suggestion"], suggestion)
        exact_utf8(pending["preview"]["parameters"]["reason"], reason)
        confirmed = decide(pending["name"], "confirm")
        self.assertEqual(confirmed["status"], "consumed")
        self.assertEqual(decide(pending["name"], "confirm"), confirmed)
        persisted_review = frappe.get_doc("RG Manuscript Review", confirmed["result"]["name"])
        for field, expected in (("original_text", original), ("content", suggestion), ("reason", reason), ("anchor", anchor)):
            exact_utf8(persisted_review.get(field), expected)
        self.assertEqual(persisted_review.source_sha256, selection_sha)
        self.assertEqual(persisted_review.source_selection, selection["name"])

        frappe.set_user(self.users["teacher"])
        comment = add_review(version["name"], comment_text, anchor)
        frappe.set_user(self.users["one"])
        reply = reply_review(comment["name"], reply_text)
        frappe.set_user(self.users["teacher"])
        resolve_review(comment["name"], "closed")
        history = get_manuscript(self.manuscript["name"])
        exact_utf8(history["manuscript"]["title"], title)
        exact_utf8(frappe.db.get_value("RG Manuscript", self.manuscript["name"], "title"), title)
        rows = {row["name"]: row for row in history["reviews"]}
        self.assertEqual(set(rows), {persisted_review.name, comment["name"], reply["name"]})
        for field, expected in (("original_text", original), ("content", suggestion), ("reason", reason), ("anchor", anchor)):
            exact_utf8(rows[persisted_review.name][field], expected)
        self.assertEqual(rows[persisted_review.name]["source_sha256"], selection_sha)
        for record, expected in ((comment["name"], comment_text), (reply["name"], reply_text)):
            exact_utf8(rows[record]["content"], expected)
            exact_utf8(frappe.db.get_value("RG Manuscript Review", record, "content"), expected)
        exact_utf8(rows[comment["name"]]["anchor"], anchor)
        self.assertEqual(rows[comment["name"]]["status"], "closed")
        file = frappe.get_doc("File", persisted_version.material_file)
        self.assertEqual(file.file_name, _safe_storage_name(filename))
        self.assertFalse(any(character in file.file_name + file.file_url for character in '<>&"\''))
        self.assertTrue(Path(file.get_full_path()).is_file())
        self.assertEqual(read_stored_file_bytes(file), content)
        frappe.set_user(self.users["one"])
        exact_utf8(get_selection(selection["name"])["original_text"], original)

    def test_saved_ai_history_hides_changed_file_and_revoked_source_author(self):
        version, selection = self.selection()
        suggestion = save_ai_suggestion(selection["name"], "不可在失效后回显的AI建议", "依据", "source-integrity-op")
        file_name = frappe.db.get_value("RG Manuscript Version", version["name"], "material_file")
        file = frappe.get_doc("File", file_name)
        path, original = Path(file.get_full_path()), read_stored_file_bytes(file)
        try:
            path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
            history = get_manuscript(self.manuscript["name"])["reviews"]
            self.assertTrue(history[0]["blocked"])
            self.assertNotIn("original_text", history[0])
        finally:
            path.write_bytes(original)
        self.assertEqual(get_manuscript(self.manuscript["name"])["reviews"][0]["name"], suggestion["name"])
        frappe.set_user("Administrator")
        member = frappe.get_doc("RG Member", self.users["one"])
        member.active = 0
        member.save(ignore_permissions=True)
        frappe.set_user(self.users["teacher"])
        history = get_manuscript(self.manuscript["name"])["reviews"]
        self.assertTrue(history[0]["blocked"])
        self.assertNotIn("original_text", history[0])
        self.assertNotIn("content", history[0])

    def test_role_revocation_blocks_old_version_and_excerpt(self):
        version, selection = self.selection()
        frappe.set_user("Administrator")
        member_name = frappe.db.get_value("RG Member", {"user": self.users["one"]}, "name")
        member = frappe.get_doc("RG Member", member_name)
        member.active = 0
        member.save(ignore_permissions=True)
        frappe.set_user(self.users["one"])
        for function, argument in ((get_manuscript, self.manuscript["name"]),
                                   (get_version_text, version["name"]), (read_selection, selection["name"])):
            with self.subTest(method=function.__name__), self.assertRaises(frappe.PermissionError):
                function(argument)

    def _gate_trace(self):
        """Record the order of PDF subprocess work and global-gate acquisitions."""
        events = []
        real_gate, real_parse = manuscript_module.global_lock, manuscript_module._parse_pdf_isolated

        def gate():
            events.append("gate")
            return real_gate()

        def parse(content, validate_only=False):
            events.append("validate" if validate_only else "parse")
            return real_parse(content, validate_only=validate_only)

        return events, (patch.object(manuscript_module, "global_lock", side_effect=gate),
                        patch.object(manuscript_module, "_parse_pdf_isolated", side_effect=parse))

    def _upload(self, content, filename, revision=0, kind="student_draft"):
        request = EnvironBuilder(method="POST", base_url="http://localhost/",
                                 data={"file": (BytesIO(content), filename)}).get_request()
        with patch.object(manuscript_module, "_write"), patch.object(frappe.local, "request", request, create=True):
            return manuscript_module.upload_version(self.manuscript["name"], revision, kind)

    def test_pdf_subprocess_work_runs_before_the_global_gate(self):
        frappe.set_user(self.users["one"])
        events, (gate, parse) = self._gate_trace()
        with gate, parse:
            version = self._upload(text_pdf(), "gate-order.pdf")
            self.assertEqual(events.count("validate"), 1, events)  # not repeated under the gate
            self.assertEqual(events[:2], ["validate", "gate"], events)
            events.clear()
            text = get_version_text(version["name"])["paragraphs"]
            self.assertEqual(events[:2], ["parse", "gate"], events)
            events.clear()
            selection = create_selection(version["name"], text[0]["text"], "paragraph:0")
            self.assertEqual(events[:2], ["parse", "gate"], events)
        self.assertEqual(text[0]["text"], "Explicit authorized paragraph.")
        self.assertEqual(selection["manuscript_version"], version["name"])
        stored = frappe.get_doc("File", frappe.db.get_value("RG Manuscript Version", version["name"], "material_file"))
        self.assertEqual(hashlib.sha256(read_stored_file_bytes(stored)).hexdigest(), version["sha256"])

    def test_docx_multipart_validates_once_before_gate_and_rejects_damage_before_writing(self):
        from research_group import files as file_module
        events = []
        real_validate, real_gate = file_module.validate_manuscript, manuscript_module.global_lock

        def validate(filename, content):
            events.append("validate")
            return real_validate(filename, content)

        def gate():
            events.append("gate")
            return real_gate()

        with patch.object(manuscript_module, "validate_manuscript", side_effect=validate), \
                patch.object(file_module, "validate_manuscript", side_effect=validate), \
                patch.object(file_module, "_validate_docx", wraps=file_module._validate_docx) as parse, \
                patch.object(manuscript_module, "global_lock", side_effect=gate):
            version = self._upload(docx_bytes(), "multipart.docx")
            self.assertEqual(events[:2], ["validate", "gate"], events)
            self.assertEqual((events.count("validate"), parse.call_count), (1, 1))
            stored = frappe.get_doc("File", frappe.db.get_value("RG Manuscript Version", version["name"], "material_file"))
            self.assertEqual(read_stored_file_bytes(stored), docx_bytes())
            counts = (frappe.db.count("RG Manuscript Version"), frappe.db.count("File"))
            events.clear()
            with patch.object(manuscript_module, "save_managed_file") as save:
                with self.assertRaises(frappe.ValidationError):
                    self._upload(mutate_docx(omit=("word/document.xml",)), "damaged.docx", revision=1)
                save.assert_not_called()
            self.assertEqual(events, ["validate"])
            self.assertEqual((frappe.db.count("RG Manuscript Version"), frappe.db.count("File")), counts)

    def test_non_participant_is_rejected_before_any_byte_work(self):
        frappe.set_user(self.users["one"])
        version = self.version(text_pdf(), "owned.pdf")
        events, (gate, parse) = self._gate_trace()
        frappe.set_user(self.users["two"])
        with gate, parse:
            for call in (lambda: self._upload(text_pdf(), "intruder.pdf"),
                         lambda: get_version_text(version["name"]),
                         lambda: create_selection(version["name"], "Explicit authorized paragraph.")):
                with self.assertRaises(frappe.PermissionError):
                    call()
        self.assertEqual(events, [])
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.PermissionError):  # wrong kind for the author is also a cheap rejection
            self._upload(text_pdf(), "annotation.pdf", kind="teacher_annotation")

    def test_dual_author_reviewer_lost_upload_role_is_rejected_before_parsing(self):
        frappe.set_user("Administrator")
        user = frappe.get_doc("User", self.users["one"])
        user.append("roles", {"role": TEACHER})
        user.save(ignore_permissions=True)
        frappe.set_user(self.users["one"])
        self.manuscript = create_manuscript("虚构同一作者与评阅人", self.users["one"])
        for removed, kind, remaining in ((TEACHER, "teacher_annotation", STUDENT),
                                         (STUDENT, "student_draft", TEACHER)):
            with self.subTest(removed=removed):
                frappe.set_user("Administrator")
                user = frappe.get_doc("User", self.users["one"])
                user.set("roles", [{"role": remaining}])
                user.save(ignore_permissions=True)
                frappe.set_user(self.users["one"])
                events, (gate, parse) = self._gate_trace()
                with gate, parse, self.assertRaises(frappe.PermissionError):
                    self._upload(text_pdf(), "role-revoked.pdf", kind=kind)
                self.assertEqual(events, [])
        self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 0)

    def test_stale_prefilter_cannot_authorize_parsed_text(self):
        version = self.version(text_pdf(), "revoked.pdf")
        frappe.set_user("Administrator")
        member = frappe.get_doc("RG Member", frappe.db.get_value("RG Member", {"user": self.users["one"]}, "name"))
        member.active = 0
        member.save(ignore_permissions=True)
        frappe.set_user(self.users["one"])
        # Simulate a pre-revocation snapshot: the locked check after parsing must still deny.
        with patch.object(manuscript_module, "prefilter_roles", return_value={STUDENT}):
            for call in (lambda: get_version_text(version["name"]),
                         lambda: create_selection(version["name"], "Explicit authorized paragraph."),
                         lambda: self._upload(text_pdf(), "after-revoke.pdf", revision=1)):
                with self.assertRaises(frappe.PermissionError):
                    call()
        self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 1)

    def test_prevalidated_proof_must_match_the_submitted_bytes(self):
        frappe.set_user(self.users["one"])
        content = docx_bytes()
        forged = manuscript_module._Prevalidated("draft.docx", hashlib.sha256(b"other").hexdigest())
        for proof in (forged, SimpleNamespace(filename="draft.docx", digest=hashlib.sha256(content).hexdigest())):
            with self.assertRaises(frappe.PermissionError):
                submit_version(self.manuscript["name"], 0, "draft.docx", content, prevalidated=proof)
        self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 0)

    def test_all_five_snapshot_fields_are_rechecked_after_real_parsing(self):
        version = self.version(text_pdf(), "snapshot.pdf")
        parse = manuscript_module._version_paragraphs
        fields = ("manuscript", "material_file", "size_bytes", "sha256", "original_filename")
        self.assertEqual(manuscript_module._IMMUTABLE_VERSION_FIELDS, fields)
        for field in fields:
            def mismatch(snapshot):
                result = parse(snapshot)
                snapshot[field] = snapshot[field] + 1 if field == "size_bytes" else snapshot[field] + "-changed"
                return result

            with self.subTest(field=field), patch.object(manuscript_module, "_version_paragraphs", side_effect=mismatch) as parsed:
                for call in (lambda: get_version_text(version["name"]),
                             lambda: create_selection(version["name"], "Explicit authorized paragraph.")):
                    with self.assertRaises(frappe.PermissionError):
                        call()
                self.assertEqual(parsed.call_count, 2)
                self.assertEqual(frappe.db.count("RG Manuscript Selection", {"manuscript": self.manuscript["name"]}), 0)
        text = get_version_text(version["name"])["paragraphs"][0]["text"]
        self.assertEqual(text, "Explicit authorized paragraph.")
        selection = create_selection(version["name"], text)
        self.assertEqual(selection["manuscript_version"], version["name"])
        self.assertEqual(frappe.db.count("RG Manuscript Selection", {"manuscript": self.manuscript["name"]}), 1)

    def _race(self, jobs):
        frappe.db.commit()
        self.committed_fixture = True
        parent = Path(os.environ.get("RG_MANUSCRIPT_TEST_WORKDIR") or frappe.get_site_path(
            "private", "tasks", "20261108-fdu-accounts-meals", "scratch", "manuscript-races")).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        command = "from research_group.tests.test_manuscripts import manuscript_race_worker; manuscript_race_worker()"
        sites = str(Path(frappe.local.sites_path).resolve())
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=parent) as folder:
            root, processes = Path(folder), []
            try:
                for index, job in enumerate(jobs):
                    processes.append(subprocess.Popen(
                        [sys.executable, "-c", command, frappe.local.site, sites, str(root), str(index),
                         json.dumps(job)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=sites))
                deadline = time.monotonic() + 30
                while not all((root / f"{index}.ready").exists() for index in range(len(jobs))):
                    if time.monotonic() > deadline or any(process.poll() is not None for process in processes):
                        self.fail("manuscript workers did not reach the independent snapshot barrier")
                    time.sleep(0.03)
                (root / "start").write_text("start", encoding="ascii")
                results = []
                for process in processes:
                    output, error = process.communicate(timeout=40)
                    self.assertEqual(process.returncode, 0, error[-3000:])
                    results.append(json.loads(output.strip().splitlines()[-1]))
                self.assertEqual(len({result["connection"] for result in results}), len(jobs))
                frappe.db.rollback()
                return results
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

    def test_independent_revoked_teacher_retains_only_student_manuscripts_and_files(self):
        version = self.version()
        frappe.set_user("Administrator")
        user = frappe.get_doc("User", self.users["teacher"])
        user.append("roles", {"role": STUDENT})
        user.save(ignore_permissions=True)
        frappe.set_user(self.users["teacher"])
        own = create_manuscript("虚构教师兼学生本人论文", self.users["other_teacher"])
        results = self._race([
            {"action": "revoke_teacher", "user": "Administrator", "target": self.users["teacher"], "hold": True},
            {"action": "list", "user": self.users["teacher"], "version": version["name"]},
        ])
        self.assertTrue(all(result["ok"] for result in results), results)
        reader = results[1]
        self.assertIn(TEACHER, reader["cached_roles"])
        self.assertEqual(reader["current_roles"], [STUDENT])
        self.assertEqual(reader["manuscripts"], [own["name"]])
        self.assertFalse(reader["files"])
        self.assertFalse(reader["file_permission"])
        self.assertTrue(reader["text_denied"])

    def test_independent_parser_does_not_hold_gate_or_identity_locks_and_rechecks_revocation(self):
        version = self.version(text_pdf(), "parse-revocation.pdf")
        for operation in ("multipart_parse", "text_parse", "selection_parse"):
            with self.subTest(operation=operation):
                # _race commits the initial upload/identity transaction. No
                # list/get endpoint acquires a gate before the parser starts.
                results = self._race([
                    {"action": "revoke_during_parse", "user": "Administrator", "target": self.users["one"]},
                    {"action": operation, "user": self.users["one"], "manuscript": self.manuscript["name"],
                     "version": version["name"]},
                ])
                writer, reader = results
                self.assertTrue(writer["ok"] and writer["revocation_committed_while_parser_paused"], results)
                self.assertFalse(reader["ok"], results)
                self.assertEqual(reader["error"], "PermissionError", results)
                self.assertTrue(reader["real_parse_completed"] and reader["revocation_before_parse_resume"], results)
                self.assertFalse(reader["returned_response"], results)
                self.assertEqual(frappe.db.count("RG Manuscript Version", {"manuscript": self.manuscript["name"]}), 1)
                self.assertEqual(frappe.db.count("RG Manuscript Selection", {"manuscript": self.manuscript["name"]}), 0)
                frappe.set_user("Administrator")
                user = frappe.get_doc("User", self.users["one"])
                user.enabled = 1
                user.append("roles", {"role": STUDENT})
                user.save(ignore_permissions=True)
                member = frappe.get_doc("RG Member", self.users["one"])
                member.active = 1
                member.save(ignore_permissions=True)

    def test_independent_competing_revisions_preserve_one_immutable_version(self):
        results = self._race([
            {"action": "upload", "user": self.users["one"], "manuscript": self.manuscript["name"],
             "filename": f"concurrent-{index}.docx"} for index in (1, 2)
        ])
        self.assertEqual(sum(result["ok"] for result in results), 1, results)
        self.assertEqual(next(result["error"] for result in results if not result["ok"]), "ValidationError")
        frappe.set_user(self.users["one"])
        versions = get_manuscript(self.manuscript["name"])["versions"]
        self.assertEqual([version["version"] for version in versions], [1])
        self.assertEqual(versions[0]["sha256"], hashlib.sha256(docx_bytes()).hexdigest())

    def test_independent_close_before_reply_preserves_closed_state_and_audit(self):
        version = self.version()
        frappe.set_user(self.users["teacher"])
        comment = add_review(version["name"], "并发处理意见")
        results = self._race([
            {"action": "close_review", "user": self.users["teacher"], "review": comment["name"], "hold": True},
            {"action": "reply_review", "user": self.users["one"], "review": comment["name"]},
        ])
        self.assertTrue(all(result["ok"] for result in results), results)
        frappe.set_user(self.users["one"])
        reviews = get_manuscript(self.manuscript["name"])["reviews"]
        self.assertEqual(len(reviews), 2)
        parent = next(row for row in reviews if row["name"] == comment["name"])
        self.assertEqual(parent["status"], "closed")
        self.assertEqual(len(parent["status_history"]), 1)
        self.assertEqual((parent["status_history"][0]["actor"], parent["status_history"][0]["before"],
                          parent["status_history"][0]["after"]), (self.users["teacher"], "open", "closed"))
        self.assertEqual(next(row for row in reviews if row["kind"] == "student_reply")["parent_review"], comment["name"])

    def test_independent_revoke_masks_management_and_saved_advice_after_wait(self):
        version, selection = self.selection()
        save_ai_suggestion(selection["name"], "并发撤销后不得读回的建议", "", "race-selection-advice")
        results = self._race([
            {"action": "revoke_selection", "user": self.users["one"], "selection": selection["name"], "hold": True},
            {"action": "selection_state", "user": self.users["one"], "manuscript": self.manuscript["name"]},
        ])
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(results[1]["selections"], [])
        self.assertEqual(results[1]["blocked"], [True])
        self.assertFalse(results[1]["has_body"])


def manuscript_paused_pdf_worker(folder, worker_id, validate_only=False):
    """Run the real bounded PDF worker, pausing its real parser before return."""
    from research_group import manuscripts
    root = Path(folder)
    extract = manuscripts._extract_pdf_paragraphs

    def paused_extract(*args, **kwargs):
        result = extract(*args, **kwargs)
        (root / f"{worker_id}.parsing").write_text(str(os.getpid()), encoding="ascii")
        deadline = time.monotonic() + 15
        while not (root / "0.revocation-committed").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("identity revocation could not commit while the real parser was paused")
            time.sleep(0.03)
        (root / f"{worker_id}.parse-resumed").write_text(str(time.monotonic_ns()), encoding="ascii")
        return result

    with patch.object(manuscripts, "_extract_pdf_paragraphs", side_effect=paused_extract):
        manuscripts._pdf_parse_worker(validate_only=validate_only)


def manuscript_race_worker():
    from research_group import manuscripts
    from research_group.ai.security import current_roles
    from research_group.permissions import file_has_permission
    site, sites_path, folder, worker_id, job_json = sys.argv[1:]
    root, job = Path(folder), json.loads(job_json)
    frappe.init(site, sites_path=sites_path)
    frappe.connect()
    frappe.in_test = True
    try:
        frappe.set_user(job["user"])
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cached_roles = sorted(frappe.get_roles())
        # Preserve a real ordinary-read snapshot before waiting for revocation.
        frappe.db.sql("SELECT name FROM `tabRG Manuscript` ORDER BY name LIMIT 1")
        parse_actions = {"multipart_parse", "text_parse", "selection_parse"}
        if job["action"] in parse_actions | {"revoke_during_parse"}:
            # Release every setup transaction before testing parse-time locks.
            frappe.db.commit()
        if job.get("hold"):
            manuscripts.global_lock()
        connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
        (root / f"{worker_id}.ready").write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not (root / "start").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("manuscript start barrier")
            time.sleep(0.03)
        if job.get("hold"):
            while not (root / "1.requested").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("manuscript reader did not request the held gate")
                time.sleep(0.03)
        original = manuscripts.global_lock

        def observed_gate():
            (root / f"{worker_id}.requested").write_text("requested", encoding="ascii")
            return original()

        try:
            extra = {}
            returned_response = False
            with patch.object(manuscripts, "global_lock", side_effect=observed_gate):
                if job["action"] == "revoke_during_parse":
                    while not (root / "1.parsing").exists():
                        if time.monotonic() > deadline:
                            raise TimeoutError("the real parser did not reach its pause")
                        time.sleep(0.03)
                    manuscripts.global_lock()
                    user = frappe.get_doc("User", job["target"])
                    user.enabled = 0
                    user.set("roles", [row for row in user.roles if row.role != STUDENT])
                    user.save(ignore_permissions=True)
                    member = frappe.get_doc("RG Member", job["target"])
                    member.active = 0
                    member.save(ignore_permissions=True)
                    assert not (root / "1.parse-resumed").exists()
                    frappe.db.commit()
                    (root / "0.revocation-committed").write_text(str(time.monotonic_ns()), encoding="ascii")
                    extra = {"revocation_committed_while_parser_paused": True}
                elif job["action"] in parse_actions:
                    run = manuscripts.subprocess.run

                    def observed_parser(command, *args, **kwargs):
                        if len(command) == 3 and "_pdf_parse_worker" in command[2]:
                            validate_only = "validate_only=True" in command[2]
                            entry = ("from research_group.tests.test_manuscripts import manuscript_paused_pdf_worker; "
                                     f"manuscript_paused_pdf_worker({str(root)!r}, {worker_id!r}, {validate_only!r})")
                            command = [command[0], "-c", entry]
                        return run(command, *args, **kwargs)

                    with patch.object(manuscripts.subprocess, "run", side_effect=observed_parser), \
                            patch.object(manuscripts, "PDF_PROCESS_TIMEOUT", 20):
                        if job["action"] == "multipart_parse":
                            request = EnvironBuilder(method="POST", base_url="http://localhost/",
                                                     data={"file": (BytesIO(text_pdf()), "waiting-parse.pdf")}).get_request()
                            with patch.object(manuscripts, "_write"), patch.object(frappe.local, "request", request, create=True):
                                manuscripts.upload_version(job["manuscript"], 1)
                        elif job["action"] == "text_parse":
                            manuscripts.get_version_text(job["version"])
                        else:
                            manuscripts.create_selection(job["version"], "Explicit authorized paragraph.")
                        returned_response = True
                elif job["action"] == "revoke_teacher":
                    user = frappe.get_doc("User", job["target"])
                    user.set("roles", [row for row in user.roles if row.role != TEACHER])
                    user.save(ignore_permissions=True)
                elif job["action"] == "upload":
                    result = manuscripts.submit_version(job["manuscript"], 0, job["filename"], docx_bytes())
                    extra = {"version": result["name"]}
                elif job["action"] == "close_review":
                    manuscripts.resolve_review(job["review"], "closed")
                elif job["action"] == "reply_review":
                    manuscripts.reply_review(job["review"], "虚构独立进程回复")
                elif job["action"] == "revoke_selection":
                    manuscripts.revoke_selection(job["selection"])
                elif job["action"] == "selection_state":
                    selections = manuscripts.list_selections(manuscript=job["manuscript"])
                    reviews = manuscripts.get_manuscript(job["manuscript"])["reviews"]
                    extra = {"selections": [row["name"] for row in selections["items"]],
                             "blocked": [row.get("blocked", False) for row in reviews],
                             "has_body": any("content" in row or "original_text" in row for row in reviews)}
                else:
                    rows = manuscripts.list_manuscripts()["items"]
                    version = frappe.get_doc("RG Manuscript Version", job["version"])
                    file = frappe.get_doc("File", version.material_file)
                    try:
                        manuscripts.get_version_text(version.name)
                        denied = False
                    except frappe.PermissionError:
                        denied = True
                    extra = {"cached_roles": cached_roles, "current_roles": sorted(current_roles()),
                             "manuscripts": [row["name"] for row in rows],
                             "files": frappe.get_list("File", filters={"name": file.name}, pluck="name"),
                             "file_permission": file_has_permission(file), "text_denied": denied}
            frappe.db.commit()
            result = {"ok": True, "connection": connection, **extra}
        except (frappe.ValidationError, frappe.PermissionError) as error:
            frappe.db.rollback()
            result = {"ok": False, "connection": connection, "error": type(error).__name__}
        if job["action"] in parse_actions:
            committed, resumed = root / "0.revocation-committed", root / f"{worker_id}.parse-resumed"
            result.update(returned_response=returned_response,
                          real_parse_completed=(root / f"{worker_id}.parsing").exists(),
                          revocation_before_parse_resume=(committed.exists() and resumed.exists()
                              and int(committed.read_text()) < int(resumed.read_text())))
        print(json.dumps(result))
    finally:
        frappe.destroy()
