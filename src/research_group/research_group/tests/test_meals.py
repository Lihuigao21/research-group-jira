"""Meal contracts, persistence boundaries, exports and independent connections."""
import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import frappe
from frappe.client import get as native_get
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, now_datetime

from research_group import meals
from research_group.policy import DRAFT, PUBLISHED, SECRETARY, STUDENT
from research_group.tests import test_core as core


def rice(quantity=1):
    return [{"id": "meal-1", "quantity": quantity, "staple": "白米饭"}]


class TestMeals(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-meal-test-" + frappe.generate_hash(length=10)
        self.users = core.create_members(self.prefix)
        self.committed_fixture = False

    def tearDown(self):
        frappe.set_user("Administrator")
        if self.committed_fixture:
            rounds = frappe.get_all("RG Meal Round", filters={"publisher": self.users["secretary"]}, pluck="name")
            orders = frappe.get_all("RG Meal Order", filters={"round": ["in", rounds]}, pluck="name") if rounds else []
            # Service tables have intentionally undeletable native controllers;
            # delete only the exact, fictional fixture identities directly.
            if orders:
                frappe.db.delete("RG Meal Order Item", {"parent": ["in", orders]})
                frappe.db.delete("RG Meal Order", {"name": ["in", orders]})
            if rounds:
                frappe.db.delete("RG Meal Item", {"parent": ["in", rounds]})
                frappe.db.delete("RG Meal Round", {"name": ["in", rounds]})
            core.TestResearchGroupCore._delete_fixture(self)
            frappe.db.commit()
        else:
            frappe.db.rollback()
        frappe.clear_cache()
        super().tearDown()

    def round(self, publish=True, **values):
        frappe.set_user(self.users["secretary"])
        doc = meals.create_round(self.prefix + "虚构工作餐", **values)
        return meals.publish_round(doc["name"], doc["revision"]) if publish else doc

    def submit(self, rnd, items=None, **values):
        frappe.set_user(self.users["one"])
        return meals.submit_order(rnd["name"], rice() if items is None else items,
                                  request_id=values.pop("request_id", frappe.generate_hash(length=16)), **values)

    def test_default_7_plus_7_complete_details_and_copied_images(self):
        frappe.set_user(self.users["one"])
        menu = meals.default_menu()
        self.assertEqual(menu["budgetCents"], 5000)
        mains = [row for row in menu["items"] if row["category"] == "meal"]
        extras = [row for row in menu["items"] if row["category"] == "addon"]
        self.assertEqual((len(mains), len(extras)), (7, 7))
        self.assertIn("青椒炒鸡腿肉", mains[2]["description"])
        self.assertIn("6种蔬菜", mains[3]["description"])
        self.assertEqual(mains[6]["fixedStaple"], "鸡蛋面")
        for index, item in enumerate(mains, 1):
            self.assertTrue(item["imageUrl"].endswith(f"meal-{index}.jpg"))
            path = Path(meals.__file__).parent / "public" / "menu" / "2026-11-07" / f"meal-{index}.jpg"
            self.assertTrue(path.read_bytes().startswith(b"\xff\xd8"))

    def test_secretary_drafts_publish_freeze_and_native_write_denied(self):
        rnd = self.round(publish=False)
        for user in (self.users["one"], self.users["teacher"]):
            frappe.set_user(user)
            with self.assertRaises(frappe.PermissionError):
                meals.get_round(rnd["name"])
            with self.assertRaises(frappe.PermissionError):
                meals.create_round("越权")
        frappe.set_user(self.users["secretary"])
        menu = rnd["menu"]
        menu["items"][0]["name"] = "秘书改名的主餐"
        updated = meals.update_draft(rnd["name"], rnd["revision"], menu=menu)
        published = meals.publish_round(rnd["name"], updated["revision"])
        self.assertEqual(published["menu"]["items"][0]["name"], "秘书改名的主餐")
        self.assertEqual(len(published["menu_sha256"]), 64)
        with self.assertRaises(frappe.ValidationError):
            meals.update_draft(rnd["name"], published["revision"], title="覆盖")
        direct = frappe.get_doc("RG Meal Round", rnd["name"])
        direct.title = "原生覆盖"
        with self.assertRaises(frappe.PermissionError):
            direct.save(ignore_permissions=True)
        child = frappe.get_doc("RG Meal Item", direct.get("items")[0].name)
        child.price_cents = 1
        with self.assertRaises(frappe.PermissionError):
            child.save(ignore_permissions=True)
        copied = meals.create_round(self.prefix + "复制", copy_from=rnd["name"])
        self.assertEqual(copied["menu"], published["menu"])

    def test_plain_text_menu_order_hash_idempotency_and_csv_survive_saving(self):
        # Names already present in the member table must be copied as text.
        meals.global_lock()
        member_name = "=虚构 <literal> & 姓名"
        frappe.db.set_value("RG Member", self.users["one"], "display_name", member_name)
        frappe.set_user(self.users["secretary"])
        title = "=工作餐 <literal> & " + self.prefix
        menu = meals.default_menu()
        menu["version"] = "version <literal> & exact"
        main, fixed = menu["items"][0], menu["items"][6]
        main["name"] = "=主餐 <literal> & 完整菜名"
        main["description"] = "配菜 <literal> &, 第一行\n完整说明第二行"
        main["staples"] = ["@主食 <literal> & 完整选项", "其他主食"]
        fixed["name"], fixed["fixedStaple"] = "固定餐 <literal> &", "固定主食 <literal> &"
        draft = meals.create_round(title, menu=menu)
        self.assertEqual((draft["title"], draft["menu"]), (title, meals.validate_menu(menu)))
        stored = frappe.get_doc("RG Meal Round", draft["name"])
        self.assertEqual(stored.menu_version, menu["version"])
        self.assertEqual(stored.items[0].description, main["description"])
        self.assertEqual(json.loads(stored.items[0].staples_json), main["staples"])
        self.assertEqual(stored.items[6].fixed_staple, fixed["fixedStaple"])
        snapshot = meals._canonical(draft["menu"])
        published = meals.publish_round(draft["name"], draft["revision"])
        self.assertEqual((published["title"], published["menu"]), (title, draft["menu"]))
        stored = frappe.get_doc("RG Meal Round", draft["name"])
        self.assertEqual(stored.menu_snapshot.encode("utf-8"), snapshot.encode("utf-8"))
        expected_menu_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        self.assertEqual(stored.menu_sha256, expected_menu_hash)
        self.assertEqual(published["menu_sha256"], expected_menu_hash)
        items = [{"id": "meal-1", "quantity": 1, "staple": main["staples"][0]},
                 {"id": "meal-7", "quantity": 1}]
        remarks = "=备注 <literal> &, 第一行\n完整备注第二行"
        payload = meals.canonical_order(published["menu"], items, remarks, True)
        values = dict(remarks=remarks, budget_acknowledged=True, request_id="plaintext-request-01")
        first = self.submit(published, items, **values)
        replay = self.submit(published, items, **values)
        self.assertEqual((first["name"], first["revision"], first["items"]),
                         (replay["name"], replay["revision"], replay["items"]))
        self.assertEqual(frappe.db.count("RG Meal Order", {"round": published["name"]}), 1)
        self.assertEqual(frappe.db.count("RG Meal Order Item", {"parent": first["name"]}), 2)
        own = meals.get_round(published["name"])["my_order"]
        self.assertEqual((own["member_name"], own["remarks"]), (member_name, remarks))
        self.assertEqual([(row["name"], row["staple"]) for row in own["items"]],
                         [(main["name"], main["staples"][0]), (fixed["name"], fixed["fixedStaple"])])
        saved_order = frappe.get_doc("RG Meal Order", first["name"])
        self.assertEqual(saved_order.menu_sha256, expected_menu_hash)
        self.assertEqual(saved_order.payload_sha256,
                         hashlib.sha256(meals._canonical(payload).encode("utf-8")).hexdigest())
        frappe.set_user(self.users["secretary"])
        meals.export_csv(published["name"])
        content = frappe.response.pop("filecontent")
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][1], "'" + title)
        self.assertEqual(rows[1][4], "'" + member_name)
        self.assertEqual(rows[1][5], "'" + main["name"])
        self.assertEqual(rows[1][6], "'" + main["staples"][0])
        self.assertEqual(rows[1][12], "'" + remarks)
        self.assertEqual((rows[2][5], rows[2][6]), (fixed["name"], fixed["fixedStaple"]))
        reason = "=作废 <literal> & 仍须保存"
        voided = meals.void_order(first["name"], reason, first["revision"])
        self.assertEqual((voided["void_reason"], voided["remarks"], voided["items"]),
                         (reason, remarks, first["items"]))
        meals.export_csv(published["name"])
        rows = list(csv.reader(StringIO(frappe.response.pop("filecontent").decode("utf-8-sig"))))
        self.assertTrue(all(row[18] == "'" + reason for row in rows[1:]))
        direct = frappe.get_doc("RG Meal Order", first["name"])
        direct.remarks = "原生接口不能改写 <literal> &"
        with self.assertRaises(frappe.PermissionError):
            direct.save(ignore_permissions=True)

    def test_49_50_51_strict_budget_and_integer_server_pricing(self):
        rnd = self.round()
        base = rice() + [{"id": "addon-4", "quantity": 2}, {"id": "addon-1", "quantity": 2}]
        first = self.submit(rnd, base)
        self.assertEqual(first["total_cents"], 4900)
        self.assertFalse(first["over_budget"])
        second = self.submit(rnd, rice(2), expected_revision=first["revision"])
        self.assertEqual(second["total_cents"], 5000)
        self.assertFalse(second["over_budget"])
        base[-1]["quantity"] = 3
        with self.assertRaises(frappe.ValidationError):
            self.submit(rnd, base, expected_revision=second["revision"])
        third = self.submit(rnd, base, expected_revision=second["revision"], budget_acknowledged=True)
        self.assertEqual(third["total_cents"], 5100)
        self.assertTrue(third["over_budget"])
        self.assertTrue(third["budget_acknowledged_at"])
        self.assertEqual(third["name"], first["name"])
        for acknowledged in (1, "1", [], None):
            with self.subTest(acknowledged=acknowledged), self.assertRaises(frappe.ValidationError):
                meals.canonical_order(rnd["menu"], base, budget_acknowledged=acknowledged)

    def test_invalid_items_and_failed_edit_preserve_valid_order(self):
        rnd = self.round()
        first = self.submit(rnd)
        invalid = [[], [{"id": "addon-1", "quantity": 1}], rice() * 2,
                   [{"id": "meal-1", "quantity": 1, "staple": "不存在"}],
                   [{"id": "meal-7", "quantity": 1, "staple": "白米饭"}],
                   [{"id": "addon-1", "quantity": 1, "staple": "白米饭"}] + rice(),
                   [{"id": "meal-1", "quantity": 1, "staple": "白米饭", "priceCents": 1}]]
        invalid += [[{"id": "meal-1", "quantity": value, "staple": "白米饭"}]
                    for value in (0, 21, 1.1, True, "1")]
        for items in invalid:
            with self.subTest(items=items), self.assertRaises(frappe.ValidationError):
                self.submit(rnd, items, expected_revision=first["revision"], budget_acknowledged=True)
        with self.assertRaises(frappe.ValidationError):
            self.submit(rnd, remarks="😀" * 301, expected_revision=first["revision"])
        current = meals.get_round(rnd["name"])["my_order"]
        self.assertEqual((current["revision"], current["total_cents"]), (1, 2500))
        valid = self.submit(rnd, remarks="😀" * 300, expected_revision=first["revision"])
        self.assertEqual(valid["remarks"], "😀" * 300)

    def test_idempotency_revision_cross_device_and_private_order_recovery(self):
        rnd = self.round()
        first = self.submit(rnd, request_id="first-request-01")
        again = self.submit(rnd, request_id="first-request-01")
        self.assertEqual((first["name"], first["revision"]), (again["name"], again["revision"]))
        with self.assertRaises(frappe.ValidationError):
            self.submit(rnd, remarks="复用请求改内容", request_id="first-request-01")
        with self.assertRaises(frappe.TimestampMismatchError):
            self.submit(rnd, remarks="旧浏览器", expected_revision=0)
        second = self.submit(rnd, remarks="另一个设备", expected_revision=1)
        self.assertEqual(second["name"], first["name"])
        self.assertEqual(second["submitted_at"], first["submitted_at"])
        self.assertEqual(meals.get_round(rnd["name"])["my_order"]["remarks"], "另一个设备")
        frappe.set_user(self.users["two"])
        self.assertIsNone(meals.get_round(rnd["name"])["my_order"])
        with self.assertRaises(frappe.PermissionError):
            meals.round_results(rnd["name"])
        for dt, name in (("RG Meal Round", rnd["name"]), ("RG Meal Order", first["name"])):
            self.assertFalse(frappe.has_permission(dt, "read", doc=name))
            with self.assertRaises(frappe.PermissionError):
                native_get(dt, name)
        self.assertEqual(frappe.db.count("RG Meal Order", {"round": rnd["name"]}), 1)

    def test_deadline_equality_close_reopen_and_recover_committed_receipt(self):
        deadline = now_datetime() + timedelta(hours=1)
        rnd = self.round(deadline=deadline)
        first = self.submit(rnd, request_id="deadline-receipt-01")
        with patch.object(meals, "now_datetime", return_value=deadline):
            with self.assertRaises(frappe.ValidationError):
                self.submit(rnd, remarks="截止修改", expected_revision=1)
            recovered = self.submit(rnd, request_id="deadline-receipt-01")
            self.assertEqual(recovered["name"], first["name"])
            self.assertFalse(meals.get_round(rnd["name"])["accepting_orders"])
        frappe.set_user(self.users["secretary"])
        closed = meals.set_round_status(rnd["name"], True, rnd["revision"])
        with self.assertRaises(frappe.ValidationError):
            self.submit(rnd, remarks="关闭修改", expected_revision=1)
        frappe.set_user(self.users["secretary"])
        with patch.object(meals, "now_datetime", return_value=deadline):
            with self.assertRaises(frappe.ValidationError):
                meals.set_round_status(rnd["name"], False, closed["revision"])

    def test_void_keeps_history_stats_csv_formula_and_multiround_prices(self):
        rnd = self.round()
        first = self.submit(rnd, remarks="=FORMULA()")
        frappe.set_user(self.users["secretary"])
        menu = rnd["menu"]
        menu["items"][0]["priceCents"] = 2600
        other = self.round(menu=menu)
        second = self.submit(other, remarks="第二轮")
        frappe.set_user(self.users["secretary"])
        original = meals.round_results(rnd["name"])
        self.assertEqual(original["items"][0]["quantity"], 1)
        self.assertEqual(original["items"][0]["staples"], {"白米饭": 1})
        meals.export_csv(rnd["name"])
        content = frappe.response.pop("filecontent")
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertTrue(content.decode("utf-8-sig").startswith('"轮次","标题"'))
        rows = list(csv.reader(StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(rows[1][12], "'=FORMULA()")
        meals.export_zip([rnd["name"], other["name"]])
        with ZipFile(BytesIO(frappe.response.pop("filecontent"))) as archive:
            self.assertEqual(set(archive.namelist()), {"逐人总表.csv", "按轮次菜品汇总.csv", "商家订单.txt"})
            aggregate = list(csv.reader(StringIO(archive.read("按轮次菜品汇总.csv").decode("utf-8-sig"))))
            self.assertEqual({row[5] for row in aggregate[1:]}, {"2500", "2600"})
        voided = meals.void_order(first["name"], "虚构误填作废", first["revision"])
        self.assertEqual(voided["items"], first["items"])
        result = meals.round_results(rnd["name"])
        self.assertEqual((result["order_count"], result["total_cents"]), (0, 0))
        self.assertEqual(result["voided_orders"][0]["voided_by"], self.users["secretary"])
        self.assertEqual(meals.round_results(other["name"])["total_cents"], second["total_cents"])
        with self.assertRaises(frappe.ValidationError):
            self.submit(rnd, expected_revision=voided["revision"])
        frappe.set_user(self.users["secretary"])
        for ids in ([], [rnd["name"], rnd["name"]], [str(i) for i in range(51)]):
            with self.assertRaises(frappe.ValidationError):
                meals.export_zip(ids)

    def test_savepoint_failure_has_no_half_order_or_children(self):
        rnd = self.round()
        child_count = frappe.db.count("RG Meal Order Item")
        with patch.object(meals.RGMealOrder, "on_update", side_effect=RuntimeError("injected persistence failure"), create=True):
            with self.assertRaises(RuntimeError):
                self.submit(rnd)
        self.assertEqual(frappe.db.count("RG Meal Order", {"round": rnd["name"]}), 0)
        self.assertEqual(frappe.db.count("RG Meal Order Item"), child_count)
        self.submit(rnd)

    def test_export_accepts_50_distinct_empty_rounds_and_rejects_51(self):
        rounds = [self.round(publish=False) for _ in range(50)]
        frappe.set_user(self.users["secretary"])
        meals.export_zip([rnd["name"] for rnd in rounds])
        with ZipFile(BytesIO(frappe.response.pop("filecontent"))) as archive:
            people = list(csv.reader(StringIO(archive.read("逐人总表.csv").decode("utf-8-sig"))))
            self.assertEqual(len(people), 1)
            self.assertEqual(archive.read("商家订单.txt").decode("utf-8").count("订单 0 人"), 50)
        extra = self.round(publish=False)
        with self.assertRaises(frappe.ValidationError):
            meals.export_zip([rnd["name"] for rnd in rounds] + [extra["name"]])

    def _race(self, jobs):
        frappe.db.commit()
        self.committed_fixture = True
        parent = Path(os.environ.get("RG_MEAL_TEST_WORKDIR") or frappe.get_site_path(
            "private", "tasks", "20261108-fdu-accounts-meals", "scratch", "meal-races")).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        script = "from research_group.tests.test_meals import meal_race_worker; meal_race_worker()"
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=parent) as folder:
            root, processes = Path(folder), []
            try:
                for index, job in enumerate(jobs):
                    processes.append(subprocess.Popen([
                        sys.executable, "-c", script, frappe.local.site, str(Path(frappe.local.sites_path).resolve()),
                        str(root), str(index), json.dumps(job),
                    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                deadline = time.monotonic() + 30
                while not all((root / f"{index}.ready").exists() for index in range(len(jobs))):
                    if time.monotonic() > deadline or any(proc.poll() is not None for proc in processes):
                        self.fail("meal independent workers did not reach the snapshot barrier")
                    time.sleep(0.03)
                (root / "start").write_text("start", encoding="ascii")
                results = []
                for process in processes:
                    output, error = process.communicate(timeout=40)
                    self.assertEqual(process.returncode, 0, msg=error[-3000:])
                    results.append(json.loads(output.strip().splitlines()[-1]))
                self.assertEqual(len({row["connection"] for row in results}), len(jobs))
                frappe.db.rollback()
                return results
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

    def test_independent_duplicate_first_submissions_have_one_order(self):
        rnd = self.round()
        results = self._race([{"action": "submit", "round": rnd["name"], "user": self.users["one"],
                               "request_id": "concurrent-request-01"} for _ in range(2)])
        self.assertTrue(all(row["ok"] for row in results), results)
        self.assertEqual(len({row["order"] for row in results}), 1)
        self.assertEqual(frappe.db.count("RG Meal Order", {"round": rnd["name"]}), 1)
        self.assertEqual(frappe.db.get_value("RG Meal Order", {"round": rnd["name"]}, "revision"), 1)

    def test_independent_waiting_submit_rechecks_close_and_identity(self):
        for action in ("close", "member", "account", "role"):
            with self.subTest(action=action):
                rnd = self.round()
                original = self.submit(rnd)
                results = self._race([
                    {"action": action, "round": rnd["name"], "user": self.users["secretary"] if action == "close"
                     else "Administrator", "target": self.users["one"], "revision": rnd["revision"], "hold": True},
                    {"action": "submit", "round": rnd["name"], "user": self.users["one"], "revision": 1,
                     "remarks": "不应覆盖的等待修改", "request_id": "waiting-request-01"},
                ])
                self.assertEqual([row["ok"] for row in results], [True, False], results)
                self.assertEqual(results[1]["error"], "ValidationError" if action == "close" else "PermissionError")
                self.assertEqual(frappe.db.get_value("RG Meal Order", original["name"], "revision"), 1)
                # Restore only our fictional identity for the next scenario.
                frappe.set_user("Administrator")
                member = frappe.get_doc("RG Member", self.users["one"])
                member.active = 1
                member.save(ignore_permissions=True)
                user = frappe.get_doc("User", self.users["one"])
                user.enabled = 1
                if not any(row.role == STUDENT for row in user.roles):
                    user.append("roles", {"role": STUDENT})
                user.save(ignore_permissions=True)
                frappe.db.commit()
                frappe.clear_cache(user=self.users["one"])


def meal_race_worker():
    site, sites_path, folder, worker_id, job_json = sys.argv[1:]
    root, job = Path(folder), json.loads(job_json)
    frappe.init(site, sites_path=sites_path)
    frappe.connect()
    frappe.in_test = True
    try:
        frappe.set_user(job["user"])
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # Deliberately establish a stale ordinary-read snapshot and cached roles.
        frappe.db.get_value("RG Meal Round", job["round"], "status")
        frappe.get_roles()
        if job.get("hold"):
            meals.global_lock()
        connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
        (root / f"{worker_id}.ready").write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not (root / "start").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("meal barrier")
            time.sleep(0.03)
        if job.get("hold"):
            while not (root / "1.requested").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("meal contender did not request gate")
                time.sleep(0.03)
        original_lock = meals.global_lock

        def observed_gate():
            (root / f"{worker_id}.requested").write_text("requested", encoding="ascii")
            return original_lock()

        try:
            extra = {}
            with patch.object(meals, "global_lock", side_effect=observed_gate):
                if job["action"] == "submit":
                    order = meals.submit_order(job["round"], rice(), remarks=job.get("remarks", ""),
                                               expected_revision=job.get("revision", 0), request_id=job["request_id"])
                    extra = {"order": order["name"], "revision": order["revision"]}
                elif job["action"] == "close":
                    meals.set_round_status(job["round"], True, job["revision"])
                elif job["action"] == "member":
                    member = frappe.get_doc("RG Member", job["target"])
                    member.active = 0
                    member.save(ignore_permissions=True)
                else:
                    user = frappe.get_doc("User", job["target"])
                    if job["action"] == "account":
                        user.enabled = 0
                    else:
                        user.set("roles", [row for row in user.roles if row.role != STUDENT])
                    user.save(ignore_permissions=True)
            frappe.db.commit()
            result = {"ok": True, "connection": connection, **extra}
        except (frappe.ValidationError, frappe.PermissionError, frappe.TimestampMismatchError) as error:
            frappe.db.rollback()
            result = {"ok": False, "connection": connection, "error": type(error).__name__}
        print(json.dumps(result))
    finally:
        frappe.destroy()
