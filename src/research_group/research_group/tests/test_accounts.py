"""Account lifecycle, private secrets, rollback and independent-connection races."""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime
from frappe.utils.password import check_password, update_password as hash_password

from research_group import accounts, notifications
from research_group.policy import SECRETARY, STUDENT, TEACHER
from research_group.tests import test_core as core

PASSWORD = "Violet!9-River@2026-Quartz#73"
NEXT_PASSWORD = "Maple@47!Orchid-9-Cloud#382"


def secret_from_link(link, name):
    parsed = urlsplit(link)
    return parse_qs(parsed.fragment)[name][0]


class TestAccounts(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-acct-test-" + frappe.generate_hash(length=10)
        self.users = core.create_members(self.prefix)
        self.student_id = ("ACC" + frappe.generate_hash(length=12)).upper()
        self.email = self.student_id.lower() + "@m.fudan.edu.cn"
        self.committed_fixture = False
        self.session_ids = []
        self.mail_calls = []
        self.throttle_patch = patch.object(accounts, "_throttle")
        self.schedule_patch = patch.object(notifications, "_schedule")
        self.throttle_patch.start()
        self.schedule_patch.start()
        original = accounts._mail

        def observe(*args, **kwargs):
            self.mail_calls.append((args, kwargs))
            return original(*args, **kwargs)

        self.mail_patch = patch.object(accounts, "_mail", side_effect=observe)
        self.mail_patch.start()

    def tearDown(self):
        self.mail_patch.stop()
        self.schedule_patch.stop()
        self.throttle_patch.stop()
        frappe.set_user("Administrator")
        for sid in self.session_ids:
            frappe.cache.hdel("session", sid)
        if self.committed_fixture:
            identities = [*self.users.values(), self.email]
            frappe.db.delete("Sessions", {"user": ["in", identities]})
            registrations = frappe.get_all("RG Registration", filters={"student_id": self.student_id}, pluck="name")
            profiles = frappe.get_all("RG Account Profile", filters={"user": ["in", identities]}, pluck="name")
            related = [*identities, *registrations, *profiles]
            if related:
                frappe.db.delete("RG Mail Event", {"related_name": ["in", related]})
            frappe.db.delete("RG Mail Event", {"actor": ["in", identities]})
            frappe.db.delete("RG Account Audit", {"target_user": ["in", identities]})
            if registrations:
                frappe.db.delete("RG Account Audit", {"registration": ["in", registrations]})
                frappe.db.delete("RG Registration", {"name": ["in", registrations]})
            frappe.db.delete("RG Account Profile", {"user": ["in", identities]})
            for dt in ("RG AI Conversation", "RG AI Message", "RG AI Memory", "RG AI Plan", "RG AI Operation",
                       "RG AI Run", "RG AI Tombstone", "RG AI State"):
                frappe.db.delete(dt, {"private_user": ["in", identities]})
            if frappe.db.exists("RG Member", self.email):
                frappe.delete_doc("RG Member", self.email, ignore_permissions=True, force=True)
            if frappe.db.exists("User", self.email):
                frappe.delete_doc("User", self.email, ignore_permissions=True, force=True)
            core.TestResearchGroupCore._delete_fixture(self)
            frappe.db.commit()
        else:
            frappe.db.rollback()
        frappe.clear_cache()
        super().tearDown()

    def registration(self, verify=True):
        frappe.set_user("Guest")
        result = accounts.request_registration("虚构入组测试", self.student_id, self.email)
        self.assertNotIn("name", result)
        name = frappe.db.get_value("RG Registration", {"email": self.email}, "name")
        link = self.mail_calls[-1][1]["values"]["link"]
        token = secret_from_link(link, "token")
        if verify:
            accounts.verify_registration(name, token)
        return name, token

    def approved(self):
        name, _ = self.registration()
        frappe.set_user(self.users["secretary"])
        accounts.review_registration(name, "approve")
        link = self.mail_calls[-1][1]["values"]["link"]
        key = secret_from_link(link, "key")
        return name, key

    def profile(self, key="one"):
        user = self.users[key]
        return accounts._insert("RG Account Profile", user=user, student_id=self.student_id,
                                contact_email=self.email, contact_verified=1, email_version=1)

    def test_non_group_password_endpoints_delegate_native_without_group_guest_csrf(self):
        from frappe.core.doctype.user import user as native_user
        frappe.set_user("Administrator")
        native = frappe.get_doc({"doctype": "User", "email": self.email, "first_name": self.prefix,
            "enabled": 1, "user_type": "System User", "send_welcome_email": 0})
        native.flags.no_welcome_mail = True
        native.insert(ignore_permissions=True)
        link = native._reset_password(send_email=False)
        key = parse_qs(urlsplit(link).query)["key"][0]
        frappe.set_user("Guest")
        with patch.object(native_user, "reset_password", return_value=None) as reset, \
                patch.object(native_user, "update_password", return_value="/native-login") as update, \
                patch.object(accounts, "browser_write", side_effect=AssertionError("group csrf must not replace native")):
            self.assertEqual(accounts.request_password_reset(self.email), {"ok": True, "message": accounts.GENERIC})
            reset.assert_called_once_with(self.email)
            self.assertEqual(accounts.update_password(PASSWORD, key=key), "/native-login")
            update.assert_called_once_with(PASSWORD, logout_all_sessions=0, key=key, old_password=None)
            update.reset_mock()
            frappe.set_user(self.email)
            self.assertEqual(accounts.update_password(NEXT_PASSWORD, old_password=PASSWORD), "/native-login")
            update.assert_called_once_with(NEXT_PASSWORD, logout_all_sessions=0, key=None, old_password=PASSWORD)

    def test_group_without_verified_contact_never_falls_back_to_native_reset(self):
        from frappe.core.doctype.user import user as native_user
        before = frappe.db.get_value("User", self.users["one"], "reset_password_key")
        frappe.set_user("Guest")
        with patch.object(native_user, "reset_password", side_effect=AssertionError("native group bypass")):
            result = accounts.request_password_reset(self.users["one"])
        self.assertEqual(result, {"ok": True, "message": accounts.GENERIC})
        self.assertEqual(frappe.db.get_value("User", self.users["one"], "reset_password_key"), before)

    def test_native_recovery_feedback_cannot_distinguish_outer_response_shape(self):
        from frappe.core.doctype.user import user as native_user
        frappe.set_user("Guest")
        def native_feedback(user):
            frappe.msgprint("Native generic feedback")
        with patch.object(native_user, "reset_password", side_effect=native_feedback):
            result = accounts.request_password_reset(self.email)
        self.assertEqual(result, {"ok": True, "message": accounts.GENERIC})
        self.assertFalse(frappe.local.message_log)

    def test_zero_native_key_expiry_is_unlimited_for_queued_mail_and_consumption(self):
        original = frappe.get_system_settings
        def native_setting(key):
            return 0 if key == "reset_password_link_expiry_duration" else original(key)
        with patch.object(frappe, "get_system_settings", side_effect=native_setting):
            _, key = self.approved()
            event = frappe.get_doc("RG Mail Event", {"related_doctype": "User", "related_name": self.email})
            self.assertFalse(event.expires_at)
            self.assertIsNone(accounts.password_link_expires(accounts._user_row(self.email)))
            future = now_datetime() + timedelta(days=2)
            with patch.object(accounts, "now_datetime", return_value=future), \
                    patch.object(notifications, "now_datetime", return_value=future):
                self.assertEqual(notifications._check_event(event.as_dict()), "")
                frappe.set_user("Guest")
                result = accounts.update_password(NEXT_PASSWORD, key=key)
            self.assertEqual(result, "/login")
            self.assertFalse(frappe.db.get_value("User", self.email, "reset_password_key"))

    def test_rejection_releases_reservations_and_preserves_review_and_old_token_denial(self):
        name, token = self.registration()
        frappe.set_user(self.users["secretary"])
        accounts.review_registration(name, "reject", "虚构身份未核实")
        rejected = frappe.get_doc("RG Registration", name)
        self.assertEqual(rejected.status, "rejected")
        self.assertFalse(rejected.reserved_email)
        self.assertFalse(rejected.reserved_student_id)
        self.assertEqual((rejected.reviewed_by, rejected.review_note),
                         (self.users["secretary"], "虚构身份未核实"))
        self.assertTrue(rejected.reviewed_at)
        self.assertTrue(frappe.db.exists("RG Account Audit", {"registration": name, "action": "registration_reject"}))
        frappe.set_user("Guest")
        with self.assertRaises(frappe.ValidationError):
            accounts.verify_registration(name, token)
        legitimate = "legitimate-" + self.student_id.lower() + "@fudan.edu.cn"
        accounts.request_registration("重新核实的申请人", self.student_id, legitimate)
        replacement = frappe.db.get_value("RG Registration", {"email": legitimate}, "name")
        self.assertTrue(replacement)
        self.assertNotEqual(replacement, name)
        self.assertEqual(frappe.db.get_value("RG Registration", name, "status"), "rejected")
        self.assertFalse(frappe.db.exists("User", legitimate))

    def test_rejected_legacy_reservation_is_released_before_new_unique_insert(self):
        name, _ = self.registration()
        frappe.set_user(self.users["secretary"])
        accounts.review_registration(name, "reject")
        # A historical pre-upgrade rejection retained these unique fields.
        frappe.db.set_value("RG Registration", name, {"reserved_email": self.email,
                                                     "reserved_student_id": self.student_id})
        frappe.set_user("Guest")
        accounts.request_registration("新申请", self.student_id, self.email)
        self.assertEqual(frappe.db.count("RG Registration", {"student_id": self.student_id}), 2)
        self.assertFalse(frappe.db.get_value("RG Registration", name, "reserved_student_id"))
        self.assertTrue(frappe.db.exists("RG Account Audit", {
            "registration": name, "action": "registration_reservation_released"}))

    def test_repeated_registration_reservation_migration_preserves_rejected_and_expired_history(self):
        from research_group.setup import migrate_registration_reservations

        expired, _ = self.registration(verify=False)
        frappe.db.set_value("RG Registration", expired, "expires_at", now_datetime() - timedelta(seconds=1))
        accounts.request_registration("重新验证的申请人", self.student_id, self.email)
        rejected = frappe.db.get_value("RG Registration", {"email": self.email,
            "student_id": self.student_id, "status": "pending_verification"}, "name")
        self.assertNotEqual(rejected, expired)
        token = secret_from_link(self.mail_calls[-1][1]["values"]["link"], "token")
        accounts.verify_registration(rejected, token)
        frappe.set_user(self.users["secretary"])
        accounts.review_registration(rejected, "reject", "保留原审核记录")
        history = {name: frappe.get_doc("RG Registration", name).as_dict() for name in (expired, rejected)}
        self.assertEqual((history[expired].status, history[rejected].status), ("expired", "rejected"))
        for row in history.values():
            self.assertFalse(row.reserved_email)
            self.assertFalse(row.reserved_student_id)
        audit_filters = {"registration": ["in", [expired, rejected]]}
        audits = frappe.get_all("RG Account Audit", filters=audit_filters, fields=["*"], order_by="name")
        self.assertIn("registration_reject", {row.action for row in audits})
        self.assertIn("registration_expired", {row.action for row in audits})

        frappe.set_user("Guest")
        accounts.request_registration("合法重新申请人", self.student_id, self.email)
        replacement = frappe.get_doc("RG Registration", {"email": self.email,
            "student_id": self.student_id, "status": "pending_verification"})
        self.assertNotIn(replacement.name, history)
        frappe.set_user("Administrator")
        for _ in range(2):
            migrate_registration_reservations()
            replacement.reload()
            self.assertEqual((replacement.reserved_email, replacement.reserved_student_id),
                             (self.email, self.student_id))
            for name, original in history.items():
                self.assertEqual(frappe.get_doc("RG Registration", name).as_dict(), original)
            self.assertEqual(frappe.get_all("RG Account Audit", filters=audit_filters,
                                           fields=["*"], order_by="name"), audits)

    def test_password_event_expiry_matches_native_key_and_expired_key_is_not_mailed(self):
        _, key = self.approved()
        event = frappe.get_doc("RG Mail Event", {"related_doctype": "User", "related_name": self.email})
        generated = accounts._user_row(self.email)
        self.assertEqual(accounts.get_datetime(event.expires_at), accounts.password_link_expires(generated))
        self.assertTrue(key)
        frappe.db.set_value("User", self.email, "last_reset_password_key_generated_on",
                            now_datetime() - timedelta(days=1))
        frappe.db.commit()
        self.committed_fixture = True
        with patch.object(notifications, "_smtp_session") as transport:
            notifications.process_event(event.name)
            transport.assert_not_called()
        stored = frappe.get_doc("RG Mail Event", event.name)
        self.assertEqual((stored.status, stored.error_class), ("cancelled", "authentication_key_changed"))

    def test_exact_fdu_domains_and_address_deception(self):
        for value in ("A123@m.fudan.edu.cn", "A123@fudan.edu.cn"):
            self.assertEqual(accounts.normalize_email(value), value.lower())
        for value in ("a@evilfudan.edu.cn", "a@fudan.edu.cn.evil", "a@sub.fudan.edu.cn",
                      "A <a@fudan.edu.cn>", "a@fudan.edu.cn\r\nBcc:x@fudan.edu.cn", "a@fudan。edu.cn"):
            with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
                accounts.normalize_email(value)

    def test_guest_csrf_origin_method_and_field_whitelist(self):
        frappe.set_user("Guest")
        nonce = frappe.generate_hash(length=40)
        key = accounts._guest_csrf_key(nonce)
        frappe.cache.set_value(key, True, expires_in_sec=60)
        request = SimpleNamespace(method="POST", host_url="http://localhost:8088/",
            headers={"Origin": "http://localhost:8088", "X-RG-CSRF-Token": nonce},
            cookies={"rg_account_csrf": nonce})
        try:
            with patch.object(frappe.local, "request", request, create=True), \
                    patch.object(frappe.local, "form_dict", frappe._dict({"email": self.email}), create=True):
                accounts.browser_write({"email"})
                request.headers["Origin"] = "http://localhost:9999"
                with self.assertRaises(frappe.PermissionError):
                    accounts.browser_write({"email"})
                request.headers["Origin"] = "http://localhost:8088"
                request.headers.pop("X-RG-CSRF-Token")
                with self.assertRaises(frappe.PermissionError):
                    accounts.browser_write({"email"})
                request.headers["X-RG-CSRF-Token"] = nonce
                frappe.form_dict.roles = [SECRETARY]
                with self.assertRaises(frappe.PermissionError):
                    accounts.browser_write({"email"})
                frappe.form_dict.pop("roles")
                request.method = "GET"
                with self.assertRaises(frappe.PermissionError):
                    accounts.browser_write({"email"})
        finally:
            frappe.cache.delete_value(key)

    def _assert_native_link_dispatch_denied(self, user, key, login_token, actors=("Guest",)):
        from frappe.api.v2 import handle_rpc_call
        from frappe.auth import LoginManager
        from frappe.handler import execute_cmd
        from frappe.www import login as native_login

        before_mail, before_queue = frappe.db.count("RG Mail Event"), frappe.db.count("Email Queue")
        # The native module remains available for explicit console operations;
        # HTTP dispatch alone must select our blocker before native side effects.
        self.assertIsNot(frappe.get_attr("frappe.www.login.login_via_key"), accounts.block_native_link_login)
        self.assertEqual(native_login.login_via_key.__module__, "frappe.www.login")
        with patch.object(frappe, "sendmail") as sendmail, \
                patch.object(LoginManager, "login_as") as login_as, \
                patch.object(native_login, "LoginManager") as token_manager:
            for actor in actors:
                frappe.set_user(actor)
                for dispatcher in (execute_cmd, handle_rpc_call):
                    for method, verb, values in (
                        ("send_login_link", "POST", {"email": user}),
                        ("login_via_key", "GET", {"key": key}),
                        ("login_via_token", "GET", {"login_token": login_token}),
                    ):
                        with self.subTest(actor=actor, dispatcher=dispatcher.__name__, method=method):
                            request = SimpleNamespace(method=verb, path="/api/method/frappe.www.login." + method)
                            with patch.object(frappe.local, "request", request, create=True), \
                                    patch.object(frappe.local, "form_dict", frappe._dict(values), create=True):
                                with self.assertRaises(frappe.PermissionError):
                                    dispatcher("frappe.www.login." + method)
                            self.assertEqual(frappe.cache.get_value("one_time_login_key:" + key,
                                             use_local_cache=False), user)
                            self.assertEqual(frappe.cache.get_value("login_token:" + login_token,
                                             use_local_cache=False), "isolated-legacy-sid")
            sendmail.assert_not_called()
            login_as.assert_not_called()
            token_manager.assert_not_called()
        self.assertEqual(frappe.db.count("RG Mail Event"), before_mail)
        self.assertEqual(frappe.db.count("Email Queue"), before_queue)

    def test_native_email_link_generation_and_legacy_http_consumers_closed(self):
        self.assertEqual(int(frappe.get_system_settings("login_with_email_link") or 0), 0)
        user = self.users["one"]
        key, token = frappe.generate_hash(), frappe.generate_hash()
        cache_keys = ["one_time_login_key:" + key, "login_token:" + token]
        frappe.cache.set_value(cache_keys[0], user, expires_in_sec=300)
        frappe.cache.set_value(cache_keys[1], "isolated-legacy-sid", expires_in_sec=300)
        try:
            self._assert_native_link_dispatch_denied(user, key, token,
                actors=("Guest", user, self.users["secretary"], "Administrator"))
        finally:
            frappe.cache.delete_value(cache_keys)

    def test_legacy_link_http_stays_closed_after_contact_password_and_status_changes(self):
        _, _ = self.approved()
        hash_password(self.email, PASSWORD)
        key, token = frappe.generate_hash(), frappe.generate_hash()
        cache_keys = ["one_time_login_key:" + key, "login_token:" + token]
        frappe.cache.set_value(cache_keys[0], self.email, expires_in_sec=300)
        frappe.cache.set_value(cache_keys[1], "isolated-legacy-sid", expires_in_sec=300)
        try:
            self._assert_native_link_dispatch_denied(self.email, key, token)
            frappe.set_user(self.email)
            accounts.request_contact_change(self.student_id.lower() + "-legacy@fudan.edu.cn", PASSWORD)
            contact_token = secret_from_link(self.mail_calls[-1][1]["values"]["link"], "token")
            accounts.verify_contact_change(contact_token)
            self._assert_native_link_dispatch_denied(self.email, key, token)
            frappe.set_user(self.email)
            accounts.change_password(PASSWORD, NEXT_PASSWORD)
            self._assert_native_link_dispatch_denied(self.email, key, token)
            frappe.set_user(self.users["secretary"])
            accounts.set_student_status(self.email, 0)
            self._assert_native_link_dispatch_denied(self.email, key, token)
        finally:
            frappe.cache.delete_value(cache_keys)

    def test_redis_throttle_limits_identity_and_site_without_plain_keys(self):
        self.throttle_patch.stop()
        try:
            kind = self.prefix + "-rate"
            accounts._throttle(kind, self.email, identity_limit=1, ip_limit=20, site_limit=20)
            with self.assertRaises(frappe.RateLimitExceededError):
                accounts._throttle(kind, self.email, identity_limit=1, ip_limit=20, site_limit=20)
            accounts._throttle(kind + "-site", "first", identity_limit=20, ip_limit=20, site_limit=1)
            with self.assertRaises(frappe.RateLimitExceededError):
                accounts._throttle(kind + "-site", "second", identity_limit=20, ip_limit=20, site_limit=1)
        finally:
            self.throttle_patch.start()

    def test_verification_is_hashed_single_use_and_does_not_create_user(self):
        name, token = self.registration(verify=False)
        row = frappe.get_doc("RG Registration", name)
        self.assertEqual(row.token_hash, accounts._digest(token))
        self.assertNotIn(token, json.dumps(row.as_dict(), default=str))
        self.assertFalse(frappe.db.exists("User", self.email))
        # Loading the public GET context never reads or consumes the key.
        from research_group.www.rg_join import get_context
        get_context(frappe._dict())
        self.assertEqual(frappe.db.get_value("RG Registration", name, "status"), "pending_verification")
        accounts.verify_registration(name, token)
        self.assertEqual(frappe.db.get_value("RG Registration", name, "status"), "awaiting_approval")
        self.assertFalse(frappe.db.exists("User", self.email))
        with self.assertRaises(frappe.ValidationError):
            accounts.verify_registration(name, token)

    def test_expired_and_reissued_registration_token(self):
        name, old = self.registration(verify=False)
        frappe.db.set_value("RG Registration", name, "expires_at", now_datetime() - timedelta(seconds=1))
        with self.assertRaises(frappe.ValidationError):
            accounts.verify_registration(name, old)
        frappe.db.set_value("RG Registration", name, "requested_at", now_datetime() - timedelta(seconds=61))
        accounts.request_registration("虚构入组测试", self.student_id, self.email)
        new = secret_from_link(self.mail_calls[-1][1]["values"]["link"], "token")
        current = frappe.db.get_value("RG Registration", {"email": self.email, "status": "pending_verification"}, "name")
        self.assertNotEqual(name, current)
        self.assertEqual(frappe.db.get_value("RG Registration", name, "status"), "expired")
        self.assertNotEqual(old, new)
        with self.assertRaises(frappe.ValidationError):
            accounts.verify_registration(name, old)
        accounts.verify_registration(current, new)

    def test_expired_unverified_alias_releases_reservation_without_losing_history(self):
        name, token = self.registration(verify=False)
        original = frappe.get_doc("RG Registration", name)
        frappe.db.set_value("RG Registration", name, "expires_at", now_datetime() - timedelta(seconds=1))
        legitimate = self.student_id.lower() + "-real@fudan.edu.cn"
        accounts.request_registration("同学真实申请", self.student_id, legitimate)
        replacement = frappe.db.get_value("RG Registration", {"email": legitimate}, "name")
        self.assertTrue(replacement)
        old = frappe.get_doc("RG Registration", name)
        self.assertEqual((old.email, old.student_id, old.display_name),
                         (original.email, original.student_id, original.display_name))
        self.assertEqual(old.status, "expired")
        self.assertFalse(old.reserved_email or old.reserved_student_id or old.token_hash)
        self.assertTrue(frappe.db.exists("RG Account Audit", {"registration": name, "action": "registration_expired"}))
        with self.assertRaises(frappe.ValidationError):
            accounts.verify_registration(name, token)
        newtoken = secret_from_link(self.mail_calls[-1][1]["values"]["link"], "token")
        accounts.verify_registration(replacement, newtoken)
        # Verification claims the slot until a human decision. Expiry no
        # longer permits another claimant to displace that identity.
        frappe.db.set_value("RG Registration", replacement, "expires_at", now_datetime() - timedelta(days=1))
        accounts.request_registration("不应接管", self.student_id, self.email)
        self.assertEqual(frappe.db.get_value("RG Registration", replacement, "status"), "awaiting_approval")

    def test_expired_pending_contact_does_not_reserve_address_indefinitely(self):
        first = self.profile()
        candidate = self.student_id.lower() + "-expired@fudan.edu.cn"
        first.pending_email, first.pending_token_hash = candidate, accounts._digest("expired-old-key")
        first.pending_expires = now_datetime() - timedelta(seconds=1)
        accounts._save(first)
        second = self.users["two"]
        hash_password(second, PASSWORD)
        frappe.set_user(second)
        accounts.request_contact_change(candidate, PASSWORD)
        own = accounts._profile(second)
        self.assertEqual(own.pending_email, candidate)
        # The previous owner still has an expired verification key and cannot
        # consume it after the new reservation is established.
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.ValidationError):
            accounts.verify_contact_change("expired-old-key")

    def test_approval_builds_only_student_once_and_secretary_never_gets_key(self):
        name, key = self.approved()
        result = accounts.review_registration(name, "approve")
        self.assertEqual(result["user"], self.email)
        self.assertNotIn(key, json.dumps(result))
        self.assertEqual(frappe.db.count("User", {"name": self.email}), 1)
        self.assertEqual(frappe.db.count("RG Member", {"user": self.email}), 1)
        self.assertEqual(frappe.db.count("RG Account Profile", {"user": self.email}), 1)
        roles = {row.role for row in frappe.get_doc("User", self.email).roles}
        self.assertEqual(roles, {STUDENT})
        self.assertEqual(accounts.contact_address(self.email)["email"], self.email)
        output = accounts.bootstrap()
        serialized = json.dumps(output, default=str)
        self.assertNotIn(key, serialized)
        self.assertNotIn("reset_password_key", serialized)
        self.assertNotIn("token_hash", serialized)

    def test_existing_user_is_not_taken_over_even_if_created_after_verification(self):
        name, _ = self.registration()
        frappe.set_user("Administrator")
        existing = frappe.get_doc({"doctype": "User", "email": self.email, "first_name": "原身份",
                                   "enabled": 0, "send_welcome_email": 0, "roles": []}).insert(ignore_permissions=True)
        frappe.set_user(self.users["secretary"])
        with self.assertRaises(frappe.ValidationError):
            accounts.review_registration(name, "approve")
        self.assertEqual(frappe.db.get_value("User", existing.name, "enabled"), 0)
        self.assertFalse(frappe.db.exists("RG Member", self.email))
        self.assertEqual(frappe.db.get_value("RG Registration", name, "status"), "awaiting_approval")

    def test_registration_conflicts_uniform_and_rejection_not_approved(self):
        name, _ = self.registration()
        frappe.set_user("Guest")
        result = accounts.request_registration("另一姓名", self.student_id, "different@fudan.edu.cn")
        self.assertEqual(result["message"], accounts.GENERIC)
        self.assertEqual(frappe.db.count("RG Registration", {"student_id": self.student_id}), 1)
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.PermissionError):
            accounts.review_registration(name, "approve")
        frappe.set_user(self.users["secretary"])
        result = accounts.review_registration(name, "reject", "虚构身份未核实")
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(frappe.db.exists("User", self.email))

    def test_approval_failure_rolls_back_user_member_profile_and_mail(self):
        name, _ = self.registration()
        frappe.set_user(self.users["secretary"])
        before = frappe.db.count("RG Mail Event")
        with patch.object(accounts, "_issue_password", side_effect=RuntimeError("isolated mail failure")):
            with self.assertRaises(RuntimeError):
                accounts.review_registration(name, "approve")
        self.assertFalse(frappe.db.exists("User", self.email))
        self.assertFalse(frappe.db.exists("RG Member", self.email))
        self.assertFalse(frappe.db.exists("RG Account Profile", {"user": self.email}))
        self.assertEqual(frappe.db.count("RG Mail Event"), before)
        self.assertEqual(frappe.db.get_value("RG Registration", name, "status"), "awaiting_approval")

    def test_self_profile_uses_fixed_identity_and_no_native_password_bypass(self):
        user = self.users["one"]
        frappe.set_user(user)
        accounts.update_profile("本人新显示名", "+86 130-0000-0000", "虚构个人简介")
        self.assertEqual(frappe.db.get_value("User", user, "name"), user)
        self.assertEqual(frappe.db.get_value("User", user, "email"), user)
        self.assertEqual(frappe.db.get_value("RG Member", user, "display_name"), "本人新显示名")
        self.assertEqual(accounts.bootstrap()["profile"]["bio"], "虚构个人简介")
        doc = frappe.get_doc("User", user)
        doc.new_password = PASSWORD
        with self.assertRaises(frappe.PermissionError):
            doc.save(ignore_permissions=True)

    def test_password_strength_once_key_expiry_and_old_password(self):
        name, key = self.approved()
        frappe.set_user("Guest")
        for weak in ("short", "passwordpassword", "111111111111111111"):
            with self.subTest(weak=weak), self.assertRaises(frappe.ValidationError):
                accounts.update_password(weak, key=key)
        self.assertEqual(accounts.update_password(PASSWORD, key=key), "/login")
        self.assertEqual(check_password(self.email, PASSWORD), self.email)
        with self.assertRaises(frappe.ValidationError):
            accounts.update_password(NEXT_PASSWORD, key=key)
        frappe.set_user(self.email)
        with self.assertRaises(frappe.AuthenticationError):
            accounts.change_password("wrong-current-password", NEXT_PASSWORD)
        accounts.change_password(PASSWORD, NEXT_PASSWORD)
        self.assertEqual(check_password(self.email, NEXT_PASSWORD), self.email)
        accounts.request_password_reset(self.email)
        newkey = secret_from_link(self.mail_calls[-1][1]["values"]["link"], "key")
        frappe.db.set_value("User", self.email, "last_reset_password_key_generated_on", now_datetime() - timedelta(days=1))
        frappe.set_user("Guest")
        with self.assertRaises(frappe.ValidationError):
            accounts.update_password(PASSWORD, key=newkey)

    def test_contact_change_requires_password_validates_new_then_reset_uses_new(self):
        user = self.users["one"]
        profile = self.profile()
        hash_password(user, PASSWORD)
        frappe.set_user(user)
        new_email = self.student_id.lower() + "-new@fudan.edu.cn"
        with self.assertRaises(frappe.AuthenticationError):
            accounts.request_contact_change(new_email, "wrong-current-password")
        accounts.request_contact_change(new_email, PASSWORD)
        link = self.mail_calls[-1][1]["values"]["link"]
        token = secret_from_link(link, "token")
        self.assertEqual(accounts.contact_address(user)["email"], self.email)
        self.assertEqual(frappe.db.get_value("RG Account Profile", profile.name, "pending_token_hash"), accounts._digest(token))
        result = accounts.verify_contact_change(token)
        self.assertEqual(result["login_id"], user)
        self.assertEqual(accounts.contact_address(user)["email"], new_email)
        self.assertEqual(self.mail_calls[-1][0][1], "contact_changed")
        self.assertEqual(self.mail_calls[-1][1]["recipient_email"], self.email)
        with self.assertRaises(frappe.ValidationError):
            accounts.verify_contact_change(token)
        before = len(self.mail_calls)
        frappe.set_user("Guest")
        accounts.request_password_reset(new_email)
        self.assertEqual(len(self.mail_calls), before + 1)
        self.assertEqual(self.mail_calls[-1][1]["recipient_user"], user)
        self.assertEqual(frappe.db.get_value("User", user, "email"), user)

    def test_secretary_cannot_manage_protected_self_or_unverified_restore(self):
        frappe.set_user(self.users["secretary"])
        for key in ("teacher", "other_teacher", "secretary"):
            with self.subTest(key=key), self.assertRaises(frappe.PermissionError):
                accounts.set_student_status(self.users[key], 0)
        with self.assertRaises(frappe.ValidationError):
            accounts.set_student_status(self.users["one"], 1)
        self.profile()
        user = self.users["one"]
        accounts.set_student_status(user, 0)
        self.assertEqual(frappe.db.get_value("User", user, "enabled"), 0)
        self.assertEqual(frappe.db.get_value("RG Member", user, "active"), 0)
        frappe.set_user(user)
        with self.assertRaises(frappe.PermissionError):
            accounts.bootstrap()
        frappe.set_user(self.users["secretary"])
        accounts.set_student_status(user, 1)
        self.assertTrue(frappe.db.exists("User", user))
        self.assertEqual(frappe.db.get_value("RG Member", user, "active"), 1)

    def test_disable_failure_is_atomic_and_does_not_schedule_valid_session_purge(self):
        self.profile()
        user = self.users["one"]
        frappe.set_user(self.users["secretary"])
        with patch.object(accounts, "_audit", side_effect=RuntimeError("isolated audit failure")):
            with self.assertRaises(RuntimeError):
                accounts.set_student_status(user, 0)
        self.assertEqual(frappe.db.get_value("User", user, "enabled"), 1)
        self.assertEqual(frappe.db.get_value("RG Member", user, "active"), 1)

    def test_secretary_cannot_restore_external_or_changed_pause(self):
        profile = self.profile()
        user = self.users["one"]
        # A verified contact alone does not authorize undoing an admin lock.
        frappe.set_user("Administrator")
        doc = frappe.get_doc("User", user)
        doc.enabled = 0
        doc.save(ignore_permissions=True)
        frappe.set_user(self.users["secretary"])
        with self.assertRaises(frappe.ValidationError):
            accounts.set_student_status(user, 1)
        self.assertEqual(frappe.db.get_value("User", user, "enabled"), 0)
        # Restore the fixture as admin, then take a genuine service pause.
        frappe.set_user("Administrator")
        doc = frappe.get_doc("User", user)
        doc.enabled = 1
        doc.save(ignore_permissions=True)
        frappe.set_user(self.users["secretary"])
        accounts.set_student_status(user, 0)
        self.assertTrue(frappe.db.get_value("RG Account Profile", profile.name, "service_pause_audit"))
        frappe.set_user("Administrator")
        doc = frappe.get_doc("User", user)
        doc.append("roles", {"role": TEACHER})
        doc.save(ignore_permissions=True)
        doc.set("roles", [row for row in doc.roles if row.role != TEACHER])
        doc.save(ignore_permissions=True)
        self.assertFalse(frappe.db.get_value("RG Account Profile", profile.name, "service_pause_audit"))
        frappe.set_user(self.users["secretary"])
        with self.assertRaises(frappe.ValidationError):
            accounts.set_student_status(user, 1)
        self.assertEqual(frappe.db.get_value("User", user, "enabled"), 0)

    def test_late_redis_session_cannot_override_missing_committed_session(self):
        user = self.users["one"]
        sid = "rg-acct-session-" + frappe.generate_hash(length=32)
        self.session_ids.append(sid)
        frappe.db.sql("INSERT INTO `tabSessions` (sid,user,sessiondata,lastupdate,status) VALUES (%s,%s,%s,%s,%s)",
                      (sid, user, "{}", now_datetime(), "Active"))
        frappe.db.commit()
        self.committed_fixture = True
        frappe.set_user(user)
        frappe.session.sid = sid
        # Pin an ordinary-read snapshot before the revocation commits.
        self.assertTrue(frappe.db.sql("SELECT sid FROM `tabSessions` WHERE sid=%s", (sid,)))
        connection = frappe.db.get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM `tabSessions` WHERE sid=%s", (sid,))
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(frappe.db.sql("SELECT sid FROM `tabSessions` WHERE sid=%s", (sid,)))
        frappe.cache.hset("session", sid, {"user": user, "sid": sid, "data": {"user": user}})
        request = SimpleNamespace(method="GET")
        with patch.object(frappe.local, "request", request, create=True):
            with self.assertRaises(frappe.AuthenticationError):
                accounts.validate_group_session()
        self.assertFalse(frappe.cache.hget("session", sid))

    def _race(self, jobs):
        frappe.db.commit()
        self.committed_fixture = True
        parent = Path(os.environ.get("RG_ACCOUNT_TEST_WORKDIR") or frappe.get_site_path(
            "private", "tasks", "20261108-fdu-accounts-meals", "scratch", "account-races")).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=parent) as folder:
            root, processes = Path(folder), []
            try:
                for index, job in enumerate(jobs):
                    processes.append(subprocess.Popen([sys.executable, "-c",
                        "from research_group.tests.test_accounts import account_race_worker; account_race_worker()",
                        frappe.local.site, str(Path(frappe.local.sites_path).resolve()), str(root), str(index), json.dumps(job)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                deadline = time.monotonic() + 30
                while not all((root / f"{index}.ready").exists() for index in range(len(jobs))):
                    if time.monotonic() > deadline or any(proc.poll() is not None for proc in processes):
                        self.fail("account workers did not reach barrier")
                    time.sleep(.03)
                (root / "start").write_text("start", encoding="ascii")
                results = []
                for process in processes:
                    output, error = process.communicate(timeout=40)
                    self.assertEqual(process.returncode, 0, error[-3000:])
                    results.append(json.loads(output.strip().splitlines()[-1]))
                self.assertEqual(len({row["connection"] for row in results}), len(jobs))
                frappe.db.rollback()
                return results
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

    def test_independent_double_approval_creates_single_account(self):
        name, _ = self.registration()
        results = self._race([{"action": "approve", "application": name, "user": self.users["secretary"]} for _ in range(2)])
        self.assertTrue(all(row["ok"] for row in results), results)
        self.assertEqual(frappe.db.count("User", {"name": self.email}), 1)
        self.assertEqual(frappe.db.count("RG Account Profile", {"user": self.email}), 1)
        self.assertEqual(frappe.db.count("RG Account Audit", {"registration": name, "action": "registration_approve"}), 1)

    def test_independent_new_claims_after_rejection_reserve_only_one_live_application(self):
        name, _ = self.registration()
        frappe.set_user(self.users["secretary"])
        accounts.review_registration(name, "reject")
        jobs = [{"action": "register", "application": name, "user": "Guest", "student_id": self.student_id,
                 "email": prefix + self.student_id.lower() + "@fudan.edu.cn"} for prefix in ("first-", "second-")]
        results = self._race(jobs)
        self.assertTrue(all(row["ok"] for row in results), results)
        self.assertEqual(frappe.db.count("RG Registration", {
            "student_id": self.student_id, "status": "pending_verification"}), 1)
        self.assertEqual(frappe.db.count("RG Registration", {"reserved_student_id": self.student_id}), 1)
        self.assertEqual(frappe.db.get_value("RG Registration", name, "status"), "rejected")

    def test_independent_double_token_consume_and_waiting_secretary_revocation(self):
        name, token = self.registration(verify=False)
        results = self._race([{"action": "verify", "application": name, "token": token, "user": "Guest"} for _ in range(2)])
        self.assertEqual(sum(row["ok"] for row in results), 1, results)
        results = self._race([
            {"action": "revoke", "user": "Administrator", "target": self.users["secretary"], "hold": True},
            {"action": "approve", "application": name, "user": self.users["secretary"]},
        ])
        self.assertEqual([row["ok"] for row in results], [True, False], results)
        self.assertEqual(results[1]["error"], "PermissionError")
        self.assertFalse(frappe.db.exists("User", self.email))


def account_race_worker():
    site, sites_path, folder, worker_id, serialized = sys.argv[1:]
    root, job = Path(folder), json.loads(serialized)
    frappe.init(site, sites_path=sites_path)
    frappe.connect()
    frappe.in_test = True
    try:
        frappe.set_user(job["user"])
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        frappe.db.get_value("RG Registration", job.get("application"), "status")
        frappe.get_roles()
        if job.get("hold"):
            accounts.global_lock()
        connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
        (root / f"{worker_id}.ready").write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not (root / "start").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("account barrier")
            time.sleep(.03)
        if job.get("hold"):
            while not (root / "1.requested").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("waiting account request")
                time.sleep(.03)
        original_lock = accounts.global_lock

        def observed_lock():
            (root / f"{worker_id}.requested").write_text("requested", encoding="ascii")
            return original_lock()

        try:
            with patch.object(accounts, "global_lock", side_effect=observed_lock), \
                    patch.object(accounts, "_throttle"), patch.object(notifications, "_schedule"):
                if job["action"] == "approve":
                    accounts.review_registration(job["application"], "approve")
                elif job["action"] == "verify":
                    accounts.verify_registration(job["application"], job["token"])
                elif job["action"] == "register":
                    accounts.request_registration("独立连接虚构申请", job["student_id"], job["email"])
                else:
                    doc = frappe.get_doc("User", job["target"])
                    doc.set("roles", [row for row in doc.roles if row.role != SECRETARY])
                    doc.save(ignore_permissions=True)
            frappe.db.commit()
            result = {"ok": True, "connection": connection}
        except (frappe.ValidationError, frappe.PermissionError) as error:
            frappe.db.rollback()
            result = {"ok": False, "error": type(error).__name__, "connection": connection}
        print(json.dumps(result))
    finally:
        frappe.destroy()
