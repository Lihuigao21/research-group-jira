"""Role switches never grant authority; home edits are fixed-route plain text."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import frappe
from frappe.client import get as native_get
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from research_group import portals
from research_group.policy import SECRETARY, STUDENT, TEACHER
from research_group.reports import teacher_overview
from research_group.tests.test_core import create_members


class TestPortals(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-portal-test-" + frappe.generate_hash(length=10)
        self.users = create_members(self.prefix)
        self.committed_fixture = False
        self.topics = {}
        for key in ("one", "two"):
            self.topics[key] = frappe.get_doc({"doctype": "RG Topic", "student": self.users[key],
                "title": self.prefix + key, "direction": self.prefix + "existing direction", "priority": "中",
                "next_milestone": "初始里程碑", "status": "进行中"}).insert(ignore_permissions=True).name

    def tearDown(self):
        frappe.set_user("Administrator")
        if self.committed_fixture:
            # The only committed test creates no portal singleton or direction.
            for doctype in ("RG Topic",):
                frappe.db.delete(doctype, {"name": ["in", list(self.topics.values())]})
            for user in self.users.values():
                frappe.delete_doc("RG Member", user, ignore_permissions=True, force=True)
                frappe.delete_doc("User", user, ignore_permissions=True, force=True)
            frappe.db.commit()
        else:
            frappe.db.rollback()
        frappe.clear_cache()
        super().tearDown()

    def test_native_document_envelopes_preserve_business_limits_and_scan_all_rows(self):
        from research_group.collaboration_security import _document_types, guard_native

        options = "\n".join(f"{number:03d}" + "测" * 197 for number in range(100))
        doc = {"doctype": "RG Survey", "title": self.prefix + "large",
               "questions": [{"doctype": "RG Survey Question", "title": f"题目{number}",
                              "question_type": "单选", "required": 1, "options": options}
                             for number in range(10)]}
        payload = json.dumps(doc, ensure_ascii=False)
        self.assertGreater(len(payload.encode("utf-8")), 100000)
        self.assertEqual(_document_types(payload), ["RG Survey"])
        self.assertEqual(_document_types(doc), ["RG Survey"])
        # No arbitrary row cutoff may hide a private document near the end.
        rows = [{"doctype": "RG Survey"}] * 101 + [{"doctype": "RG Message", "body": "private"}]
        for envelope in (rows, json.dumps(rows, ensure_ascii=False)):
            types = _document_types(envelope)
            self.assertEqual(len(types), 102)
            self.assertEqual(types[-1], "RG Message")
            with self.assertRaises(frappe.PermissionError):
                for doctype in types:
                    guard_native(doctype)

    def test_single_and_combined_roles_switch_without_mutating_roles(self):
        for key, ids in (("teacher", {"teacher"}), ("one", {"student"}),
                         ("secretary", {"secretary", "student"})):
            frappe.set_user(self.users[key])
            before = frappe.get_roles()
            result = portals.bootstrap()
            self.assertEqual({row["id"] for row in result["portals"]}, ids)
            for role in ids:
                self.assertEqual(portals.bootstrap(role)["portal"], role)
            self.assertEqual(frappe.get_roles(), before)
            for role in {"teacher", "secretary", "student"} - ids:
                with self.assertRaises(frappe.PermissionError):
                    portals.bootstrap(role)

    def test_disabled_member_and_account_have_no_portal(self):
        for field in ("member", "user"):
            frappe.set_user("Administrator")
            if field == "member":
                record = frappe.get_doc("RG Member", self.users["one"])
                record.active = 0
            else:
                record = frappe.get_doc("User", self.users["two"])
                record.enabled = 0
            record.save(ignore_permissions=True)
            frappe.set_user(self.users["one" if field == "member" else "two"])
            with self.assertRaises(frappe.PermissionError):
                portals.bootstrap()

    def test_student_filter_is_enforced_and_unowned_student_has_empty_state(self):
        frappe.set_user(self.users["one"])
        frappe.get_doc({"doctype": "RG Progress", "topic": self.topics["one"], "progress_date": nowdate(),
            "summary": "当前学生进展", "blockers": "当前阻碍", "next_milestone": "当前里程碑"}).insert()
        frappe.set_user(self.users["teacher"])
        own = teacher_overview({"student": self.users["one"]})[1]
        self.assertEqual([row["topic"] for row in own], [self.topics["one"]])
        self.assertEqual(own[0]["summary"], "当前学生进展")
        self.assertTrue(own[0]["last_progress_at"])
        empty = portals.student_detail(self.users["secretary"])
        self.assertEqual(empty["topics"], [])
        self.assertEqual(empty["student"]["user"], self.users["secretary"])
        for key in ("one", "secretary"):
            frappe.set_user(self.users[key])
            with self.assertRaises(frappe.PermissionError):
                portals.student_detail(self.users["two"])
            with self.assertRaises(frappe.PermissionError):
                teacher_overview({"student": self.users["two"]})

    def test_teacher_student_list_retains_disabled_historical_student(self):
        frappe.set_user("Administrator")
        member = frappe.get_doc("RG Member", self.users["one"])
        member.active = 0
        member.save(ignore_permissions=True)
        frappe.set_user(self.users["teacher"])
        row = next(item for item in portals.teacher_students()["students"] if item.user == self.users["one"])
        self.assertFalse(row.active)
        self.assertEqual(portals.student_detail(self.users["one"])["topics"][0]["topic"], self.topics["one"])

    def test_direction_goals_and_unlinked_direction_are_retained(self):
        frappe.set_user(self.users["teacher"])
        standalone = portals.save_direction(self.prefix + "standalone", "组级目标", "高", [])
        linked = portals.save_direction(self.prefix + "linked", "关联目标", "中", [self.topics["one"]])
        result = portals.directions()
        one = next(row for row in result["directions"] if row["name"] == standalone["name"])
        two = next(row for row in result["directions"] if row["name"] == linked["name"])
        self.assertEqual((one["goal"], one["priority"], one["topics"]), ("组级目标", "高", []))
        self.assertEqual([row["topic"] for row in two["topics"]], [self.topics["one"]])
        with self.assertRaises(frappe.TimestampMismatchError):
            portals.save_direction("旧版本覆盖", name=linked["name"], expected_revision=0)
        frappe.set_user(self.users["secretary"])
        with self.assertRaises(frappe.PermissionError):
            portals.save_direction("秘书不可写方向")

    def test_explicit_direction_unlink_does_not_reappear_from_legacy_title(self):
        frappe.set_user(self.users["teacher"])
        title = self.prefix + "existing direction"
        initial = portals.directions()["directions"]
        legacy = next(row for row in initial if row["title"] == title)
        self.assertIsNone(legacy["name"])
        self.assertEqual({row["topic"] for row in legacy["topics"]}, set(self.topics.values()))
        saved = portals.save_direction(title, "显式维护", "中", [self.topics["one"]])
        linked = portals.directions()
        explicit = next(row for row in linked["directions"] if row["name"] == saved["name"])
        self.assertEqual([row["topic"] for row in explicit["topics"]], [self.topics["one"]])
        self.assertEqual(len([row for row in linked["directions"] if row["title"] == title]), 1)
        portals.save_direction(title, "解除全部关联", "中", [], name=saved["name"],
                               expected_revision=saved["revision"])
        unlinked = portals.directions()
        explicit = next(row for row in unlinked["directions"] if row["name"] == saved["name"])
        self.assertEqual(explicit["topics"], [])
        # Unlinked topics remain available for a deliberate future association.
        self.assertTrue(set(self.topics.values()).issubset({row["name"] for row in unlinked["available_topics"]}))
        frappe.set_user("Administrator")
        other = frappe.get_doc({"doctype": "RG Topic", "student": self.users["one"],
            "title": self.prefix + "unconfigured", "direction": self.prefix + "unconfigured direction",
            "priority": "中", "status": "进行中", "next_milestone": "保留旧方向聚合"}).insert(ignore_permissions=True)
        frappe.set_user(self.users["teacher"])
        aggregate = next(row for row in portals.directions()["directions"] if row["title"] == other.direction)
        self.assertIsNone(aggregate["name"])
        self.assertEqual([row["topic"] for row in aggregate["topics"]], [other.name])

    def test_home_navigation_whitelist_and_optimistic_revision(self):
        frappe.set_user(self.users["secretary"])
        original = portals.home_settings()
        # Editing the submitted copy must not mutate the saved expectation for
        # restore. The API returns a detached JSON value, just as HTTP does.
        nav = [dict(row) for row in original["navigation"]]
        nav[0]["visible"] = False
        nav[1]["label"] = "讨论申请"
        nav.reverse()
        changed = portals.save_home("纯文本首页介绍", nav, original["revision"])
        self.assertEqual(changed["introduction"], "纯文本首页介绍")
        self.assertEqual(changed["navigation"], nav)
        self.assertEqual({row["id"] for row in changed["navigation"]}, set(portals.NAVIGATION))
        self.assertEqual(changed["revision"], original["revision"] + 1)
        self.assertNotEqual(nav, original["navigation"])
        with self.assertRaises(frappe.TimestampMismatchError):
            portals.save_home("旧覆盖", nav, original["revision"])
        self.assertEqual(portals.home_settings()["navigation"], nav)
        restored = portals.restore_home(changed["revision"])
        self.assertEqual(restored["introduction"], original["introduction"])
        self.assertEqual(restored["navigation"], original["navigation"])
        self.assertEqual(restored["revision"], changed["revision"] + 1)
        frappe.set_user(self.users["teacher"])
        with self.assertRaises(frappe.PermissionError):
            portals.save_home("教师不能维护首页", nav, restored["revision"])

    def test_home_rejects_html_executable_schemes_external_routes_and_unknown_ids(self):
        frappe.set_user(self.users["secretary"])
        original = portals.home_settings()
        for payload in ("<script>alert(1)</script>", "javascript:alert(1)", "data:text/html,test"):
            with self.assertRaises(frappe.ValidationError):
                portals.save_home(payload, original["navigation"], original["revision"])
        for patch in ({"route": "https://external.invalid"}, {"id": "sql"}, {"label": "<img src=x>"}):
            nav = [dict(row) for row in original["navigation"]]
            nav[0].update(patch)
            with self.assertRaises(frappe.ValidationError):
                portals.save_home("正文", nav, original["revision"])
        self.assertEqual(portals.home_settings()["revision"], original["revision"])

    def test_announcements_audience_draft_offline_and_history_boundary(self):
        frappe.set_user(self.users["secretary"])
        draft = portals.save_announcement(self.prefix + "draft", "草稿正文")
        restricted = portals.save_announcement(self.prefix + "teacher", "教师可见", "teacher", status="published")
        public = portals.save_announcement(self.prefix + "all", "全组可见", status="published")
        frappe.set_user(self.users["one"])
        names = {row.name for row in portals.bootstrap()["announcements"]}
        self.assertIn(public["name"], names)
        self.assertNotIn(draft["name"], names)
        self.assertNotIn(restricted["name"], names)
        frappe.set_user(self.users["teacher"])
        self.assertIn(restricted["name"], {row.name for row in portals.bootstrap()["announcements"]})
        frappe.set_user(self.users["secretary"])
        with self.assertRaises(frappe.ValidationError):
            portals.save_announcement(public["title"], "发布后覆盖", name=public["name"],
                                      expected_revision=public["revision"], status="published")
        offline = portals.save_announcement(public["title"], public["body"], name=public["name"],
                                            expected_revision=public["revision"], status="offline")
        frappe.set_user(self.users["one"])
        self.assertNotIn(offline["name"], {row.name for row in portals.bootstrap()["announcements"]})

    def test_internal_portal_tables_deny_native_read_and_write(self):
        frappe.set_user(self.users["teacher"])
        direction = portals.save_direction(self.prefix + "private direction", "组级目标")
        for key in ("teacher", "secretary", "one"):
            frappe.set_user(self.users[key])
            self.assertFalse(frappe.has_permission("RG Research Direction", "read", doc=direction["name"]))
            with self.assertRaises(frappe.PermissionError):
                native_get("RG Research Direction", direction["name"])
        record = frappe.get_doc("RG Research Direction", direction["name"])
        record.goal = "原生越权覆盖"
        with self.assertRaises(frappe.PermissionError):
            record.save(ignore_permissions=True)

    def test_waiting_role_switch_rechecks_current_roles_after_revoke(self):
        frappe.set_user("Administrator")
        target = frappe.get_doc("User", self.users["secretary"])
        target.append("roles", {"role": TEACHER})
        target.save(ignore_permissions=True)
        frappe.db.commit()
        self.committed_fixture = True
        parent = Path(os.environ.get("RG_PORTAL_TEST_WORKDIR") or frappe.get_site_path(
            "private", "tasks", "20261108-fdu-accounts-meals", "scratch", "portal-races")).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=parent) as folder:
            root = Path(folder)
            argv = [sys.executable, "-c", "from research_group.tests.test_portals import role_worker; role_worker()",
                    frappe.local.site, str(Path(frappe.local.sites_path).resolve()), str(root), self.users["secretary"]]
            process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 25
                while not (root / "ready").exists():
                    if time.monotonic() > deadline or process.poll() is not None:
                        self.fail("portal worker failed before cached-role barrier")
                    time.sleep(.02)
                portals.global_lock()
                target = frappe.get_doc("User", self.users["secretary"])
                target.set("roles", [row for row in target.roles if row.role != SECRETARY])
                target.save(ignore_permissions=True)
                (root / "start").write_text("start", encoding="ascii")
                deadline = time.monotonic() + 25
                while not (root / "requested").exists():
                    if time.monotonic() > deadline:
                        self.fail("portal worker did not request authorization gate")
                    time.sleep(.02)
                frappe.db.commit()
                output, error = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, error[-2000:])
                result = json.loads(output.strip().splitlines()[-1])
                self.assertFalse(result["can_secretary"])
                self.assertEqual(set(result["portals"]), {"teacher", "student"})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
        frappe.db.rollback()


def role_worker():
    from unittest.mock import patch
    site, sites_path, folder, user = sys.argv[1:]
    root = Path(folder)
    frappe.init(site, sites_path=sites_path)
    frappe.connect()
    frappe.in_test = True
    try:
        frappe.set_user(user)
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        frappe.db.get_value("User", user, "enabled")
        frappe.get_roles()
        (root / "ready").write_text("cached roles", encoding="ascii")
        deadline = time.monotonic() + 25
        while not (root / "start").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("portal role barrier")
            time.sleep(.02)
        original = portals.global_lock

        def observed_gate():
            (root / "requested").write_text("requested", encoding="ascii")
            return original()

        with patch.object(portals, "global_lock", side_effect=observed_gate):
            result = portals.bootstrap()
            try:
                portals.bootstrap("secretary")
                can_secretary = True
            except frappe.PermissionError:
                can_secretary = False
        frappe.db.rollback()
        print(json.dumps({"portals": [row["id"] for row in result["portals"]], "can_secretary": can_secretary}))
    finally:
        frappe.destroy()
