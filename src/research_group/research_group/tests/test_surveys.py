"""Real Frappe policy/validation and independent MariaDB connection controls."""
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.client import get as api_get
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, now_datetime

from research_group.policy import DRAFT, PUBLISHED, SECRETARY, STUDENT, secure_get
from research_group.surveys import (
    CLOSED, close_survey, export_csv, get_survey, my_response, publish_survey, submit_response, survey_results,
)
from research_group.tests import test_core as core


class TestSurveys(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-survey-test-" + frappe.generate_hash(length=10)
        self.users = core.create_members(self.prefix)
        self.committed_fixture = False

    def tearDown(self):
        frappe.set_user("Administrator")
        if self.committed_fixture:
            surveys = frappe.get_all("RG Survey", filters={"publisher": ["in", list(self.users.values())]}, pluck="name")
            for name in surveys:
                for response in frappe.get_all("RG Survey Response", filters={"survey": name}, pluck="name"):
                    frappe.delete_doc("RG Survey Response", response, ignore_permissions=True, force=True)
                frappe.delete_doc("RG Survey", name, ignore_permissions=True, force=True)
            core.TestResearchGroupCore._delete_fixture(self)
            frappe.db.commit()
        else:
            frappe.db.rollback()
        frappe.clear_cache()
        super().tearDown()

    def survey(self, questions=None, publish=True, **values):
        frappe.set_user(self.users["secretary"])
        if questions is None:
            questions = [
                {"title": "虚构单选", "question_type": "单选", "required": 1, "options": "甲\n乙"},
                {"title": "虚构多选", "question_type": "多选", "required": 1, "options": "早\n晚"},
                {"title": "虚构简答", "question_type": "简答", "required": 1},
            ]
        doc = frappe.get_doc({"doctype": "RG Survey", "title": self.prefix + "问卷",
                              "questions": questions, **values}).insert()
        if publish:
            publish_survey(doc.name)
            doc.reload()
        return doc

    def answer(self, doc, choice="甲", text="虚构简答", multiple=None):
        return {doc.questions[0].name: choice, doc.questions[1].name: multiple or ["早"],
                doc.questions[2].name: text}

    def test_secretary_native_form_and_draft_member_visibility(self):
        for key in ("one", "teacher"):
            frappe.set_user(self.users[key])
            self.assertFalse(frappe.has_permission("RG Survey", "create"))
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc({"doctype": "RG Survey", "title": "越权草稿"}).insert()
        doc = self.survey(publish=False)
        self.assertTrue(frappe.has_permission("RG Survey", "write", doc=doc))
        writable = doc.meta.get_permitted_fieldnames(permission_type="write")
        for field in ("title", "description", "deadline"):
            self.assertIn(field, writable)
            self.assertTrue(doc.has_permlevel_access_to(field, permission_type="write"))
        # Frappe's stored-value field list excludes Table fields. Check the
        # parent field's permission level and the child's inherited permissions.
        table = doc.meta.get_field("questions")
        self.assertEqual(table.fieldtype, "Table")
        self.assertFalse(table.read_only)
        self.assertTrue(doc.has_permlevel_access_to("questions", permission_type="write"))
        child_meta = frappe.get_meta(table.options)
        child_writable = child_meta.get_permitted_fieldnames(parenttype=doc.doctype, permission_type="write")
        for field in ("title", "question_type", "required", "options"):
            self.assertIn(field, child_writable)
            self.assertFalse(child_meta.get_field(field).read_only)
        # Exercise ordinary parent save, including an existing row and a new
        # row, then read their persisted identities and values from the DB.
        child_name = doc.questions[0].name
        doc.questions[0].title = "秘书更正的草稿题目"
        doc.questions[0].question_type = "多选"
        doc.questions[0].required = 0
        doc.questions[0].options = "甲\n乙\n丙"
        doc.append("questions", {"title": "秘书新增的草稿题目", "question_type": "简答", "required": 0})
        doc.save()
        doc = frappe.get_doc("RG Survey", doc.name)
        self.assertEqual(doc.status, DRAFT)
        self.assertEqual(len(doc.questions), 4)
        self.assertEqual(doc.questions[0].name, child_name)
        self.assertEqual(doc.questions[0].title, "秘书更正的草稿题目")
        self.assertEqual(doc.questions[0].question_type, "多选")
        self.assertEqual(doc.questions[0].required, 0)
        self.assertEqual(doc.questions[0].options, "甲\n乙\n丙")
        self.assertEqual(doc.questions[3].title, "秘书新增的草稿题目")
        self.assertEqual(doc.questions[3].parent, doc.name)
        self.assertEqual(doc.questions[3].parenttype, "RG Survey")
        for key in ("one", "teacher"):
            frappe.set_user(self.users[key])
            with self.assertRaises(frappe.PermissionError):
                api_get("RG Survey", doc.name)
            with self.assertRaises(frappe.PermissionError):
                get_survey(doc.name)
            self.assertFalse(frappe.get_list("RG Survey", filters={"name": doc.name}, pluck="name"))
            self.assertFalse(frappe.get_list("RG Survey Question", parent_doctype="RG Survey",
                                            filters={"parent": doc.name}, fields=["name", "title"]))

    def test_publish_rejects_empty_blank_duplicate_and_expired_drafts(self):
        invalid = [[], [{"title": " ", "question_type": "简答"}],
                   [{"title": "重复", "question_type": "单选", "options": "甲\n 甲 "}],
                   [{"title": "不足选项", "question_type": "多选", "options": "甲"}],
                   [{"title": "简答多余选项", "question_type": "简答", "options": "甲\n乙"}]]
        for questions in invalid:
            doc = self.survey(questions=questions, publish=False)
            with self.subTest(questions=questions), self.assertRaises(frappe.ValidationError):
                publish_survey(doc.name)
            self.assertEqual(frappe.db.get_value("RG Survey", doc.name, "status"), DRAFT)
            self.assertEqual(frappe.db.count("RG Survey Response", {"survey": doc.name}), 0)
        doc = self.survey(publish=False, deadline=now_datetime() - timedelta(seconds=1))
        with self.assertRaises(frappe.ValidationError):
            publish_survey(doc.name)
        self.assertEqual(frappe.db.get_value("RG Survey", doc.name, "status"), DRAFT)

    def test_single_poll_optional_inputs_and_no_automatic_deadline(self):
        doc = self.survey(questions=[{"title": "是否参加", "question_type": "单选",
                                     "required": 0, "options": "是\n否"}])
        self.assertEqual(doc.kind, "投票")
        frappe.set_user(self.users["teacher"])
        first = submit_response(doc.name, {})
        self.assertEqual(first["answers"], {doc.questions[0].name: ""})
        second = submit_response(doc.name, {doc.questions[0].name: "是"})
        self.assertEqual(first["name"], second["name"])
        self.assertEqual(second["revision"], 2)

    def test_native_question_count_create_and_edit_limits(self):
        questions = [{"title": f"虚构题目 {index}", "question_type": "简答", "required": 0}
                     for index in range(100)]
        doc = self.survey(questions=questions, publish=False)
        self.assertEqual(len(frappe.get_doc("RG Survey", doc.name).questions), 100)
        before = frappe.db.count("RG Survey", {"publisher": self.users["secretary"]})
        with self.assertRaises(frappe.ValidationError):
            self.survey(questions=[*questions, {"title": "第101题", "question_type": "简答"}], publish=False)
        self.assertEqual(frappe.db.count("RG Survey", {"publisher": self.users["secretary"]}), before)
        doc.questions[99].title = "第100题的有效草稿修改"
        doc.save()
        stored = frappe.get_doc("RG Survey", doc.name)
        self.assertEqual(len(stored.questions), 100)
        self.assertEqual(stored.questions[99].title, "第100题的有效草稿修改")
        modified = stored.modified
        doc.append("questions", {"title": "超出上限", "question_type": "简答"})
        with self.assertRaises(frappe.ValidationError):
            doc.save()
        stored = frappe.get_doc("RG Survey", doc.name)
        self.assertEqual(len(stored.questions), 100)
        self.assertEqual(stored.modified, modified)
        self.assertEqual(stored.questions[99].title, "第100题的有效草稿修改")

    def test_unicode_option_capacity_and_native_create_edit_limits(self):
        options = [chr(0x1F600 + index) * 200 for index in range(100)]
        raw = "\n".join(options)
        self.assertGreater(len(raw.encode("utf-8")), 65535)
        doc = self.survey(questions=[{"title": "Unicode选项容量", "question_type": "单选",
                                     "required": 1, "options": raw}], publish=False)
        doc.questions[0].title = "Unicode选项有效修改"
        doc.save()
        stored = frappe.get_doc("RG Survey", doc.name)
        self.assertEqual(stored.questions[0].options, raw)
        modified = stored.modified
        oversized = [*options, chr(0x1F6FF) * 200]
        before = frappe.db.count("RG Survey", {"publisher": self.users["secretary"]})
        with self.assertRaisesRegex(frappe.ValidationError, "第 1 题"):
            self.survey(questions=[{"title": "101个选项", "question_type": "单选",
                                   "options": "\n".join(oversized)}], publish=False)
        self.assertEqual(frappe.db.count("RG Survey", {"publisher": self.users["secretary"]}), before)
        for invalid in ("\n".join(oversized), "\n".join([options[0] + "😀", *options[1:]])):
            candidate = frappe.get_doc("RG Survey", doc.name)
            candidate.questions[0].options = invalid
            with self.assertRaisesRegex(frappe.ValidationError, "第 1 题"):
                candidate.save()
            stored = frappe.get_doc("RG Survey", doc.name)
            self.assertEqual(stored.modified, modified)
            self.assertEqual(stored.questions[0].options, raw)
        publish_survey(doc.name)
        frappe.set_user(self.users["one"])
        public = get_survey(doc.name)
        self.assertEqual(public["questions"][0]["options"], options)
        self.assertEqual(frappe.db.get_value("RG Survey Question", stored.questions[0].name, "options"), raw)

    def test_later_question_errors_include_only_stable_question_number(self):
        doc = self.survey()
        frappe.set_user(self.users["one"])
        good = self.answer(doc)
        cases = [(2, {**good, doc.questions[1].name: ["非法选项"]}),
                 (3, {**good, doc.questions[2].name: ""}),
                 (3, {**good, doc.questions[2].name: 99}),
                 (3, {**good, doc.questions[2].name: "字" * 5001})]
        for number, answers in cases:
            with self.subTest(number=number), self.assertRaisesRegex(frappe.ValidationError, f"第 {number} 题") as failure:
                submit_response(doc.name, answers)
            self.assertNotIn(doc.questions[number - 1].title, str(failure.exception))
        self.assertEqual(frappe.db.count("RG Survey Response", {"survey": doc.name}), 0)
        draft = self.survey(publish=False)
        draft.questions[1].options = "相同\n相同"
        draft.save()
        with self.assertRaisesRegex(frappe.ValidationError, "第 2 题"):
            publish_survey(draft.name)

    def test_invalid_whole_answer_never_creates_or_partially_updates(self):
        doc = self.survey()
        frappe.set_user(self.users["one"])
        good = self.answer(doc)
        invalid = [[], None, "not json", {**good, "not-a-question": "伪造"},
                   {doc.questions[0].name: "甲", doc.questions[1].name: ["早"]},
                   {**good, doc.questions[0].name: "非法"}, {**good, doc.questions[0].name: ["甲"]},
                   {**good, doc.questions[1].name: "早"}, {**good, doc.questions[1].name: ["早", "早"]},
                   {**good, doc.questions[1].name: ["未知"]}, {**good, doc.questions[2].name: 12},
                   {**good, doc.questions[2].name: "字" * 5001}]
        for value in invalid:
            with self.subTest(value=str(value)[:100]), self.assertRaises(frappe.ValidationError):
                submit_response(doc.name, value)
            self.assertEqual(frappe.db.count("RG Survey Response", {"survey": doc.name}), 0)
        response = submit_response(doc.name, json.dumps(good))
        with self.assertRaises(frappe.ValidationError):
            submit_response(doc.name, {**good, doc.questions[0].name: "非法"})
        self.assertEqual(my_response(doc.name)["answers"], good)
        self.assertEqual(my_response(doc.name)["revision"], 1)
        self.assertEqual(my_response(doc.name)["name"], response["name"])
        duplicate = '{"' + doc.questions[0].name + '":"甲","' + doc.questions[0].name + '":"乙"}'
        with self.assertRaises(frappe.ValidationError):
            submit_response(doc.name, duplicate)

    def test_update_counts_latest_content_once_and_unique_index_is_enforced(self):
        doc = self.survey()
        frappe.set_user(self.users["one"])
        first = submit_response(doc.name, self.answer(doc, text="旧答复"))
        second = submit_response(doc.name, self.answer(doc, choice="乙", text="最新答复", multiple=["晚", "早"]))
        self.assertEqual(first["name"], second["name"])
        self.assertEqual(first["submitted_at"], second["submitted_at"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["answers"][doc.questions[1].name], ["早", "晚"])
        self.assertEqual(frappe.db.count("RG Survey Response", {"survey": doc.name}), 1)
        duplicate = frappe.get_doc("RG Survey Response", first["name"])
        duplicate.name = frappe.generate_hash(length=10)
        with self.assertRaises((frappe.UniqueValidationError, frappe.DuplicateEntryError)):
            duplicate.db_insert()
        frappe.set_user(self.users["secretary"])
        result = survey_results(doc.name)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual([option["count"] for option in result["questions"][0]["options"]], [0, 1])
        self.assertEqual([option["count"] for option in result["questions"][1]["options"]], [1, 1])
        self.assertEqual(result["questions"][2]["answers"][0]["answer"], "最新答复")

    def test_realname_details_and_export_are_secretary_only_even_for_teachers(self):
        doc = self.survey()
        replies = {}
        for key in ("one", "two", "teacher"):
            frappe.set_user(self.users[key])
            replies[key] = submit_response(doc.name, self.answer(doc, text="私密答复" + key))
        for key in ("one", "teacher"):
            frappe.set_user(self.users[key])
            self.assertEqual(my_response(doc.name)["respondent"], self.users[key])
            self.assertEqual(api_get("RG Survey Response", replies[key]["name"])["respondent"], self.users[key])
            with self.assertRaises(frappe.PermissionError):
                api_get("RG Survey Response", replies["two"]["name"])
            visible = frappe.get_list("RG Survey Response", filters={"survey": doc.name},
                                      fields=["name", "respondent", "answers_json"])
            self.assertEqual([item.respondent for item in visible], [self.users[key]])
            for method in (survey_results, export_csv):
                frappe.response.pop("filecontent", None)
                with self.assertRaises(frappe.PermissionError):
                    method(doc.name)
                self.assertNotIn("filecontent", frappe.response)
        frappe.set_user(self.users["secretary"])
        result = survey_results(doc.name)
        self.assertEqual(result["submitted_count"], 3)
        self.assertEqual({row["respondent"] for row in result["responses"]},
                         {self.users[key] for key in ("one", "two", "teacher")})
        self.assertEqual(len(frappe.get_list("RG Survey Response", filters={"survey": doc.name})), 3)
        frappe.set_user("Administrator")
        frappe.get_doc("User", self.users["other_teacher"]).add_roles(SECRETARY)
        frappe.set_user(self.users["other_teacher"])
        self.assertEqual(survey_results(doc.name)["submitted_count"], 3)
        self.assertEqual(close_survey(doc.name)["status"], CLOSED)

    def test_disabled_account_and_member_lose_direct_read_write_and_list_access(self):
        doc = self.survey()
        frappe.set_user(self.users["one"])
        response = submit_response(doc.name, self.answer(doc))
        for dt, name, field in (("RG Member", self.users["one"], "active"),
                                ("User", self.users["one"], "enabled")):
            frappe.set_user("Administrator")
            frappe.db.set_value(dt, name, field, 0)
            frappe.clear_cache(user=self.users["one"])
            frappe.set_user(self.users["one"])
            for method, args in ((get_survey, (doc.name,)), (my_response, (doc.name,)),
                                 (submit_response, (doc.name, self.answer(doc)))):
                with self.subTest(doctype=dt, method=method.__name__), self.assertRaises(frappe.PermissionError):
                    method(*args)
            for doctype, name in (("RG Survey", doc.name), ("RG Survey Response", response["name"])):
                with self.assertRaises(frappe.PermissionError):
                    api_get(doctype, name)
                self.assertFalse(frappe.get_list(doctype, filters={"name": name}))
            frappe.set_user("Administrator")
            frappe.db.set_value(dt, self.users["one"], field, 1)
        frappe.db.set_value("RG Member", self.users["secretary"], "active", 0)
        frappe.set_user(self.users["secretary"])
        for method in (survey_results, export_csv, close_survey):
            with self.assertRaises(frappe.PermissionError):
                method(doc.name)

    def test_server_deadline_equality_and_close_preserve_old_answers(self):
        current = now_datetime()
        deadline = current + timedelta(hours=1)
        doc = self.survey(deadline=deadline)
        frappe.set_user(self.users["one"])
        with patch("research_group.surveys.now_datetime", return_value=deadline - timedelta(microseconds=1)):
            first = submit_response(doc.name, self.answer(doc))
        for instant in (deadline, deadline + timedelta(seconds=1)):
            with patch("research_group.surveys.now_datetime", return_value=instant):
                with self.assertRaises(frappe.ValidationError):
                    submit_response(doc.name, self.answer(doc, text="过期修改"))
                frappe.set_user(self.users["two"])
                with self.assertRaises(frappe.ValidationError):
                    submit_response(doc.name, self.answer(doc))
                frappe.set_user(self.users["one"])
        self.assertEqual(my_response(doc.name)["answers"], first["answers"])
        frappe.set_user(self.users["secretary"])
        close_survey(doc.name)
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.ValidationError):
            submit_response(doc.name, self.answer(doc))
        self.assertEqual(my_response(doc.name)["revision"], 1)

    def test_native_crud_cannot_forge_state_identity_or_change_frozen_questions(self):
        doc = self.survey(publish=False)
        forged = frappe.get_doc({"doctype": "RG Survey", "title": "伪造发布", "status": PUBLISHED})
        with self.assertRaises(frappe.ValidationError):
            forged.insert()
        doc.flags.rg_service = True
        doc.status = PUBLISHED
        with self.assertRaises(frappe.ValidationError):
            doc.save()
        doc.reload()
        publish_survey(doc.name)
        for field, value in (("title", "覆盖"), ("deadline", "2099-01-01 00:00:00"),
                             ("publisher", self.users["teacher"]), ("status", DRAFT)):
            candidate = frappe.get_doc("RG Survey", doc.name)
            candidate.set(field, value)
            with self.subTest(field=field), self.assertRaises((frappe.ValidationError, frappe.PermissionError)):
                candidate.save(ignore_permissions=True)
        child = frappe.get_doc("RG Survey Question", doc.questions[0].name)
        child.title = "独立覆盖已发布题目"
        with self.assertRaises(frappe.ValidationError):
            child.save(ignore_permissions=True)
        frappe.set_user(self.users["one"])
        forged = frappe.get_doc({"doctype": "RG Survey Response", "survey": doc.name,
                                "respondent": self.users["two"], "answers_json": "{}"})
        forged.flags.rg_service = True
        with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
            forged.insert(ignore_permissions=True)
        self.assertEqual(frappe.db.count("RG Survey Response", {"survey": doc.name}), 0)

    def test_child_identity_cannot_move_between_drafts(self):
        first, second = self.survey(publish=False), self.survey(publish=False)
        original = first.questions[0].name
        second.questions[0].name = original
        with self.assertRaises(frappe.PermissionError):
            second.save()
        self.assertEqual(frappe.db.get_value("RG Survey Question", original, "parent"), first.name)

    def test_csv_content_formula_neutralization_and_inactive_history(self):
        doc = self.survey(questions=[
            {"title": "含符号单选", "question_type": "单选", "required": 1, "options": "=SUM(1,2)\n普通"},
            {"title": "含符号多选", "question_type": "多选", "required": 1, "options": "-2\n@user"},
            {"title": "简答", "question_type": "简答", "required": 1},
        ])
        frappe.set_user("Administrator")
        frappe.db.set_value("RG Member", self.users["one"], "display_name", "=虚构姓名")
        frappe.set_user(self.users["one"])
        submit_response(doc.name, {doc.questions[0].name: "=SUM(1,2)",
                                  doc.questions[1].name: ["-2", "@user"],
                                  doc.questions[2].name: "  =1+1\n第二行,带逗号"})
        frappe.set_user(self.users["secretary"])
        before = survey_results(doc.name)
        export_csv(doc.name)
        content = frappe.response.pop("filecontent")
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], self.users["one"])
        self.assertEqual(rows[1][1], "'=虚构姓名")
        self.assertEqual(rows[1][5], "'=SUM(1,2)")
        self.assertEqual(rows[1][6], "'-2\n@user")
        self.assertEqual(rows[1][7], "'=1+1\n第二行,带逗号")
        frappe.set_user("Administrator")
        frappe.db.set_value("RG Member", self.users["one"], "active", 0)
        frappe.set_user(self.users["secretary"])
        after = survey_results(doc.name)
        self.assertEqual(after["submitted_count"], 1)
        self.assertEqual(after["active_member_count"], before["active_member_count"] - 1)
        self.assertEqual(after["active_submitted_count"], 0)

    def _race(self, jobs):
        frappe.db.commit()
        self.committed_fixture = True
        parent = Path(os.environ.get("RG_SURVEY_TEST_WORKDIR") or frappe.get_site_path(
            "private", "tasks", "20261106-surveys", "work", "scratch", "races")).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        script = "from research_group.tests.test_surveys import survey_race_worker; survey_race_worker()"
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=parent) as temp:
            root, processes = Path(temp), []
            try:
                for index, job in enumerate(jobs):
                    processes.append(subprocess.Popen([
                        sys.executable, "-c", script, frappe.local.site,
                        str(Path(frappe.local.sites_path).resolve()), str(root), str(index), json.dumps(job),
                    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                timeout = time.monotonic() + 30
                while not all((root / f"{index}.ready").exists() for index in range(len(jobs))):
                    if time.monotonic() > timeout or any(process.poll() is not None for process in processes):
                        self.fail("independent survey connections did not reach the snapshot barrier")
                    time.sleep(0.05)
                (root / "start").write_text("start", encoding="ascii")
                results = []
                for process in processes:
                    output, error = process.communicate(timeout=40)
                    self.assertEqual(process.returncode, 0, msg=error[-2000:])
                    results.append(json.loads(output.strip().splitlines()[-1]))
                self.assertEqual(len({result["connection"] for result in results}), len(jobs))
                frappe.db.rollback()
                return results
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

    def test_same_person_concurrent_first_submissions_keep_one_current_response(self):
        doc = self.survey()
        results = self._race([
            {"action": "submit", "survey": doc.name, "user": self.users["one"], "answers": self.answer(doc, text=text)}
            for text in ("并发甲", "并发乙")
        ])
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(len({result["response"] for result in results}), 1)
        self.assertEqual(frappe.db.count("RG Survey Response", {"survey": doc.name}), 1)
        self.assertEqual(frappe.db.get_value("RG Survey Response", {"survey": doc.name}, "revision"), 2)

    def _revocation_race(self, doc, action, user, revocation, role=None):
        """Restore only the exact fictional account/member/role state we revoke."""
        role_rows = frappe.get_all("Has Role", filters={"parent": user, "parenttype": "User", "role": role},
                                   fields=["*"]) if revocation == "role" else []
        if revocation == "role":
            self.assertTrue(role_rows)
        original = None
        if revocation in {"account", "member"}:
            dt, field = ("User", "enabled") if revocation == "account" else ("RG Member", "active")
            original = frappe.db.get_value(dt, user, field)
        try:
            return self._race([
                {"action": "revoke", "survey": doc.name, "user": "Administrator", "target": user,
                 "revocation": revocation, "role": role, "hold": True, "wait_for": 1},
                {"action": action, "survey": doc.name, "user": user, "answers": self.answer(doc)},
            ])
        finally:
            frappe.set_user("Administrator")
            if revocation in {"account", "member"}:
                frappe.db.set_value(dt, user, field, original, update_modified=False)
            else:
                for row in role_rows:
                    if not frappe.db.exists("Has Role", row.name):
                        frappe.get_doc({"doctype": "Has Role", **row}).db_insert()
            frappe.db.commit()
            frappe.clear_cache(user=user)

    def test_waiting_submit_rechecks_current_account_member_and_role(self):
        for revocation in ("account", "member", "role"):
            with self.subTest(revocation=revocation):
                doc = self.survey()
                frappe.set_user(self.users["one"])
                first = submit_response(doc.name, self.answer(doc, text="撤权前保留内容"))
                results = self._revocation_race(doc, "submit", self.users["one"], revocation, STUDENT)
                self.assertEqual([result["ok"] for result in results], [True, False], results)
                self.assertEqual(results[1]["error"], "PermissionError")
                self.assertIn(STUDENT, results[1]["cached_roles"])
                stored = frappe.db.get_value("RG Survey Response", first["name"], ["revision", "answers_json"], as_dict=True)
                self.assertEqual(stored.revision, 1)
                self.assertIn("撤权前保留内容", stored.answers_json)

    def test_waiting_secretary_actions_recheck_current_secretary_role(self):
        for action in ("publish", "close", "results", "export", "edit"):
            with self.subTest(action=action):
                doc = self.survey(publish=action not in {"publish", "edit"})
                expected = doc.status
                results = self._revocation_race(doc, action, self.users["secretary"], "role", SECRETARY)
                self.assertEqual([result["ok"] for result in results], [True, False], results)
                self.assertEqual(results[1]["error"], "PermissionError")
                self.assertIn(SECRETARY, results[1]["cached_roles"])
                self.assertFalse(results[1]["has_filecontent"])
                self.assertEqual(frappe.db.get_value("RG Survey", doc.name, "status"), expected)
                self.assertEqual(frappe.db.get_value("RG Survey Question", doc.questions[0].name, "title"), "虚构单选")

    def test_waiting_member_reads_recheck_current_business_role(self):
        for action in ("read", "own"):
            with self.subTest(action=action):
                doc = self.survey()
                results = self._revocation_race(doc, action, self.users["one"], "role", STUDENT)
                self.assertEqual([result["ok"] for result in results], [True, False], results)
                self.assertEqual(results[1]["error"], "PermissionError")
                self.assertIn(STUDENT, results[1]["cached_roles"])

    def test_close_parent_lock_rejects_waiting_first_submission_and_edit(self):
        for existing in (False, True):
            doc = self.survey()
            if existing:
                frappe.set_user(self.users["one"])
                submit_response(doc.name, self.answer(doc, text="保留旧答卷"))
            results = self._race([
                {"action": "close", "survey": doc.name, "user": self.users["secretary"], "hold": True, "wait_for": 1},
                {"action": "submit", "survey": doc.name, "user": self.users["one"], "answers": self.answer(doc, text="被拒修改")},
            ])
            self.assertEqual([result["ok"] for result in results], [True, False], results)
            self.assertEqual(results[1]["error"], "ValidationError")
            self.assertEqual(frappe.db.count("RG Survey Response", {"survey": doc.name}), int(existing))
            self.assertEqual(frappe.db.get_value("RG Survey", doc.name, "status"), CLOSED)
            if existing:
                response = frappe.db.get_value("RG Survey Response", {"survey": doc.name}, ["revision", "answers_json"], as_dict=True)
                self.assertEqual(response.revision, 1)
                self.assertIn("保留旧答卷", response.answers_json)

    def test_submission_parent_lock_commits_before_waiting_close(self):
        doc = self.survey()
        results = self._race([
            {"action": "submit", "survey": doc.name, "user": self.users["one"], "answers": self.answer(doc), "hold": True, "wait_for": 1},
            {"action": "close", "survey": doc.name, "user": self.users["secretary"]},
        ])
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual(frappe.db.count("RG Survey Response", {"survey": doc.name}), 1)
        self.assertEqual(frappe.db.get_value("RG Survey", doc.name, "status"), CLOSED)

    def test_native_draft_edit_waits_for_publish_and_cannot_change_locked_questions(self):
        doc = self.survey(publish=False)
        results = self._race([
            {"action": "publish", "survey": doc.name, "user": self.users["secretary"], "hold": True, "wait_for": 1},
            {"action": "edit", "survey": doc.name, "user": self.users["secretary"]},
        ])
        self.assertEqual([result["ok"] for result in results], [True, False], results)
        self.assertEqual(frappe.db.get_value("RG Survey", doc.name, "status"), PUBLISHED)
        self.assertEqual(frappe.db.get_value("RG Survey Question", doc.questions[0].name, "title"), "虚构单选")


def survey_race_worker():
    site, sites_path, folder, worker_id, job_json = sys.argv[1:]
    job, root = json.loads(job_json), Path(folder)
    frappe.init(site, sites_path=sites_path)
    frappe.connect()
    try:
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        frappe.set_user(job["user"])
        snapshot = secure_get("RG Survey", job["survey"])
        cached_roles = list(frappe.get_roles())
        if job.get("hold"):
            frappe.get_doc("RG Survey", job["survey"], for_update=True)
        connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
        (root / f"{worker_id}.ready").write_text("snapshot ready", encoding="ascii")
        timeout = time.monotonic() + 30
        while not (root / "start").exists():
            if time.monotonic() > timeout:
                raise TimeoutError("survey snapshot barrier")
            time.sleep(0.05)
        if job.get("wait_for") is not None:
            while not (root / f"{job['wait_for']}.requested").exists():
                if time.monotonic() > timeout:
                    raise TimeoutError("contender did not request the parent lock")
                time.sleep(0.05)
        original_get_doc = frappe.get_doc

        def observe_parent(*args, **kwargs):
            if args[:2] == ("RG Survey", job["survey"]) and kwargs.get("for_update"):
                (root / f"{worker_id}.requested").write_text("parent requested", encoding="ascii")
            return original_get_doc(*args, **kwargs)

        try:
            extra = {}
            with patch.object(frappe, "get_doc", side_effect=observe_parent):
                if job["action"] == "submit":
                    response = submit_response(job["survey"], job["answers"])
                    extra = {"response": response["name"], "revision": response["revision"]}
                elif job["action"] == "close":
                    close_survey(job["survey"])
                elif job["action"] == "publish":
                    publish_survey(job["survey"])
                elif job["action"] == "results":
                    survey_results(job["survey"])
                elif job["action"] == "export":
                    export_csv(job["survey"])
                elif job["action"] == "read":
                    get_survey(job["survey"])
                elif job["action"] == "own":
                    my_response(job["survey"])
                elif job["action"] == "revoke":
                    if job["revocation"] == "account":
                        frappe.db.set_value("User", job["target"], "enabled", 0, update_modified=False)
                    elif job["revocation"] == "member":
                        frappe.db.set_value("RG Member", job["target"], "active", 0, update_modified=False)
                    else:
                        # Deliberately retain the contender's cached roles.
                        frappe.db.delete("Has Role", {"parent": job["target"], "parenttype": "User", "role": job["role"]})
                else:
                    snapshot.questions[0].title = "不应覆盖的题目"
                    snapshot.save()
            frappe.db.commit()
            result = {"ok": True, "connection": connection, "snapshot_status": snapshot.status,
                      "cached_roles": cached_roles, "has_filecontent": "filecontent" in frappe.response, **extra}
        except (frappe.ValidationError, frappe.PermissionError) as error:
            frappe.db.rollback()
            result = {"ok": False, "connection": connection, "snapshot_status": snapshot.status,
                      "error": type(error).__name__, "message": str(error), "cached_roles": cached_roles,
                      "has_filecontent": "filecontent" in frappe.response}
        print(json.dumps(result))
    finally:
        frappe.destroy()
