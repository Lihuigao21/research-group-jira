"""Minimum file structure, filename limits and bounded native ZIP reads.

The positive presentation files are unmodified Apache POI regression fixtures;
fixtures/provenance.json binds their origin, license and original byte hashes.
Corrupt variants below are produced in memory from those real files.
"""
import hashlib
import json
from io import BytesIO
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import frappe
import olefile

from research_group.files import (
    MAX_ARCHIVE_BYTES, MAX_FILENAME_BYTES, MAX_FILENAME_CHARS,
    MAX_PACKAGE_XML_ENTRIES, MAX_PRESENTATION_XML_BYTES, MAX_XML_PART_BYTES,
    OFFICE_REL_NAMESPACES, PACKAGE_REL_NS, PRESENTATION_NAMESPACES, WORD_NAMESPACES,
    _PackageXML, _PackageXMLBudget, _WordText, _read_package_xml, _read_word_xml, _validate_pptx,
    read_stored_file_bytes, validate_material, zip_files,
)

FIXTURES = Path(__file__).parent / "fixtures"


def presentation_bytes(extension):
    name = "basic_test_ppt_file.ppt" if extension == ".ppt" else "sample.pptx"
    return (FIXTURES / name).read_bytes()


def mutate_ppt(stream_name, mutate):
    """Keep the genuine OLE envelope and alter one same-size binary stream."""
    output = BytesIO(presentation_bytes(".ppt"))
    with olefile.OleFileIO(output, write_mode=True) as ole:
        stream = bytearray(ole.openstream(stream_name).read())
        mutate(stream)
        ole.write_stream(stream_name, bytes(stream))
    return output.getvalue()


def ppt_locations():
    with olefile.OleFileIO(BytesIO(presentation_bytes(".ppt"))) as ole:
        current = ole.openstream("Current User").read()
        document = ole.openstream("PowerPoint Document").read()
    edit = struct.unpack_from("<I", current, 16)[0]
    directory = struct.unpack_from("<I", document, edit + 20)[0]
    entry = struct.unpack_from("<I", document, directory + 8)[0]
    assert entry & 0xFFFFF == 1  # The real fixture's newest directory contains document id 1.
    doc = struct.unpack_from("<I", document, directory + 12)[0]
    return edit, directory, doc, len(document)


def mutate_pptx(replace=None, omit=()):
    output = BytesIO()
    with ZipFile(BytesIO(presentation_bytes(".pptx"))) as original, ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for info in original.infolist():
            if info.filename not in omit:
                archive.writestr(info.filename, (replace or {}).get(info.filename, original.read(info)))
        for name, content in (replace or {}).items():
            if name not in original.namelist():
                # ZipInfo normalizes backslashes on Windows; override after its
                # constructor so the corruption is identical on Windows/Linux.
                info = ZipInfo(name)
                info.filename = info.orig_filename = name
                archive.writestr(info, content)
    return output.getvalue()


class TestMaterialFormats(unittest.TestCase):
    def test_word_contexts_and_separators_share_retained_entry_budget_before_allocation(self):
        word = WORD_NAMESPACES[0]
        budget = _PackageXMLBudget(limit=5)
        summary = _WordText(budget)
        summary.start(f"{{{word}}}document", {})
        summary.start(f"{{{word}}}body", {})
        summary.start(f"{{{word}}}p", {})
        summary.start(f"{{{word}}}r", {})
        summary.start(f"{{{word}}}t", {})
        summary.data("ab")
        summary.end(f"{{{word}}}t")
        summary.start(f"{{{word}}}tab", {})
        summary.end(f"{{{word}}}tab")
        summary.start(f"{{{word}}}br", {})
        summary.end(f"{{{word}}}br")
        self.assertEqual(budget.used, 5)
        with self.assertRaisesRegex(ValueError, "retained-entry budget"):
            summary.start(f"{{{word}}}cr", {})
        self.assertIs(summary.budget, budget)
        self.assertEqual(summary.text_size, 4)
        self.assertEqual(summary.paragraph_contexts[0][0], ["ab", "\t", "\n"])

    def test_word_nested_contexts_and_separator_text_remain_bounded(self):
        word = WORD_NAMESPACES[0]
        summary = _WordText()
        with patch("research_group.files.MAX_MANUSCRIPT_PARAGRAPHS", 2):
            summary.start(f"{{{word}}}p", {})
            summary.start(f"{{{word}}}txbxContent", {})
            summary.start(f"{{{word}}}p", {})
            summary.start(f"{{{word}}}txbxContent", {})
            with self.assertRaisesRegex(ValueError, "Word paragraph limit"):
                summary.start(f"{{{word}}}p", {})
        self.assertEqual(summary.paragraph_count, 2)
        self.assertEqual(len(summary.paragraph_contexts), 2)
        summary = _WordText()
        with patch("research_group.files.MAX_MANUSCRIPT_TEXT", 3):
            summary.start(f"{{{word}}}p", {})
            summary.start(f"{{{word}}}r", {})
            summary.start(f"{{{word}}}t", {})
            summary.data("A")
            summary.end(f"{{{word}}}t")
            summary.start(f"{{{word}}}tab", {})
            summary.end(f"{{{word}}}tab")
            summary.start(f"{{{word}}}br", {})
            summary.end(f"{{{word}}}br")
            with self.assertRaisesRegex(ValueError, "Word text limit"):
                summary.start(f"{{{word}}}cr", {})
        self.assertEqual(summary.text_size, 3)
        self.assertEqual(summary.paragraph_contexts[0][0], ["A", "\t", "\n"])

    def test_word_main_xml_part_cap_precedes_parser_and_member_read(self):
        archive = Mock()
        info = SimpleNamespace(file_size=MAX_PRESENTATION_XML_BYTES + 1)
        with patch("research_group.files.ElementTree.XMLParser") as parser:
            with self.assertRaisesRegex(ValueError, "Word document size"):
                _read_word_xml(archive, info, _PackageXMLBudget())
        archive.open.assert_not_called()
        parser.assert_not_called()

    def test_real_positive_fixtures_and_original_hashes(self):
        provenance = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        for item in provenance["files"]:
            content = (FIXTURES / item["file"]).read_bytes()
            self.assertEqual(len(content), item["bytes"])
            self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
        for extension in (".ppt", ".pptx"):
            with self.subTest(extension=extension):
                self.assertEqual(validate_material("real" + extension, presentation_bytes(extension)), extension)

    def test_ppt_current_user_record_and_offsets(self):
        _, _, _, size = ppt_locations()
        variants = (
            lambda b: b.__setitem__(slice(None), bytes(len(b))),
            lambda b: struct.pack_into("<H", b, 2, 0x03E8),
            lambda b: struct.pack_into("<I", b, 4, 0),
            lambda b: struct.pack_into("<I", b, 16, size + 1),
            lambda b: struct.pack_into("<I", b, 12, 0xF3D1C4DF),
        )
        for index, mutate in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(frappe.ValidationError):
                validate_material("corrupt.ppt", mutate_ppt("Current User", mutate))

    def test_ppt_user_edit_persist_directory_and_document(self):
        edit, directory, document, size = ppt_locations()
        variants = (
            lambda b: struct.pack_into("<H", b, edit + 2, 0),
            lambda b: struct.pack_into("<I", b, edit + 4, 0),
            lambda b: struct.pack_into("<I", b, edit + 16, edit),
            lambda b: struct.pack_into("<I", b, edit + 20, size + 1),
            lambda b: struct.pack_into("<H", b, directory + 2, 0),
            lambda b: struct.pack_into("<I", b, directory + 8, 0xFFF00001),
            lambda b: struct.pack_into("<I", b, directory + 12, size + 1),
            lambda b: b.__setitem__(slice(document, document + 8), bytes(8)),
            lambda b: struct.pack_into("<I", b, document + 4, size),
            lambda b: b.__setitem__(slice(document + 8, document + 16), bytes(8)),
        )
        for index, mutate in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(frappe.ValidationError):
                validate_material("corrupt.ppt", mutate_ppt("PowerPoint Document", mutate))

    def test_pptx_requires_opc_entry_and_referenced_parts(self):
        with ZipFile(BytesIO(presentation_bytes(".pptx"))) as original:
            output = BytesIO()
            with ZipFile(output, "w") as shell:
                for name in ("[Content_Types].xml", "ppt/presentation.xml"):
                    shell.writestr(name, original.read(name))
            old_two_xml_shell = output.getvalue()
            root = original.read("_rels/.rels")
            presentation = original.read("ppt/presentation.xml")
            rels = original.read("ppt/_rels/presentation.xml.rels")
        variants = (
            old_two_xml_shell,
            mutate_pptx(omit=("_rels/.rels",)),
            mutate_pptx(omit=("ppt/_rels/presentation.xml.rels",)),
            mutate_pptx(omit=("ppt/slides/slide1.xml",)),
            mutate_pptx({"_rels/.rels": root.replace(b"/officeDocument\"", b"/notPresentation\"")}),
            mutate_pptx({"ppt/presentation.xml": presentation.replace(b'r:id="rId2"', b'r:id="missing"')}),
            mutate_pptx({"ppt/presentation.xml": presentation.replace(b' r:id="rId2"', b'')}),
            mutate_pptx({"ppt/_rels/presentation.xml.rels": rels.replace(b"slides/slide1.xml", b"slides/missing.xml")}),
            mutate_pptx({"ppt/slides/slide1.xml": b'<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'}),
        )
        for index, content in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(frappe.ValidationError):
                validate_material("broken.pptx", content)

    def test_pptx_rejects_package_escape_and_harmful_xml(self):
        with ZipFile(BytesIO(presentation_bytes(".pptx"))) as original:
            rels = original.read("ppt/_rels/presentation.xml.rels")
        for target in (b"../../escape.xml", b"%2e%2e/%2e%2e/escape.xml", b"..\\escape.xml", b"slides%2fslide1.xml"):
            with self.subTest(target=target), self.assertRaises(frappe.ValidationError):
                validate_material("escape.pptx", mutate_pptx({
                    "ppt/_rels/presentation.xml.rels": rels.replace(b"slides/slide1.xml", target),
                }))
        for name in ("../escape.xml", "/escape.xml", "ppt/../escape.xml", "ppt\\escape.xml"):
            with self.subTest(name=name), self.assertRaises(frappe.ValidationError):
                validate_material("escape.pptx", mutate_pptx({name: b"<root/>"}))
        dangerous = '<!DOCTYPE sld [<!ENTITY payload "expanded">]><sld>&payload;</sld>'
        for encoding in ("utf-8", "utf-16", "utf-32"):
            declaration = f'<?xml version="1.0" encoding="{encoding}"?>'
            content = (declaration + dangerous).encode(encoding)
            with self.subTest(encoding=encoding), self.assertRaises(frappe.ValidationError):
                validate_material("entity.pptx", mutate_pptx({"ppt/slides/slide1.xml": content}))

    def test_pptx_follows_root_relationship_to_renamed_presentation(self):
        output = BytesIO()
        with ZipFile(BytesIO(presentation_bytes(".pptx"))) as original, ZipFile(output, "w") as archive:
            for info in original.infolist():
                name, content = info.filename, original.read(info)
                if name in {"[Content_Types].xml", "_rels/.rels"}:
                    content = content.replace(b"ppt/presentation.xml", b"ppt/custom.xml")
                if name == "ppt/presentation.xml":
                    name = "ppt/custom.xml"
                elif name == "ppt/_rels/presentation.xml.rels":
                    name = "ppt/_rels/custom.xml.rels"
                archive.writestr(name, content)
        self.assertEqual(validate_material("renamed.pptx", output.getvalue()), ".pptx")

    def test_pptx_expansion_limit_precedes_member_read(self):
        self.assertEqual(MAX_ARCHIVE_BYTES, 250 * 1024 * 1024)
        content = bytearray(presentation_bytes(".pptx"))
        central = content.index(b"PK\x01\x02")
        struct.pack_into("<I", content, central + 24, MAX_ARCHIVE_BYTES + 1)
        with patch("research_group.files.ZipFile.open", side_effect=AssertionError("must reject before reading")) as read:
            with self.assertRaises(frappe.ValidationError):
                validate_material("oversized.pptx", bytes(content))
            read.assert_not_called()

    def test_pptx_xml_part_limit_precedes_parser_and_member_open(self):
        self.assertEqual(MAX_XML_PART_BYTES, 16 * 1024 * 1024)
        archive = Mock()
        info = SimpleNamespace(file_size=MAX_XML_PART_BYTES + 1)
        with patch("research_group.files.ElementTree.XMLParser") as parser:
            with self.assertRaisesRegex(ValueError, "XML part size"):
                _read_package_xml(archive, info)
        archive.open.assert_not_called()
        parser.assert_not_called()

    def test_pptx_presentation_limit_precedes_parsing(self):
        self.assertEqual(MAX_PRESENTATION_XML_BYTES, 8 * 1024 * 1024)
        original_infolist = ZipFile.infolist
        # Alter only central-directory metadata, without creating large scratch
        # data. The presentation cap also applies when its filename is changed.
        for name, size, message in (
            ("ppt/presentation.xml", MAX_PRESENTATION_XML_BYTES + 1, "presentation size"),
            ("ppt/slides/slide1.xml", MAX_XML_PART_BYTES + 1, "XML part size"),
        ):
            def sized_members(archive):
                infos = original_infolist(archive)
                next(info for info in infos if info.filename == name).file_size = size
                return infos

            parsed = []

            def read_xml(archive, info, budget=None):
                parsed.append(info.filename)
                return _read_package_xml(archive, info, budget)

            with (self.subTest(name=name),
                  patch("research_group.files.ZipFile.infolist", sized_members),
                  patch("research_group.files._read_package_xml", read_xml),
                  self.assertRaisesRegex(ValueError, message)):
                _validate_pptx(presentation_bytes(".pptx"))
            if name == "ppt/presentation.xml":
                self.assertNotIn(name, parsed)

    def test_pptx_retained_budget_precedes_attribute_and_reference_allocation(self):
        class UncopyableAttrs:
            def __len__(self):
                return 3

            def __contains__(self, key):
                return False

            def __iter__(self):
                raise AssertionError("attributes must not be copied past the budget")

        budget = _PackageXMLBudget(limit=3)
        summary = _PackageXML(budget)
        summary.start(f"{{{PACKAGE_REL_NS}}}Relationships", {})
        with self.assertRaisesRegex(ValueError, "retained-entry budget"):
            summary.start(f"{{{PACKAGE_REL_NS}}}Relationship", UncopyableAttrs())
        self.assertEqual(summary.children, [])
        self.assertEqual(budget.used, 0)

        reference = f"{{{OFFICE_REL_NAMESPACES[0]}}}id"
        budget = _PackageXMLBudget(limit=1)
        summary = _PackageXML(budget)
        summary.start("root", {})
        summary.start("first", {reference: "rId1"})
        with self.assertRaisesRegex(ValueError, "retained-entry budget"):
            summary.start("second", {reference: "rId2"})
        self.assertEqual(summary.references, {"rId1"})
        self.assertEqual(budget.used, 1)

        budget = _PackageXMLBudget(limit=2)
        summary = _PackageXML(budget)
        summary.start(f"{{{PRESENTATION_NAMESPACES[0]}}}presentation", {})
        with self.assertRaisesRegex(ValueError, "retained-entry budget"):
            summary.start(f"{{{PRESENTATION_NAMESPACES[0]}}}sldId", {reference: "rId1"})
        self.assertEqual(summary.slides, [])
        self.assertEqual(summary.slide_ids, set())

    def test_pptx_retained_budget_is_shared_by_all_package_parts(self):
        self.assertEqual(MAX_PACKAGE_XML_ENTRIES, 100_000)
        budgets = []

        def read_xml(archive, info, budget=None):
            budgets.append(budget)
            return _read_package_xml(archive, info, budget)

        content = presentation_bytes(".pptx")
        with patch("research_group.files._read_package_xml", read_xml):
            self.assertEqual(validate_material("real.pptx", content), ".pptx")
        self.assertGreater(len(budgets), 1)
        self.assertIsNotNone(budgets[0])
        self.assertTrue(all(budget is budgets[0] for budget in budgets))
        used = budgets[0].used
        self.assertGreater(used, 1)
        with patch("research_group.files.MAX_PACKAGE_XML_ENTRIES", used):
            self.assertEqual(validate_material("at-budget.pptx", content), ".pptx")
        with (patch("research_group.files.MAX_PACKAGE_XML_ENTRIES", used - 1),
              self.assertRaisesRegex(ValueError, "retained-entry budget")):
            _validate_pptx(content)

    def test_filename_character_and_utf8_storage_boundaries(self):
        self.assertEqual(MAX_FILENAME_CHARS, 134)
        self.assertEqual(MAX_FILENAME_BYTES, 249)
        content = presentation_bytes(".ppt")
        for name in ("a" * 129 + ".ppt", "a" * 130 + ".ppt", "中" * 81 + "a.ppt", "中" * 81 + "aa.ppt"):
            with self.subTest(name=name):
                self.assertEqual(validate_material(name, content), ".ppt")
        for name, message in (("a" * 131 + ".ppt", "134"), ("中" * 82 + ".ppt", "249")):
            with self.subTest(name=name), self.assertRaisesRegex(frappe.ValidationError, message):
                validate_material(name, content)


class TestBoundedStoredReads(unittest.TestCase):
    def test_native_zip_passes_remaining_budget_before_reading(self):
        one = SimpleNamespace(name="one", file_name="one.pdf", attached_to_doctype=None, has_permission=lambda *_: True, is_downloadable=lambda: True)
        two = SimpleNamespace(name="two", file_name="two.pdf", attached_to_doctype=None, has_permission=lambda *_: True, is_downloadable=lambda: True)
        with (patch("research_group.files.frappe.get_doc", side_effect=[one, two]),
              patch("research_group.files.read_stored_file_bytes", side_effect=[b"abc", b"defg"]) as read,
              patch.object(frappe, "response", {})):
            zip_files(json.dumps(["one", "two"]))
            self.assertEqual(read.call_args_list[0].kwargs, {"max_read_bytes": MAX_ARCHIVE_BYTES})
            self.assertEqual(read.call_args_list[1].kwargs, {"max_read_bytes": MAX_ARCHIVE_BYTES - 3})
            with ZipFile(BytesIO(frappe.response["filecontent"])) as archive:
                self.assertEqual(archive.read("one_one.pdf"), b"abc")
                self.assertEqual(archive.read("two_two.pdf"), b"defg")

    def test_live_file_size_and_bounded_read(self):
        # Task-local runtime directory; no multi-hundred-megabyte scratch file.
        work = Path(frappe.get_site_path("private", "files"))
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as directory:
            source = Path(directory) / "bytes.bin"
            source.write_bytes(b"original-bytes")
            file = Mock(is_folder=False, file_url="/private/files/test.bin")
            file.get_full_path.return_value = str(source)
            self.assertEqual(read_stored_file_bytes(file, max_read_bytes=14), b"original-bytes")
            with self.assertRaises(frappe.ValidationError):
                read_stored_file_bytes(file, max_read_bytes=13)

    def test_growth_between_size_check_and_read_remains_bounded(self):
        file = Mock(is_folder=False, file_url="/private/files/test.bin")
        source = Mock()
        source.__enter__ = Mock(return_value=source)
        source.__exit__ = Mock(return_value=False)
        source.read.return_value = b"123456"
        with (patch("research_group.files.open", return_value=source),
              patch("research_group.files.os.fstat", return_value=SimpleNamespace(st_size=3))):
            with self.assertRaises(frappe.ValidationError):
                read_stored_file_bytes(file, max_read_bytes=5)
        source.read.assert_called_once_with(6)
