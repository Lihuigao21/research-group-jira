"""Native draft corrections and complete permission-aware report queries."""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, nowdate

from research_group.policy import ARCHIVED, DRAFT, PUBLISHED, SECRETARY, STUDENT, TEACHER, secure_get
from research_group.services import assign_reporter, publish_meeting, submit_material
from research_group.tests import test_core as core


class TestReportCorrections(IntegrationTestCase):
    # Reuse fixture helpers without inheriting and rerunning every core test.
    appointment = core.TestResearchGroupCore.appointment
    meeting = core.TestResearchGroupCore.meeting
    _delete_fixture = core.TestResearchGroupCore._delete_fixture

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-test-" + frappe.generate_hash(length=10)
        self.users = {key: f"{self.prefix}-{key}@example.invalid"
                      for key in ("teacher", "other_teacher", "one", "two", "secretary")}
        assigned = {"teacher": [TEACHER], "other_teacher": [TEACHER], "one": [STUDENT],
                    "two": [STUDENT], "secretary": [SECRETARY, STUDENT]}
        for key, email in self.users.items():
            frappe.get_doc({"doctype": "User", "email": email, "first_name": "虚构测试" + key,
                "user_type": "System User", "language": "zh", "enabled": 1, "send_welcome_email": 0,
                "roles": [{"role": role} for role in assigned[key]]}).insert(ignore_permissions=True)
            frappe.get_doc({"doctype": "RG Member", "user": email, "display_name": "虚构测试" + key,
                "active": 1, "is_demo": 1}).insert(ignore_permissions=True)
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

    def test_publisher_corrects_only_draft_student_via_native_save(self):
        from frappe.client import save

        meeting, report = self.meeting(publish=False)
        original_name = report.name
        values = frappe.get_doc("RG Meeting Report", report.name).as_dict()
        values["student"] = self.users["two"]
        result = save(values)
        self.assertEqual(result["name"], original_name)
        self.assertEqual(result["student"], self.users["two"])
        self.assertEqual(result["meeting"], meeting.name)
        self.assertFalse(frappe.db.exists("RG Meeting Report", {
            "meeting": meeting.name, "student": self.users["one"],
        }))
        # An unchanged save must exclude the report itself from duplicate checks.
        frappe.get_doc("RG Meeting Report", report.name).save()
        other_meeting, _ = self.meeting(publish=False)
        for field, value in (("meeting", other_meeting.name), ("topic", "伪造主题"),
                             ("latest_version", 1), ("last_submitted_at", "2030-01-01 00:00:00"),
                             ("late", 1)):
            with self.subTest(field=field):
                changed = frappe.get_doc("RG Meeting Report", report.name)
                changed.set(field, value)
                with self.assertRaises(frappe.ValidationError):
                    changed.save()
        publish_meeting(meeting.name)
        changed = frappe.get_doc("RG Meeting Report", report.name)
        changed.student = self.users["one"]
        with self.assertRaises(frappe.PermissionError):
            changed.save()
        self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "student"), self.users["two"])

    def test_draft_correction_rejects_inactive_duplicate_and_nonpublisher(self):
        meeting, report = self.meeting(publish=False)
        frappe.set_user("Administrator")
        frappe.db.set_value("RG Member", self.users["two"], "active", 0)
        frappe.set_user(self.users["secretary"])
        changed = frappe.get_doc("RG Meeting Report", report.name)
        changed.student = self.users["two"]
        with self.assertRaises(frappe.ValidationError):
            changed.save()
        frappe.set_user("Administrator")
        frappe.db.set_value("RG Member", self.users["two"], "active", 1)
        frappe.set_user(self.users["secretary"])
        second = assign_reporter(meeting.name, self.users["two"])
        changed = frappe.get_doc("RG Meeting Report", report.name)
        changed.student = self.users["two"]
        with self.assertRaises(frappe.ValidationError):
            changed.save()
        frappe.set_user(self.users["other_teacher"])
        changed = frappe.get_doc("RG Meeting Report", report.name)
        changed.student = self.users["secretary"]
        with self.assertRaises(frappe.PermissionError):
            changed.save()
        self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "student"), self.users["one"])
        self.assertEqual(frappe.db.get_value("RG Meeting Report", second.name, "student"), self.users["two"])

    def test_draft_correction_waits_for_publish_and_rechecks_current_status(self):
        meeting, report = self.meeting(publish=False)
        frappe.db.commit()
        self.committed_fixture = True
        parent = Path(frappe.get_site_path("private", "rg-test-concurrency"))
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=parent) as folder:
            root = Path(folder).resolve()
            jobs = [
                {"action": "publish", "meeting": meeting.name, "user": self.users["secretary"]},
                {"action": "correct", "meeting": meeting.name, "report": report.name,
                 "student": self.users["two"], "user": self.users["secretary"]},
            ]
            script = "from research_group.tests.test_report_corrections import correction_race_worker; correction_race_worker()"
            processes = []
            try:
                for index, job in enumerate(jobs):
                    processes.append(subprocess.Popen([
                        sys.executable, "-c", script, frappe.local.site,
                        str(Path(frappe.local.sites_path).resolve()), str(root), str(index), json.dumps(job),
                    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                deadline = time.monotonic() + 30
                while not all((root / f"{index}.ready").exists() for index in range(2)):
                    if time.monotonic() > deadline or any(process.poll() is not None for process in processes):
                        self.fail("independent correction workers did not reach the snapshot barrier")
                    time.sleep(0.05)
                (root / "start").write_text("start", encoding="ascii")
                results = []
                for process in processes:
                    output, error = process.communicate(timeout=40)
                    self.assertEqual(process.returncode, 0, error[-2000:])
                    results.append(json.loads(output.strip().splitlines()[-1]))
                self.assertEqual(len({result["connection"] for result in results}), 2)
                self.assertEqual([result["snapshot_status"] for result in results], [DRAFT, DRAFT])
                self.assertTrue(results[0]["ok"], results)
                self.assertFalse(results[1]["ok"], results)
                self.assertIn("发布后", results[1]["message"])
                frappe.db.rollback()
                self.assertEqual(frappe.db.get_value("RG Group Meeting", meeting.name, "status"), PUBLISHED)
                self.assertEqual(frappe.db.get_value("RG Meeting Report", report.name, "student"), self.users["one"])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

    def test_registered_secretary_schedule_pending_ontime_late_and_identity(self):
        from frappe.desk.query_report import run

        meeting, first = self.meeting(publish=False)
        second = assign_reporter(meeting.name, self.users["two"])
        pending = assign_reporter(meeting.name, self.users["secretary"])
        publish_meeting(meeting.name)
        hidden, _ = self.meeting(publish=False)
        for key, report, submitted_at, topic in (
            ("one", first, "2030-01-02 23:58:00", "按时报告"),
            ("two", second, "2030-01-03 00:00:00", "迟交报告"),
        ):
            frappe.set_user(self.users[key])
            with patch("research_group.services.now_datetime", return_value=get_datetime(submitted_at)):
                submit_material(report.name, topic, key + ".pdf", core.pdf_bytes(self.prefix + key))
        frappe.set_user(self.users["two"])
        result = run("RG Secretary Schedule", filters={}, ignore_prepared_report=True)
        rows = {row["report"]: row for row in result["result"] if row["meeting"] == meeting.name}
        self.assertEqual(set(rows), {first.name, second.name, pending.name})
        self.assertFalse(any(row["meeting"] == hidden.name for row in result["result"]))
        for report, key, topic, versions, late in (
            (first, "one", "按时报告", 1, 0), (second, "two", "迟交报告", 1, 1),
            (pending, "secretary", "待提交", 0, 0),
        ):
            row = rows[report.name]
            self.assertEqual(row["student"], self.users[key])
            self.assertEqual(row["topic"], topic)
            self.assertEqual(row["versions"], versions)
            self.assertEqual(row["late"], late)
            if versions:
                self.assertEqual(get_datetime(row["submitted_at"]), get_datetime(
                    "2030-01-02 23:58:00" if key == "one" else "2030-01-03 00:00:00"))
            else:
                self.assertFalse(row["submitted_at"])

    def test_student_timeline_keeps_all_three_histories_beyond_10000(self):
        from research_group.reports import student_timeline

        timestamp = "2030-01-01 00:00:00"
        count = 10001
        # Bulk fixtures isolate each retrieval boundary from document lifecycle;
        # all stay in this test's rollback transaction, with valid owned links.
        frappe.db.bulk_insert("RG Progress", fields=[
            "name", "topic", "student", "progress_date", "summary", "recorded_at", "creation",
        ], values=[(f"{self.prefix}-progress-{index}", self.topics["one"], self.users["one"],
                    nowdate(), "保留进展", timestamp, timestamp) for index in range(count)])
        frappe.db.bulk_insert("RG Appointment", fields=[
            "name", "topic", "student", "teacher", "start_at", "end_at", "agenda", "status", "creation",
        ], values=[(f"{self.prefix}-appointment-{index}", self.topics["one"], self.users["one"],
                    self.users["teacher"], "2030-01-01 14:00:00", "2030-01-01 15:00:00", "保留议题",
                    ARCHIVED, timestamp) for index in range(count)])
        frappe.db.bulk_insert("RG Discussion Record", fields=[
            "name", "appointment", "topic", "student", "teacher", "conclusion", "actions", "recorded_at", "creation",
        ], values=[(f"{self.prefix}-discussion-{index}", f"{self.prefix}-appointment-{index}",
                    self.topics["one"], self.users["one"], self.users["teacher"], "保留结论", "保留行动",
                    timestamp, timestamp) for index in range(count)])
        frappe.set_user(self.users["two"])
        unrelated = frappe.get_doc({"doctype": "RG Progress", "topic": self.topics["two"],
            "progress_date": nowdate(), "summary": "他人私人进展", "next_milestone": "虚构下一步"}).insert()
        frappe.set_user(self.users["one"])
        rows = student_timeline()[1]
        self.assertTrue(all(row["student"] == self.users["one"] for row in rows))
        self.assertFalse(any(row["reference_name"] == unrelated.name for row in rows))
        for doctype in ("RG Progress", "RG Appointment", "RG Discussion Record"):
            own = [row for row in rows if row["reference_type"] == doctype
                   and row["reference_name"].startswith(self.prefix)]
            self.assertEqual(len(own), count, doctype)

    def test_schedule_keeps_all_visible_meetings_and_reports_beyond_10000(self):
        from research_group.reports import secretary_schedule

        quiet, quiet_report = self.meeting()
        count = 10001
        timestamp = "2031-12-30 00:00:00"
        names = [f"{self.prefix}-meeting-{index}" for index in range(count)]
        frappe.db.bulk_insert("RG Group Meeting", fields=[
            "name", "title", "publisher", "status", "start_at", "end_at", "deadline", "location", "creation",
        ], values=[(name, "虚构边界组会", self.users["secretary"], PUBLISHED, "2032-01-01 14:00:00",
                    "2032-01-01 16:00:00", "2031-12-31 23:59:00", "虚构会议室", timestamp) for name in names])
        frappe.db.bulk_insert("RG Meeting Report", fields=[
            "name", "meeting", "student", "topic", "latest_version", "last_submitted_at", "late", "creation",
        ], values=[(f"{self.prefix}-report-{index}", name, self.users["one"], "已有提交主题", 1,
                    timestamp, 0, timestamp) for index, name in enumerate(names)])
        frappe.set_user(self.users["one"])
        with patch("research_group.reports.frappe.get_list", wraps=frappe.get_list) as queries:
            rows = secretary_schedule()[1]
        expected_meetings = set(names) | {quiet.name}
        selected = {row["meeting"]: row for row in rows if row["meeting"] in expected_meetings}
        self.assertEqual(len(selected), count + 1)
        self.assertEqual(selected[quiet.name]["report"], quiet_report.name)
        for name in names:
            self.assertEqual(selected[name]["student"], self.users["one"])
            self.assertEqual(selected[name]["topic"], "已有提交主题")
            self.assertEqual(selected[name]["versions"], 1)
        meeting_names = {row["meeting"] for row in rows}
        report_queries = [call for call in queries.call_args_list if call.args and call.args[0] == "RG Meeting Report"]
        self.assertEqual(len(report_queries), 1)
        self.assertEqual(set(report_queries[0].kwargs["filters"]["meeting"][1]), meeting_names)
        self.assertEqual(report_queries[0].kwargs["limit_page_length"], 0)


def correction_race_worker():
    """Hold publication's parent lock before native correction attempts it."""
    site, sites_path, folder, worker_id, job_json = sys.argv[1:]
    job, root = json.loads(job_json), Path(folder)
    frappe.init(site, sites_path=sites_path)
    frappe.connect()
    try:
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        frappe.set_user(job["user"])
        snapshot = secure_get("RG Group Meeting", job["meeting"])
        report = None
        if job["action"] == "publish":
            from research_group.ai.security import native_identity_lock
            native_identity_lock()
            frappe.get_doc("RG Group Meeting", job["meeting"], for_update=True)
        else:
            report = frappe.get_doc("RG Meeting Report", job["report"])
        connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
        (root / f"{worker_id}.ready").write_text("snapshot ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not (root / "start").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("snapshot barrier")
            time.sleep(0.05)
        try:
            if job["action"] == "publish":
                while not (root / "correcting").exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("correction did not request the parent lock")
                    time.sleep(0.05)
                publish_meeting(job["meeting"])
            else:
                native_get_doc = frappe.get_doc

                def observe_parent_lock(*args, **kwargs):
                    if args[:2] == ("RG Group Meeting", job["meeting"]) and kwargs.get("for_update"):
                        (root / "correcting").write_text("parent lock requested", encoding="ascii")
                    return native_get_doc(*args, **kwargs)

                report.student = job["student"]
                with patch.object(frappe, "get_doc", side_effect=observe_parent_lock):
                    report.save()
            frappe.db.commit()
            result = {"ok": True, "connection": connection, "snapshot_status": snapshot.status}
        except (frappe.ValidationError, frappe.PermissionError) as error:
            frappe.db.rollback()
            result = {"ok": False, "connection": connection, "snapshot_status": snapshot.status,
                      "error": type(error).__name__, "message": str(error)}
        print(json.dumps(result))
    finally:
        frappe.destroy()
