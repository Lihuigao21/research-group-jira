"""Native history and File boundaries for surveys that support no attachments."""
import json
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.client import get as api_get
from frappe.tests import IntegrationTestCase

from research_group.files import (
    ResearchGroupFile, SURVEY_FILE_DOCTYPES, read_stored_file_bytes, zip_files,
)
from research_group.policy import MANAGED
from research_group.surveys import close_survey, publish_survey, submit_response
from research_group.tests import test_core as core


class TestSurveyPrivacy(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-survey-privacy-" + frappe.generate_hash(length=10)
        self.users = core.create_members(self.prefix)
        self.native_files, self.legacy_files = [], []

    def tearDown(self):
        frappe.set_user("Administrator")
        # Test-only historical rows share this test's ordinary blob. Remove
        # their rows without asking File to delete another row's physical data.
        for name in self.legacy_files:
            frappe.db.delete("File", {"name": name})
        for name in self.native_files:
            if frappe.db.exists("File", name):
                frappe.delete_doc("File", name, ignore_permissions=True, force=True)
        frappe.db.rollback()
        frappe.clear_cache()
        super().tearDown()

    def _survey(self, published=False):
        frappe.set_user(self.users["secretary"])
        doc = frappe.get_doc({"doctype": "RG Survey", "title": self.prefix + "草稿",
            "questions": [{"title": "虚构简答", "question_type": "简答", "required": 1}]}).insert()
        if published:
            publish_survey(doc.name)
            doc.reload()
        return doc

    def _parents(self):
        draft = self._survey()
        published = self._survey(published=True)
        frappe.set_user(self.users["one"])
        response = submit_response(published.name, {published.questions[0].name: "本人虚构答卷"})
        return [("RG Survey", draft.name), ("RG Survey Question", draft.questions[0].name),
                ("RG Survey Response", response["name"])]

    def _legacy_version(self, doctype, name, marker):
        frappe.set_user("Administrator")
        return frappe.get_doc({"doctype": "Version", "ref_doctype": doctype, "docname": name,
            "data": json.dumps({"changed": [["title", marker, "当前公开内容"]]}, ensure_ascii=False),
        }).insert(ignore_permissions=True)

    def _native_file(self, label):
        # Native insert derives owner from the current user, even when supplied.
        frappe.set_user(self.users["secretary"])
        file = frappe.get_doc({"doctype": "File", "file_name": self.prefix + label + ".pdf",
            "content": core.pdf_bytes(self.prefix + label), "is_private": 1}).insert(ignore_permissions=True)
        self.native_files.append(file.name)
        self.assertEqual(frappe.db.get_value("File", file.name, "owner"), self.users["secretary"])
        return file

    def _legacy_file(self, original, doctype, name):
        row = frappe.get_doc({"doctype": "File", "name": self.prefix + frappe.generate_hash(length=6),
            "file_name": original.file_name, "file_url": original.file_url, "is_private": 1,
            "is_folder": 0, "folder": "Home", "owner": self.users["secretary"],
            "content_hash": original.content_hash, "file_size": original.file_size,
            "attached_to_doctype": doctype, "attached_to_name": name})
        row.db_insert()  # seed a pre-existing row without exercising the new guard
        self.legacy_files.append(row.name)
        return frappe.get_doc("File", row.name)

    def test_native_survey_and_answer_loading_omit_legacy_versions(self):
        from frappe.desk.form.load import get_docinfo, getdoc

        self.assertFalse(frappe.get_meta("RG Survey").track_changes)
        self.assertFalse(frappe.get_meta("RG Survey Response").track_changes)
        self.assertTrue(frappe.get_meta("RG Member").track_changes)
        doc = self._survey()
        original_publisher, original_owner = doc.publisher, doc.owner
        doc.title = self.prefix + "公开标题"
        doc.questions[0].title = "公开问题"
        doc.save()
        publish_survey(doc.name)
        self.assertEqual(frappe.db.count("Version", {"ref_doctype": "RG Survey", "docname": doc.name}), 0)
        frappe.set_user(self.users["one"])
        response = submit_response(doc.name, {doc.questions[0].name: "当前实名答复"})
        records = [("RG Survey", doc.name), ("RG Survey Response", response["name"])]
        marker = "UNPUBLISHED-" + self.prefix
        versions = [self._legacy_version(doctype, name, marker) for doctype, name in records]
        for key in ("one", "secretary"):
            frappe.set_user(self.users[key])
            for doctype, name in records:
                for load in (lambda: getdoc(doctype, name),
                             lambda: get_docinfo(doctype=doctype, name=name)):
                    frappe.response["docs"] = []
                    frappe.response.pop("docinfo", None)
                    load()
                    self.assertFalse(frappe.response["docinfo"].get("versions"))
                    self.assertNotIn(marker, frappe.as_json({
                        "docs": frappe.response.get("docs"), "docinfo": frappe.response["docinfo"],
                    }))
        frappe.set_user(self.users["secretary"])
        close_survey(doc.name)
        doc.reload()
        self.assertEqual(doc.publisher, original_publisher)
        self.assertEqual(doc.owner, original_owner)
        self.assertEqual(doc.modified_by, self.users["secretary"])
        self.assertTrue(doc.published_at)
        self.assertTrue(doc.closed_at)
        self.assertTrue(all(frappe.db.exists("Version", version.name) for version in versions))

    def test_native_direct_and_list_version_policy_still_denies_business_roles(self):
        doc = self._survey(published=True)
        frappe.set_user(self.users["one"])
        response = submit_response(doc.name, {doc.questions[0].name: "当前答卷"})
        versions = [self._legacy_version("RG Survey", doc.name, "旧草稿"),
                    self._legacy_version("RG Survey Response", response["name"], "旧答卷")]
        unrelated = self._legacy_version("RG Member", self.users["one"], "无关成员历史")
        # Native Version has System Manager permissions, not business-role
        # grants. Keep that policy intact instead of adding a new grant.
        for key in ("one", "teacher", "secretary"):
            frappe.set_user(self.users[key])
            for version in (*versions, unrelated):
                with self.assertRaises(frappe.PermissionError):
                    api_get("Version", version.name)
                with self.assertRaises(frappe.PermissionError):
                    frappe.get_list("Version", filters={"name": version.name}, fields=["name", "data"])
        frappe.set_user("Administrator")
        for version in (*versions, unrelated):
            self.assertEqual(api_get("Version", version.name)["name"], version.name)
            self.assertEqual(frappe.get_list("Version", filters={"name": version.name}, pluck="name"), [version.name])

    def test_survey_file_json_insertion_rejects_valid_content_before_any_write(self):
        parents = self._parents()
        self.assertFalse(set(SURVEY_FILE_DOCTYPES).intersection(MANAGED))
        for index, (doctype, name) in enumerate(parents):
            for user in (self.users["secretary"], "Administrator"):
                frappe.set_user(user)
                for private in (0, 1):
                    filename = self.prefix + f"insert-{index}-{private}.pdf"
                    content = core.pdf_bytes(filename)
                    count = frappe.db.count("File")
                    paths = [Path(frappe.get_site_path(directory, "files", filename))
                             for directory in ("private", "public")]
                    self.assertTrue(all(not path.exists() for path in paths))
                    with patch.object(ResearchGroupFile, "write_file",
                                      side_effect=AssertionError("survey attachment reached physical write")) as write:
                        with self.assertRaises(frappe.PermissionError):
                            frappe.get_doc({"doctype": "File", "file_name": filename, "content": content,
                                "is_private": private, "attached_to_doctype": doctype,
                                "attached_to_name": name}).insert(ignore_permissions=True)
                        write.assert_not_called()
                    self.assertEqual(frappe.db.count("File"), count)
                    self.assertTrue(all(not path.exists() for path in paths))

    def test_generic_file_cannot_be_reparented_to_a_survey_before_privacy_move(self):
        parents = self._parents()
        file = self._native_file("reparent")
        path = Path(file.get_full_path())
        content, count = path.read_bytes(), frappe.db.count("File")
        public_path = Path(frappe.get_site_path("public", "files", path.name))
        self.assertFalse(public_path.exists())
        for doctype, name in parents:
            frappe.set_user(self.users["secretary"])
            candidate = frappe.get_doc("File", file.name)
            candidate.attached_to_doctype, candidate.attached_to_name = doctype, name
            candidate.is_private = 0
            with self.assertRaises(frappe.PermissionError):
                candidate.save(ignore_permissions=True)
            saved = frappe.db.get_value("File", file.name, ["attached_to_doctype", "attached_to_name", "is_private"], as_dict=True)
            self.assertFalse(saved.attached_to_doctype)
            self.assertFalse(saved.attached_to_name)
            self.assertEqual(saved.is_private, 1)
            self.assertEqual(path.read_bytes(), content)
            self.assertFalse(public_path.exists())
            self.assertEqual(frappe.db.count("File"), count)

    def test_legacy_survey_files_are_hidden_from_business_reads_lists_and_downloads(self):
        from frappe.core.doctype.file.utils import find_file_by_url

        parents = self._parents()
        original = self._native_file("legacy")
        content = Path(original.get_full_path()).read_bytes()
        legacy = [self._legacy_file(original, doctype, name) for doctype, name in parents]
        published = frappe.db.get_value("RG Survey Response", parents[-1][1], "survey")
        published_file = self._legacy_file(original, "RG Survey", published)
        legacy.append(published_file)
        unrelated = self._legacy_file(original, "User", self.users["secretary"])
        frappe.get_doc({"doctype": "Comment", "comment_type": "Attachment",
            "reference_doctype": "RG Survey", "reference_name": published,
            "owner": self.users["secretary"], "comment_email": self.users["secretary"],
            "content": f'<a href="{original.file_url}">{original.file_name}</a>',
        }).db_insert()
        for key in ("one", "teacher", "secretary"):
            frappe.set_user(self.users[key])
            for file in legacy:
                self.assertFalse(file.has_permission("read"))
                self.assertFalse(file.is_downloadable())
                self.assertIsNone(find_file_by_url(file.file_url, file.name))
                self.assertFalse(frappe.get_list("File", filters={"name": file.name}, fields=["name", "file_url"]))
                for read in (lambda: api_get("File", file.name), file.get_content,
                             lambda: read_stored_file_bytes(file), lambda: zip_files([file.name])):
                    frappe.response.pop("filecontent", None)
                    with self.assertRaises(frappe.PermissionError):
                        read()
                    self.assertNotIn("filecontent", frappe.response)
                with self.assertRaises(frappe.PermissionError):
                    frappe.delete_doc("File", file.name, ignore_permissions=True, force=True)
        commands = {
            "frappe.desk.form.load.getdoc": "research_group.files.load_document",
            "frappe.desk.form.load.get_docinfo": "research_group.files.load_document_info",
            "frappe.desk.form.load.get_filtered_attachments": "research_group.files.filtered_attachments",
        }
        for native, expected in commands.items():
            self.assertEqual(frappe.override_whitelisted_method(native), expected)
        load = frappe.get_attr(commands["frappe.desk.form.load.getdoc"])
        info = frappe.get_attr(commands["frappe.desk.form.load.get_docinfo"])
        filtered = frappe.get_attr(commands["frappe.desk.form.load.get_filtered_attachments"])
        visible_parents = [
            ("secretary", "RG Survey", parents[0][1]),
            ("secretary", "RG Survey", published),
            ("teacher", "RG Survey", published),
            ("one", "RG Survey", published),
            ("secretary", "RG Survey Response", parents[-1][1]),
            ("one", "RG Survey Response", parents[-1][1]),
        ]
        for key, doctype, name in visible_parents:
            frappe.set_user(self.users[key])
            for loader in (load, info):
                frappe.response["docs"] = []
                frappe.response.pop("docinfo", None)
                loader(doctype=doctype, name=name)
                docinfo = frappe.response["docinfo"]
                self.assertEqual(docinfo.attachments, [])
                self.assertEqual(docinfo.attachment_logs, [])
                self.assertNotIn(original.file_url, frappe.as_json(docinfo))
            self.assertEqual(filtered(doctype, name, "[]"), [])
        # Existing unrelated owner access and Administrator maintenance remain.
        frappe.set_user(self.users["secretary"])
        self.assertEqual(api_get("File", original.name)["name"], original.name)
        info(doctype="User", name=self.users["secretary"])
        self.assertIn(unrelated.name, [row.name for row in frappe.response["docinfo"].attachments])
        self.assertEqual([row.name for row in filtered("User", self.users["secretary"], "[]")], [unrelated.name])
        frappe.set_user("Administrator")
        info(doctype="RG Survey", name=published)
        self.assertIn(published_file.name, [row.name for row in frappe.response["docinfo"].attachments])
        self.assertEqual([row.name for row in filtered("RG Survey", published, "[]")], [published_file.name])
        for file in legacy:
            self.assertEqual(api_get("File", file.name)["name"], file.name)
            self.assertEqual(read_stored_file_bytes(file), content)
            candidate = frappe.get_doc("File", file.name)
            candidate.attached_to_doctype = candidate.attached_to_name = None
            with self.assertRaises(frappe.PermissionError):
                candidate.save(ignore_permissions=True)
        self.assertEqual(Path(original.get_full_path()).read_bytes(), content)
