"""Real Frappe/DB tests. Concurrent controls use independent OS processes."""
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, nowdate
from pypdf import PdfWriter

from research_group.files import save_managed_file
from research_group.policy import (
    APPROVED, ARCHIVED, CANCELLED, PENDING, REJECTED, SECRETARY, STUDENT, TEACHER, secure_get,
)
from research_group.services import (
    archive_discussion, assign_reporter, cancel_appointment, decide_appointment,
    material_history, publish_meeting, submit_material, update_teacher_feedback,
    upload_discussion_material,
)


def pdf_bytes(label="test"):
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": label})
    writer.write(stream)
    return stream.getvalue()


def create_members(prefix):
    """Shared, explicitly fictional member/combined-role fixture."""
    users = {key: f"{prefix}-{key}@example.invalid"
             for key in ("teacher", "other_teacher", "one", "two", "secretary")}
    assigned = {"teacher": [TEACHER], "other_teacher": [TEACHER], "one": [STUDENT],
                "two": [STUDENT], "secretary": [SECRETARY, STUDENT]}
    for key, email in users.items():
        frappe.get_doc({"doctype": "User", "email": email, "first_name": "虚构测试" + key,
            "user_type": "System User", "language": "zh", "enabled": 1, "send_welcome_email": 0,
            "roles": [{"role": role} for role in assigned[key]]}).insert(ignore_permissions=True)
        frappe.get_doc({"doctype": "RG Member", "user": email, "display_name": "虚构测试" + key,
            "active": 1, "is_demo": 1}).insert(ignore_permissions=True)
    return users


class TestResearchGroupCore(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-test-" + frappe.generate_hash(length=10)
        self.users = create_members(self.prefix)
        self.topics = {}
        for key in ("one", "two", "secretary"):
            self.topics[key] = frappe.get_doc({"doctype": "RG Topic", "student": self.users[key],
                "title": self.prefix + key, "direction": "虚构研究方向", "priority": "中",
                "next_milestone": "初始里程碑", "status": "进行中"}).insert(ignore_permissions=True).name
        self.committed_fixture = False

    def tearDown(self):
        frappe.set_user("Administrator")
        if self.committed_fixture:
            self._delete_fixture()
            frappe.db.commit()
        else:
            frappe.db.rollback()
        frappe.clear_cache()
        super().tearDown()

    def appointment(self, key="one", start="2030-01-02 14:00:00", end="2030-01-02 15:00:00"):
        frappe.set_user(self.users[key])
        return frappe.get_doc({"doctype": "RG Appointment", "topic": self.topics[key],
            "teacher": self.users["teacher"], "start_at": start, "end_at": end, "agenda": "虚构讨论议题"}).insert()

    def meeting(self, publish=True):
        frappe.set_user(self.users["secretary"])
        meeting = frappe.get_doc({"doctype": "RG Group Meeting", "title": self.prefix + "组会",
            "start_at": "2030-01-03 14:00:00", "end_at": "2030-01-03 16:00:00", "location": "虚构会议室"}).insert()
        report = assign_reporter(meeting.name, self.users["one"])
        if publish:
            publish_meeting(meeting.name)
        return meeting, report

    def test_private_rows_rest_lists_and_combined_roles(self):
        from frappe.client import get as api_get
        appointment = self.appointment()
        frappe.set_user(self.users["secretary"])
        for dt, name in (("RG Topic", self.topics["one"]), ("RG Appointment", appointment.name)):
            self.assertFalse(frappe.has_permission(dt, "read", doc=name))
            self.assertFalse(frappe.get_list(dt, filters={"name": name}, pluck="name"))
            with self.assertRaises(frappe.PermissionError):
                api_get(dt, name)
        visible = set(frappe.get_list("RG Topic", pluck="name"))
        self.assertIn(self.topics["secretary"], visible)
        self.assertNotIn(self.topics["two"], visible)
        from frappe.permissions import get_doc_permissions
        own_topic = frappe.get_doc("RG Topic", self.topics["secretary"])
        self.assertTrue(get_doc_permissions(own_topic).get("read"))
        frappe.set_user(self.users["teacher"])
        self.assertTrue(frappe.has_permission("RG Appointment", "read", doc=appointment.name))

    def test_progress_append_only_and_server_identity(self):
        frappe.set_user(self.users["one"])
        for i in range(2):
            p = frappe.get_doc({"doctype": "RG Progress", "topic": self.topics["one"],
                "student": self.users["two"], "recorded_at": "1990-01-01 00:00:00",
                "progress_date": nowdate(), "summary": f"本次进展{i}", "next_milestone": "下一步"}).insert()
            self.assertEqual(p.student, self.users["one"])
            self.assertNotEqual(str(p.recorded_at), "1990-01-01 00:00:00")
        self.assertEqual(frappe.db.count("RG Progress", {"topic": self.topics["one"]}), 2)
        p.summary = "覆盖"
        with self.assertRaises(frappe.PermissionError):
            p.save()
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc({"doctype": "RG Progress", "topic": self.topics["two"], "summary": "越权",
                "progress_date": nowdate(), "next_milestone": "越权"}).insert()

    def test_state_transitions_direct_crud_and_teacher_feedback(self):
        a = self.appointment()
        with self.assertRaises(frappe.PermissionError):
            decide_appointment(a.name, APPROVED)
        a.status = APPROVED
        a.flags.rg_service = True
        with self.assertRaises(frappe.ValidationError):
            a.save()
        frappe.set_user(self.users["other_teacher"])
        with self.assertRaises(frappe.PermissionError):
            decide_appointment(a.name, APPROVED)
        frappe.set_user(self.users["teacher"])
        decide_appointment(a.name, APPROVED, "已确认")
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.PermissionError):
            archive_discussion(a.name, "结论", "行动", "伪造教师意见")
        record = archive_discussion(a.name, "已验证的结论", "补充收敛测试")
        self.assertEqual(frappe.db.get_value("RG Appointment", a.name, "status"), ARCHIVED)
        with self.assertRaises(frappe.ValidationError):
            cancel_appointment(a.name)
        frappe.set_user(self.users["teacher"])
        update_teacher_feedback(record.name, "教师复核意见")
        self.assertEqual(frappe.db.get_value("RG Discussion Record", record.name, "teacher_feedback"), "教师复核意见")

    def test_new_progress_has_doctype_and_field_write_access(self):
        frappe.set_user(self.users["one"])
        self.assertTrue(frappe.has_permission("RG Progress", "create"))
        self.assertTrue(frappe.has_permission("RG Progress", "write"))
        doc = frappe.get_doc({"doctype": "RG Progress", "topic": self.topics["one"],
            "student": self.users["one"], "progress_date": nowdate(), "summary": "新表单输入",
            "blockers": "待核查", "next_milestone": "后续控制"})
        self.assertTrue(doc.has_permission("write"))
        writable = set(doc.meta.get_permitted_fieldnames(permission_type="write"))
        for field in ("topic", "progress_date", "summary", "blockers", "next_milestone"):
            self.assertIn(field, writable)
            self.assertTrue(doc.has_permlevel_access_to(field, permission_type="write"))
            self.assertFalse(doc.meta.get_field(field).read_only)
        doc.insert()
        self.assertFalse(doc.has_permission("write"))
        doc.set("__islocal", 1)
        self.assertFalse(doc.has_permission("write"))
        doc.set("__islocal", 0)
        doc.summary = "覆盖历史"
        with self.assertRaises(frappe.PermissionError):
            doc.save()

    def test_cancelled_and_rejected_cannot_archive(self):
        for decision in (CANCELLED, REJECTED):
            a = self.appointment()
            if decision == CANCELLED:
                cancel_appointment(a.name)
            else:
                frappe.set_user(self.users["teacher"])
                decide_appointment(a.name, REJECTED)
            with self.assertRaises(frappe.ValidationError):
                archive_discussion(a.name, "结论", "行动")
            self.assertFalse(frappe.db.exists("RG Discussion Record", {"appointment": a.name}))

    def test_overlap_and_adjacent_time(self):
        a = self.appointment()
        overlap = self.appointment("two", "2030-01-02 14:30:00", "2030-01-02 15:30:00")
        adjacent = self.appointment("two", "2030-01-02 15:00:00", "2030-01-02 16:00:00")
        frappe.set_user(self.users["teacher"])
        decide_appointment(a.name, APPROVED)
        with self.assertRaises(frappe.ValidationError):
            decide_appointment(overlap.name, APPROVED)
        self.assertEqual(frappe.db.get_value("RG Appointment", overlap.name, "status"), PENDING)
        decide_appointment(adjacent.name, APPROVED)

    def test_private_file_list_download_and_mutation(self):
        from frappe.core.doctype.file.utils import find_file_by_url
        a = self.appointment()
        file = save_managed_file("private.pdf", pdf_bytes(), "RG Appointment", a.name)
        original_path = Path(file.get_full_path())
        self.assertTrue(file.is_private)
        # Generic File upload is not an alternate path around the controlled service.
        with self.assertRaises(frappe.PermissionError):
            forged = frappe.get_doc({"doctype": "File", "file_name": "bypass.pdf", "content": pdf_bytes("bypass"),
                "is_private": 1, "attached_to_doctype": "RG Appointment", "attached_to_name": a.name})
            forged.flags.rg_file = True
            forged.insert(ignore_permissions=True)
        for key in ("two", "secretary"):
            frappe.set_user(self.users[key])
            self.assertFalse(frappe.has_permission("File", "read", doc=file.name))
            self.assertFalse(frappe.get_list("File", filters={"name": file.name}, fields=["name", "file_url"]))
            self.assertIsNone(find_file_by_url(file.file_url, file.name))
        frappe.set_user(self.users["one"])
        self.assertIsNotNone(find_file_by_url(file.file_url, file.name))
        for changes in ({"is_private": 0}, {"attached_to_name": "other"}, {"attached_to_doctype": "User"}):
            candidate = frappe.get_doc("File", file.name)
            candidate.update(changes)
            with self.assertRaises(frappe.PermissionError):
                candidate.save(ignore_permissions=True)
            self.assertTrue(original_path.exists())
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc("File", file.name)
        frappe.set_user(self.users["teacher"])
        self.assertIsNotNone(find_file_by_url(file.file_url, file.name))
        frappe.set_user("Guest")
        self.assertIsNone(find_file_by_url(file.file_url, file.name))

    def test_material_validation_and_failed_submission_state(self):
        meeting, report = self.meeting()
        frappe.set_user(self.users["two"])
        with self.assertRaises(frappe.PermissionError):
            submit_material(report.name, "伪造报告", "valid.pdf", pdf_bytes())
        frappe.set_user(self.users["one"])
        invalid = [("fake.pdf", b"%PDF-fake\n%%EOF"), ("fake.ppt", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1junk"),
                   ("fake.pptx", b"PKjunk"), ("bad.exe", pdf_bytes())]
        for filename, content in invalid:
            with self.subTest(filename=filename), self.assertRaises(frappe.ValidationError):
                submit_material(report.name, "报告主题", filename, content)
        with patch.dict(frappe.conf, {"rg_max_file_size": 16}):
            with self.assertRaises(frappe.ValidationError):
                submit_material(report.name, "超限", "big.pdf", pdf_bytes())
        self.assertEqual(frappe.db.count("RG Material Version", {"report": report.name}), 0)
        self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "latest_version"), 0)

    def test_private_material_endpoint_rejects_unrelated_teacher_and_accepts_designated_teacher(self):
        appointment = self.appointment()
        frappe.set_user(self.users["teacher"])
        decide_appointment(appointment.name, APPROVED)
        record = archive_discussion(appointment.name, "虚构端点权限结论", "虚构后续行动")
        content = pdf_bytes(self.prefix + "private-endpoint")
        for doctype, name in (("RG Appointment", appointment.name), ("RG Discussion Record", record.name)):
            with self.subTest(doctype=doctype):
                before = frappe.db.count("File")
                frappe.set_user(self.users["other_teacher"])
                with patch("research_group.services._upload", return_value=("endpoint-private.pdf", content)) as upload:
                    with self.assertRaises(frappe.PermissionError):
                        upload_discussion_material(doctype, name)
                    upload.assert_not_called()
                self.assertEqual(frappe.db.count("File"), before)
                frappe.set_user(self.users["teacher"])
                with patch("research_group.services._upload", return_value=("endpoint-private.pdf", content)):
                    result = upload_discussion_material(doctype, name)
                file = frappe.get_doc("File", result["name"])
                self.assertEqual((file.attached_to_doctype, file.attached_to_name), (doctype, name))
                self.assertTrue(file.is_private)
                self.assertEqual(Path(file.get_full_path()).read_bytes(), content)

    def test_generic_same_content_public_upload_is_rejected_before_native_write(self):
        appointment = self.appointment()
        content = pdf_bytes(self.prefix + "pre-write-guard")
        managed = save_managed_file("guard-private.pdf", content, "RG Appointment", appointment.name)
        frappe.set_user("Administrator")  # Isolate the guard from native public-upload restrictions.
        for encoded in (False, True):
            payload = base64.b64encode(content).decode("ascii") if encoded else content
            generic = frappe.get_doc({"doctype": "File", "file_name": "guard-public.pdf",
                "content": payload, "decode": encoded, "is_private": 0, "content_hash": "0" * 32})
            with patch("frappe.core.doctype.file.file.File.write_file", side_effect=AssertionError("must reject before write")) as write:
                with self.assertRaises(frappe.PermissionError):
                    generic.insert(ignore_permissions=True)
                write.assert_not_called()
        self.assertEqual(Path(managed.get_full_path()).read_bytes(), content)

    def test_pptx_structure_is_validated_beyond_zip_and_content_type(self):
        from research_group.files import validate_material
        types = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                 '</Types>')
        for content in ("not XML", "<html/>", '<!DOCTYPE p [<!ENTITY secret "x">]><p/>'):
            output = BytesIO()
            with ZipFile(output, "w") as z:
                z.writestr("[Content_Types].xml", types)
                z.writestr("ppt/presentation.xml", content)
            with self.subTest(content=content), self.assertRaises(frappe.ValidationError):
                validate_material("fake.pptx", output.getvalue())
        output = BytesIO()
        with ZipFile(output, "w") as z:
            z.writestr("[Content_Types].xml", types)
            z.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>')
        # Two XML parts lack the OPC entry relationship and cannot stand in
        # for a real PowerPoint package.
        with self.assertRaises(frappe.ValidationError):
            validate_material("shell.pptx", output.getvalue())

    def test_topic_200_character_storage_boundary(self):
        meeting, report = self.meeting()
        frappe.set_user(self.users["one"])
        for length in (140, 141, 200):
            topic = "题" * length
            result = submit_material(report.name, topic, "topic.pdf", pdf_bytes(str(length)))
            self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "topic"), topic)
            self.assertEqual(frappe.db.get_value("RG Material Version", result["name"], "report_topic"), topic)
        with self.assertRaises(frappe.ValidationError):
            submit_material(report.name, "题" * 201, "topic.pdf", pdf_bytes("too long"))
        self.assertEqual(frappe.db.count("RG Material Version", {"report": report.name}), 3)

    def test_existing_meeting_rejects_empty_deadline_and_new_report_rejects_late(self):
        meeting, report = self.meeting(publish=False)
        expected = frappe.db.get_value("RG Group Meeting", meeting.name, "deadline")
        meeting.deadline = None
        with self.assertRaises(frappe.ValidationError):
            meeting.save()
        self.assertEqual(frappe.db.get_value("RG Group Meeting", meeting.name, "deadline"), expected)
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc({"doctype": "RG Meeting Report", "meeting": meeting.name,
                "student": self.users["two"], "late": 1}).insert()
        self.assertFalse(frappe.db.exists("RG Meeting Report", {"meeting": meeting.name, "student": self.users["two"]}))

    def test_filename_boundary_survives_native_collision_suffix(self):
        meeting, report = self.meeting()
        frappe.set_user(self.users["one"])
        for filename in ("a" * 130 + ".pdf", "中" * 81 + "aa.pdf"):
            for number in (1, 2):
                result = submit_material(report.name, "名称边界", filename,
                    pdf_bytes(self.prefix + filename[:1] + str(number)))
                version = frappe.get_doc("RG Material Version", result["name"])
                file = frappe.get_doc("File", version.material_file)
                self.assertEqual(version.original_filename, filename)
                self.assertLessEqual(len(file.file_name), 140)
                self.assertLessEqual(len(Path(file.get_full_path()).name.encode("utf-8")), 255)
                self.assertTrue(Path(file.get_full_path()).is_file())
        for filename in ("a" * 131 + ".pdf", "中" * 82 + ".pdf"):
            with self.assertRaises(frappe.ValidationError):
                submit_material(report.name, "名称超限", filename, pdf_bytes("rejected"))
        self.assertEqual(frappe.db.count("RG Material Version", {"report": report.name}), 4)

    def test_reupload_history_deadline_public_scope_and_integrity(self):
        from research_group.audit import material_integrity
        meeting, report = self.meeting()
        self.assertEqual(str(meeting.deadline), "2030-01-02 23:59:00")
        deadline = get_datetime(meeting.deadline)
        frappe.set_user(self.users["one"])
        with patch("research_group.services.now_datetime", return_value=deadline - timedelta(minutes=1)):
            first = submit_material(report.name, "首版主题", "first.pdf", pdf_bytes("first"))
        with patch("research_group.services.now_datetime", return_value=deadline + timedelta(minutes=1)):
            second = submit_material(report.name, "修订主题", "second.pdf", pdf_bytes("second"))
        self.assertEqual([first["version"], second["version"]], [1, 2])
        self.assertEqual([first["late"], second["late"]], [0, 1])
        frappe.set_user(self.users["two"])
        history = material_history(report.name)
        self.assertEqual([r.original_filename for r in history], ["second.pdf", "first.pdf"])
        self.assertEqual([r.late for r in history], [1, 0])
        from frappe.core.doctype.file.utils import find_file_by_url
        for row in history:
            file = frappe.get_doc("File", row.material_file)
            self.assertIsNotNone(find_file_by_url(file.file_url, file.name))
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            material_history(report.name)
        frappe.set_user("Administrator")
        self.assertTrue(material_integrity()["ok"])

    def test_failed_material_savepoint_compensates_disk_before_outer_commit(self):
        from research_group.files import ResearchGroupFile
        from research_group.services import save_internal as original_save

        meeting, report = self.meeting()
        frappe.set_user(self.users["one"])
        original_write = ResearchGroupFile.write_file
        for stage in ("write", "after_insert", "report_save"):
            paths, urls = [], []

            def watched_write(file):
                result = original_write(file)
                paths.append(Path(file.get_full_path()))
                urls.append(file.file_url)
                if stage == "write":
                    raise frappe.ValidationError("落盘后故障注入")
                return result

            def after_insert(file):
                if stage == "after_insert":
                    raise frappe.ValidationError("File插入后故障注入")

            def save(doc, new=False):
                if stage == "report_save" and doc.doctype == "RG Meeting Report":
                    raise frappe.ValidationError("报告更新故障注入")
                return original_save(doc, new=new)

            with self.subTest(stage=stage), patch.object(ResearchGroupFile, "write_file", watched_write), \
                    patch.object(ResearchGroupFile, "after_insert", after_insert), \
                    patch("research_group.services.save_internal", side_effect=save):
                with self.assertRaises(frappe.ValidationError):
                    submit_material(report.name, "故障回归", "fault.pdf", pdf_bytes(self.prefix + stage))
            # An internal caller deliberately catches and commits, so native
            # after_rollback callbacks will never clean these bytes.
            self.committed_fixture = True
            frappe.db.commit()
            self.assertTrue(paths)
            self.assertTrue(all(not path.exists() for path in paths), paths)
            self.assertFalse(any(frappe.db.exists("File", {"file_url": url}) for url in urls))
            self.assertEqual(frappe.db.count("RG Material Version", {"report": report.name}), 0)
            self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "latest_version"), 0)

    def test_failed_deduplicated_upload_keeps_shared_file(self):
        from research_group.files import ResearchGroupFile

        meeting, report = self.meeting()
        frappe.set_user(self.users["one"])
        content = pdf_bytes(self.prefix + "shared")
        first = submit_material(report.name, "保留原版", "shared.pdf", content)
        version = frappe.get_doc("RG Material Version", first["name"])
        file = frappe.get_doc("File", version.material_file)
        path = Path(file.get_full_path())
        with patch.object(ResearchGroupFile, "after_insert", side_effect=frappe.ValidationError("去重后故障")):
            with self.assertRaises(frappe.ValidationError):
                submit_material(report.name, "失败重传", "shared-again.pdf", content)
        self.committed_fixture = True
        frappe.db.commit()
        self.assertEqual(path.read_bytes(), content)
        self.assertTrue(frappe.db.exists("File", file.name))
        self.assertEqual(frappe.db.count("File", {"file_url": file.file_url}), 1)
        self.assertEqual(frappe.db.count("RG Material Version", {"report": report.name}), 1)
        self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "latest_version"), 1)

    def test_corrupt_existing_blob_is_not_reused_or_removed_on_rejection(self):
        meeting, report = self.meeting()
        frappe.set_user(self.users["one"])
        content = pdf_bytes(self.prefix + "corrupt-dedup")
        first = submit_material(report.name, "保留首版", "dedup-original.pdf", content)
        version = frappe.get_doc("RG Material Version", first["name"])
        file = frappe.get_doc("File", version.material_file)
        path = Path(file.get_full_path())
        before = frappe.db.count("File")
        corrupt = content.replace(b"%PDF-", b"%BAD-", 1)
        self.assertNotEqual(corrupt, content)
        path.write_bytes(corrupt)
        try:
            with self.assertRaises(frappe.ValidationError):
                submit_material(report.name, "不接受损坏复用", "dedup-again.pdf", content)
            self.committed_fixture = True
            frappe.db.commit()  # The savepoint must compensate even if the caller commits.
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), corrupt)
            self.assertTrue(frappe.db.exists("File", file.name))
            self.assertEqual(frappe.db.count("File"), before)
            self.assertEqual(frappe.db.count("RG Material Version", {"report": report.name}), 1)
            self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "latest_version"), 1)
            self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "topic"), "保留首版")
        finally:
            path.write_bytes(content)

    def test_inactive_members_lose_records_lists_files_and_actions(self):
        from frappe.client import get as api_get
        from frappe.core.doctype.file.utils import find_file_by_url
        from research_group.reports import teacher_overview

        appointment = self.appointment()
        private = save_managed_file("inactive-private.pdf", pdf_bytes(self.prefix + "private"),
            "RG Appointment", appointment.name)
        meeting, report = self.meeting()
        draft, unused_report = self.meeting(publish=False)
        frappe.set_user(self.users["one"])
        submitted = submit_material(report.name, "公开材料", "inactive-public.pdf", pdf_bytes(self.prefix + "public"))
        public = frappe.get_doc("File", frappe.db.get_value("RG Material Version", submitted["name"], "material_file"))
        records = (("RG Topic", self.topics["one"]), ("RG Appointment", appointment.name),
                   ("RG Group Meeting", meeting.name), ("RG Meeting Report", report.name),
                   ("RG Material Version", submitted["name"]))
        for key, role in (("one", STUDENT), ("teacher", TEACHER), ("secretary", SECRETARY)):
            user = self.users[key]
            frappe.set_user("Administrator")
            frappe.db.set_value("RG Member", user, "active", 0)
            frappe.set_user(user)
            self.assertIn(role, frappe.get_roles())  # Native role remains enabled.
            for dt, name in records:
                self.assertFalse(frappe.has_permission(dt, "read", doc=name), (key, dt))
                self.assertFalse(frappe.get_list(dt, filters={"name": name}, pluck="name"), (key, dt))
                with self.assertRaises(frappe.PermissionError):
                    api_get(dt, name)
            for file in (private, public):
                self.assertFalse(frappe.has_permission("File", "read", doc=file.name))
                self.assertFalse(frappe.get_list("File", filters={"name": file.name}, pluck="name"))
                self.assertIsNone(find_file_by_url(file.file_url, file.name))
            with self.assertRaises(frappe.PermissionError):
                material_history(report.name)
            action = {
                "one": lambda: cancel_appointment(appointment.name),
                "teacher": lambda: decide_appointment(appointment.name, APPROVED),
                "secretary": lambda: publish_meeting(draft.name),
            }[key]
            with self.assertRaises(frappe.PermissionError):
                action()
            with self.assertRaises(frappe.PermissionError):
                teacher_overview()
            frappe.set_user("Administrator")
            frappe.db.set_value("RG Member", user, "active", 1)

    def test_draft_meeting_and_report_permissions(self):
        meeting, report = self.meeting(publish=False)
        frappe.set_user(self.users["two"])
        self.assertFalse(frappe.has_permission("RG Group Meeting", "read", doc=meeting.name))
        self.assertFalse(frappe.get_list("RG Meeting Report", filters={"name": report.name}))
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.PermissionError):
            submit_material(report.name, "提前提交", "valid.pdf", pdf_bytes())
        frappe.set_user(self.users["secretary"])
        publish_meeting(meeting.name)
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.PermissionError):
            assign_reporter(meeting.name, self.users["two"])

    def test_integrity_and_zip_preserve_original_file_bytes(self):
        from research_group.audit import material_integrity
        from research_group.files import zip_files

        meeting, report = self.meeting()
        binary_pdf = pdf_bytes(self.prefix + "binary")
        utf8_pdf = pdf_bytes(self.prefix + "utf8").replace(b"%\xe2\xe3\xcf\xd3", b"%UTF8", 1)
        # Equal-length header replacement preserves the PDF's xref offsets.
        self.assertIn(b"%UTF8", utf8_pdf)
        utf8_pdf.decode("utf-8")
        with self.assertRaises(UnicodeDecodeError):
            binary_pdf.decode("utf-8")
        originals = (utf8_pdf, binary_pdf)
        files = []
        versions = []
        frappe.set_user(self.users["one"])
        for number, content in enumerate(originals, 1):
            submitted = submit_material(report.name, "原字节回归", f"raw{number}.pdf", content)
            version = frappe.get_doc("RG Material Version", submitted["name"])
            file = frappe.get_doc("File", version.material_file)
            self.assertEqual(Path(file.get_full_path()).read_bytes(), content)
            self.assertEqual(version.size_bytes, len(content))
            self.assertEqual(version.sha256, hashlib.sha256(content).hexdigest())
            files.append(file)
            versions.append(version)
        # UTF-8 and legacy-decodable binary PDFs must both survive ZIP unchanged.
        with patch.dict(frappe.response, {}, clear=True):
            zip_files([file.name for file in files])
            with ZipFile(BytesIO(frappe.response["filecontent"])) as archive:
                self.assertEqual(archive.namelist(), [file.name + "_" + file.file_name for file in files])
                for file, original in zip(files, originals):
                    self.assertEqual(archive.read(file.name + "_" + file.file_name), original)
        frappe.set_user("Administrator")
        self.assertTrue(material_integrity()["ok"])
        # Same-size disk corruption must fail the stored hash, not use cached text.
        path = Path(files[0].get_full_path())
        path.write_bytes(utf8_pdf.replace(b"%UTF8", b"%EDIT", 1))
        try:
            audit = material_integrity()
            self.assertFalse(audit["ok"])
            self.assertIn({"version": versions[0].name,
                           "error": "hash_size_parent_or_privacy_mismatch"}, audit["errors"])
        finally:
            path.write_bytes(utf8_pdf)

    def test_teacher_overview_and_student_timeline(self):
        from research_group.reports import teacher_overview, student_timeline
        frappe.set_user(self.users["one"])
        for i in range(2):
            frappe.get_doc({"doctype": "RG Progress", "topic": self.topics["one"], "progress_date": nowdate(),
                "summary": f"进展{i}", "blockers": f"阻碍{i}", "next_milestone": f"里程碑{i}"}).insert()
        a = self.appointment()
        frappe.set_user(self.users["teacher"])
        decide_appointment(a.name, APPROVED)
        archive_discussion(a.name, "最近结论", "后续行动", "教师意见")
        columns, rows = teacher_overview()
        own = next(r for r in rows if r["topic"] == self.topics["one"])
        self.assertEqual(own["blockers"], "阻碍1")
        self.assertEqual(own["next_milestone"], "里程碑1")
        self.assertEqual(own["conclusion"], "最近结论")
        frappe.set_user(self.users["secretary"])
        self.assertFalse(any(r["student"] == self.users["one"] for r in student_timeline()[1]))
        with self.assertRaises(frappe.PermissionError):
            teacher_overview()

    def test_zip_rejects_private_and_mixed_requests_without_response_bytes(self):
        from research_group.files import zip_files

        appointment = self.appointment()
        private = save_managed_file("zip-private.pdf", pdf_bytes(self.prefix + "zip-private"),
            "RG Appointment", appointment.name)
        meeting, report = self.meeting()
        frappe.set_user(self.users["one"])
        version = submit_material(report.name, "可读材料", "zip-public.pdf", pdf_bytes(self.prefix + "zip-public"))
        public = frappe.db.get_value("RG Material Version", version["name"], "material_file")
        for user in (self.users["two"], self.users["secretary"]):
            frappe.set_user(user)
            for files in ([private.name], [public, private.name], [private.name, public]):
                with patch.dict(frappe.response, {}, clear=True):
                    with self.assertRaises(frappe.PermissionError):
                        zip_files(files)
                    self.assertNotIn("filecontent", frappe.response)

    def _run_race(self, jobs):
        """Workers preload a REPEATABLE READ snapshot before the shared release."""
        frappe.db.commit()
        self.committed_fixture = True
        parent = Path(frappe.get_site_path("private", "rg-test-concurrency"))
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=parent) as temp:
            root = Path(temp).resolve()
            processes = []
            script = "from research_group.tests.test_core import race_worker; race_worker()"
            try:
                for i, job in enumerate(jobs):
                    argv = [sys.executable, "-c", script, frappe.local.site,
                            str(Path(frappe.local.sites_path).resolve()), str(root), str(i), json.dumps(job)]
                    processes.append(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                deadline = time.monotonic() + 30
                while not all((root / f"{i}.ready").exists() for i in range(len(jobs))):
                    if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                        raise AssertionError("independent DB workers failed to reach the snapshot barrier")
                    time.sleep(0.05)
                (root / "start").write_text("start", encoding="ascii")
                results = []
                for p in processes:
                    out, err = p.communicate(timeout=40)
                    self.assertEqual(p.returncode, 0, msg=err[-2000:])
                    results.append(json.loads(out.strip().splitlines()[-1]))
                self.assertEqual(len({r["connection"] for r in results}), len(jobs))
                frappe.db.rollback()
                return results
            finally:
                for p in processes:
                    if p.poll() is None:
                        p.kill()
                        p.communicate(timeout=5)

    def test_teacher_overview_latest_is_per_topic_beyond_10000_rows(self):
        from research_group.reports import teacher_overview

        frappe.set_user(self.users["one"])
        progress = frappe.get_doc({"doctype": "RG Progress", "topic": self.topics["one"],
            "progress_date": nowdate(), "summary": "安静课题仍存在的最新进展", "blockers": "保留阻碍",
            "next_milestone": "保留里程碑"}).insert()
        frappe.db.set_value("RG Progress", progress.name, "recorded_at", "2000-01-01 10:00:00", update_modified=False)
        appointment = self.appointment()
        frappe.set_user(self.users["teacher"])
        decide_appointment(appointment.name, APPROVED)
        discussion = archive_discussion(appointment.name, "安静课题仍存在的最新结论", "保留行动")
        frappe.db.set_value("RG Discussion Record", discussion.name, "discussed_at", "2000-01-01 11:00:00", update_modified=False)
        # Bulk fixture rows intentionally isolate the retrieval boundary from
        # document lifecycle work. They are rolled back with this test.
        timestamp = "2030-01-01 00:00:00"
        frappe.db.bulk_insert("RG Progress", fields=[
            "name", "topic", "student", "summary", "blockers", "next_milestone", "recorded_at", "creation",
        ], values=[(f"{self.prefix}-bulk-p-{i}", self.topics["two"], self.users["two"],
                    "活跃课题进展", "活跃阻碍", "活跃里程碑", timestamp, timestamp) for i in range(10001)])
        frappe.db.bulk_insert("RG Discussion Record", fields=[
            "name", "appointment", "topic", "student", "teacher", "conclusion", "actions", "discussed_at", "creation",
        ], values=[(f"{self.prefix}-bulk-d-{i}", f"{self.prefix}-bulk-a-{i}", self.topics["two"],
                    self.users["two"], self.users["teacher"], "活跃结论", "活跃行动", timestamp, timestamp)
                   for i in range(10001)])
        rows = teacher_overview()[1]
        quiet = next(row for row in rows if row["topic"] == self.topics["one"])
        self.assertEqual(quiet["summary"], "安静课题仍存在的最新进展")
        self.assertEqual(quiet["blockers"], "保留阻碍")
        self.assertEqual(quiet["next_milestone"], "保留里程碑")
        self.assertEqual(quiet["discussion"], discussion.name)
        self.assertEqual(quiet["conclusion"], "安静课题仍存在的最新结论")

    def test_material_history_includes_more_than_1000_retained_versions(self):
        meeting, report = self.meeting()
        frappe.set_user(self.users["one"])
        content = pdf_bytes(self.prefix + "history")
        first = submit_material(report.name, "历史边界", "history.pdf", content)
        original = frappe.get_doc("RG Material Version", first["name"])
        file = frappe.get_doc("File", original.material_file)
        # Create the same valid dedup shape as repeated uploads without 1000
        # redundant disk writes: each version has its own immutable File row.
        versions, files = [], []
        for number in range(2, 1002):
            name, file_name = f"{self.prefix}-version-{number}", f"{self.prefix}-file-{number}"
            versions.append((name, report.name, number, "历史边界", "history.pdf", file_name,
                             self.users["one"], original.submitted_at, 0, original.sha256, len(content),
                             self.users["one"], original.creation, original.creation))
            files.append((file_name, file.file_name, file.file_url, 1, 0, file.content_hash, file.file_size,
                          "RG Material Version", name, self.users["one"], original.creation, original.creation))
        frappe.db.bulk_insert("RG Material Version", fields=[
            "name", "report", "version", "report_topic", "original_filename", "material_file", "submitted_by",
            "submitted_at", "late", "sha256", "size_bytes", "owner", "creation", "modified",
        ], values=versions)
        frappe.db.bulk_insert("File", fields=[
            "name", "file_name", "file_url", "is_private", "is_folder", "content_hash", "file_size",
            "attached_to_doctype", "attached_to_name", "owner", "creation", "modified",
        ], values=files)
        frappe.db.set_value("RG Meeting Report", report.name, "latest_version", 1001)
        frappe.set_user(self.users["two"])
        history = material_history(report.name)
        self.assertEqual(len(history), 1001)
        self.assertEqual([history[0].version, history[-1].version], [1001, 1])
        self.assertTrue(all(row.file_url for row in history))

    def test_native_crud_and_assignment_wait_for_publish_current_read(self):
        for action in ("create_report", "assign_reporter"):
            meeting, report = self.meeting(publish=False)
            results = self._run_race([
                {"action": "publish", "name": meeting.name, "user": self.users["secretary"], "hold_meeting": True},
                {"action": action, "name": meeting.name, "student": self.users["two"], "user": self.users["secretary"]},
            ])
            self.assertTrue(results[0]["ok"], results)
            self.assertFalse(results[1]["ok"], results)
            self.assertEqual([row["snapshot_status"] for row in results], ["草稿", "草稿"])
            self.assertEqual(frappe.db.get_value("RG Group Meeting", meeting.name, "status"), "已发布")
            self.assertFalse(frappe.db.exists("RG Meeting Report", {"meeting": meeting.name, "student": self.users["two"]}))

    def test_concurrent_overlapping_approvals_independent_connections(self):
        a, b = self.appointment(), self.appointment("two")
        results = self._run_race([
            {"action": "approve", "name": doc.name, "user": self.users["teacher"]} for doc in (a, b)
        ])
        self.assertEqual(sum(r["ok"] for r in results), 1, results)
        statuses = [frappe.db.get_value("RG Appointment", d.name, "status") for d in (a, b)]
        self.assertEqual(sorted(statuses), sorted([APPROVED, PENDING]))

    def test_concurrent_cancel_approve_uses_current_state(self):
        a = self.appointment()
        results = self._run_race([
            {"action": "approve", "name": a.name, "user": self.users["teacher"]},
            {"action": "cancel", "name": a.name, "user": self.users["one"]},
        ])
        self.assertTrue(results[1]["ok"], results)
        self.assertEqual(frappe.db.get_value("RG Appointment", a.name, "status"), CANCELLED)

    def test_concurrent_reupload_preserves_both_versions(self):
        meeting, report = self.meeting()
        results = self._run_race([
            {"action": "upload", "name": report.name, "user": self.users["one"], "label": str(i)} for i in (1, 2)
        ])
        self.assertTrue(all(r["ok"] for r in results), results)
        rows = frappe.get_all("RG Material Version", filters={"report": report.name}, pluck="version", order_by="version")
        self.assertEqual(rows, [1, 2])
        self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "latest_version"), 2)

    def _delete_fixture(self):
        emails = list(self.users.values())
        reports = frappe.get_all("RG Meeting Report", filters={"student": ["in", emails]}, pluck="name")
        versions = frappe.get_all("RG Material Version", filters={"report": ["in", reports]}, pluck="name") if reports else []
        meetings = frappe.get_all("RG Group Meeting", filters={"publisher": ["in", emails]}, pluck="name")
        owned = {"RG Material Version": versions, "RG Meeting Report": reports, "RG Group Meeting": meetings}
        for dt in ("RG Discussion Record", "RG Appointment", "RG Progress", "RG Topic"):
            owned[dt] = frappe.get_all(dt, filters={"student": ["in", emails]}, pluck="name")
        for dt, names in owned.items():
            for name in names:
                for file in frappe.get_all("File", filters={"attached_to_doctype": dt, "attached_to_name": name}, pluck="name"):
                    frappe.delete_doc("File", file, ignore_permissions=True, force=True)
                frappe.delete_doc(dt, name, ignore_permissions=True, force=True)
        for email in emails:
            frappe.delete_doc("RG Member", email, ignore_permissions=True, force=True)
            frappe.delete_doc("User", email, ignore_permissions=True, force=True)


def race_worker():
    """A separate interpreter opens an independent MariaDB connection."""
    site, sites_path, folder, worker_id, job_json = sys.argv[1:]
    job, root = json.loads(job_json), Path(folder)
    frappe.init(site, sites_path=sites_path)
    frappe.connect()
    try:
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        frappe.set_user(job["user"])
        if job["action"] == "upload":
            dt = "RG Meeting Report"
        elif job["action"] in {"publish", "create_report", "assign_reporter"}:
            dt = "RG Group Meeting"
        else:
            dt = "RG Appointment"
        snapshot = secure_get(dt, job["name"])
        snapshot_status = snapshot.get("status")
        if job.get("hold_meeting"):
            from research_group.ai.security import native_identity_lock
            native_identity_lock()
            frappe.get_doc("RG Group Meeting", job["name"], for_update=True)
        connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
        (root / f"{worker_id}.ready").write_text("snapshot ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not (root / "start").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("barrier")
            time.sleep(0.05)
        try:
            if job["action"] == "approve":
                decide_appointment(job["name"], APPROVED)
            elif job["action"] == "cancel":
                cancel_appointment(job["name"])
            elif job["action"] == "publish":
                publish_meeting(job["name"])
            elif job["action"] == "create_report":
                frappe.get_doc({"doctype": "RG Meeting Report", "meeting": job["name"], "student": job["student"]}).insert()
            elif job["action"] == "assign_reporter":
                assign_reporter(job["name"], job["student"])
            else:
                submit_material(job["name"], "并发主题" + job["label"], "race" + job["label"] + ".pdf", pdf_bytes(job["label"]))
            frappe.db.commit()
            result = {"ok": True, "connection": connection, "snapshot_status": snapshot_status}
        except (frappe.ValidationError, frappe.PermissionError) as error:
            frappe.db.rollback()
            result = {"ok": False, "connection": connection, "snapshot_status": snapshot_status, "error": type(error).__name__}
        print(json.dumps(result))
    finally:
        frappe.destroy()


