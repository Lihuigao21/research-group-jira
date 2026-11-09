"""Committed private mail events; every transport is an isolated local capture."""
import json
import os
import smtplib
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.client import get as native_get
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from research_group import accounts, notifications as mail
from research_group.ai.security import global_lock
from research_group.policy import SECRETARY, STUDENT
from research_group.tests import test_core as core


class CaptureSMTP:
    def __init__(self, error=None):
        self.messages = []
        self.error = error
        self.closed = 0

    def sendmail(self, sender, recipients, body):
        self.messages.append((sender, recipients, body))
        if self.error:
            raise self.error
        return {}

    def close(self):
        self.closed += 1


class TestNotifications(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.prefix = "rg-notify-" + frappe.generate_hash(length=10)
        # Keep real retry throttles isolated across repeated DB test runs.
        request_ip_patch = patch.object(frappe.local, "request_ip", self.prefix, create=True)
        request_ip_patch.start()
        self.addCleanup(request_ip_patch.stop)
        self.users = core.create_members(self.prefix)
        self.configuration = {"rg_mail_mode": "capture", "rg_mail_from": "notifications@example.invalid"}
        self.config_patch = patch.object(mail, "_configuration", side_effect=lambda: self.configuration)
        self.schedule_patch = patch.object(mail, "_schedule")
        self.config_patch.start()
        self.schedule_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.schedule_patch.stop)
        self.profiles = {}
        for index, (key, user) in enumerate(self.users.items()):
            self.profiles[key] = accounts._insert(
                "RG Account Profile", user=user, student_id="N" + frappe.generate_hash(length=16),
                contact_email=f"{self.prefix}-{index}@m.fudan.edu.cn", contact_verified=1, email_version=1,
            ).name
        frappe.db.commit()
        self.committed_fixture = True

    def tearDown(self):
        frappe.db.rollback()
        frappe.set_user("Administrator")
        global_lock()
        users = list(self.users.values())
        frappe.db.delete("RG Mail Event", {"actor": ["in", users]})
        frappe.db.delete("RG Mail Event", {"recipient_user": ["in", users]})
        frappe.db.delete("RG Message", {"actor": ["in", users]})
        frappe.db.delete("RG Account Audit", {"actor": ["in", users]})
        frappe.db.delete("RG Account Audit", {"target_user": ["in", users]})
        frappe.db.delete("RG Account Profile", {"user": ["in", users]})
        frappe.db.delete("RG Registration", {"display_name": self.prefix})
        core.TestResearchGroupCore._delete_fixture(self)
        frappe.db.commit()
        frappe.clear_cache()
        super().tearDown()

    def preview(self, recipients=None, **values):
        frappe.set_user(self.users["secretary"])
        return mail.preview_message(self.prefix, "虚构通知正文，不包含任何研究讨论材料。",
                                    recipients=recipients or [self.users["one"], self.users["two"]], **values)

    def confirmed(self, recipients=None):
        preview = self.preview(recipients)
        return mail.confirm_message(preview["name"], preview["confirmation_digest"])

    def event(self, name):
        return frappe.get_doc("RG Mail Event", name)

    def process(self, name):
        frappe.db.commit()
        mail.process_event(name)
        return self.event(name)

    def test_preview_requires_privileged_actor_and_exact_member_selection(self):
        preview = self.preview()
        self.assertEqual(preview["recipient_count"], 2)
        self.assertEqual(preview["body"], "虚构通知正文，不包含任何研究讨论材料。")
        self.assertEqual({row["user"] for row in preview["recipients"]},
                         {self.users["one"], self.users["two"]})
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.PermissionError):
            mail.preview_message("越权", "正文", recipients=[self.users["two"]])
        frappe.set_user(self.users["secretary"])
        for recipients in (["external@m.fudan.edu.cn"], [self.users["one"], self.users["one"]]):
            with self.assertRaises(frappe.ValidationError):
                mail.preview_message("无效", "正文", recipients=recipients)
        with self.assertRaises(frappe.ValidationError):
            mail.preview_message("伪造\n头部", "正文", recipients=[self.users["one"]])

    def bound_notice(self, suffix="automatic"):
        frappe.set_user(self.users["secretary"])
        return mail.queue_template(self.prefix + ":" + suffix, "account_status", recipient_user=self.users["one"],
            related_doctype="User", related_name=self.users["one"],
            values={"display_name": "虚构学生", "status": "已启用", "login_id": self.users["one"]})

    def test_automatic_known_failure_owner_and_secretary_recovery_preserves_safe_status_contract(self):
        notice = self.bound_notice()
        self.configuration["rg_mail_mode"] = "disabled"
        self.assertEqual(self.process(notice["name"]).status, "unconfigured")
        frappe.set_user(self.users["one"])
        original = mail.automatic_mail_status()
        extended = mail.automatic_mail_status(include_recovery=1)
        self.assertEqual(extended["events"], original["events"])
        self.assertEqual(extended["recovery"], {notice["name"]: "retry"})
        for key in ("two", "teacher"):
            frappe.set_user(self.users[key])
            with self.assertRaises(frappe.PermissionError):
                mail.retry_event(notice["name"])
        frappe.set_user(self.users["one"])
        result = mail.retry_event(notice["name"])
        self.assertEqual(result["status"], "queued")
        self.assertEqual(set(result), set(original["events"][0]))
        self.assertTrue(frappe.db.exists("RG Account Audit", {"target_user": self.users["one"],
            "action": "automatic_mail_retried", "actor": self.users["one"]}))
        self.configuration["rg_mail_mode"] = "capture"
        self.assertEqual(self.process(notice["name"]).status, "captured")
        mail._update(notice["name"], status="failed", attempts=1)
        frappe.set_user(self.users["secretary"])
        self.assertEqual(mail.retry_event(notice["name"])["status"], "queued")
        self.assertNotIn("body_ciphertext", json.dumps(mail.automatic_mail_status(self.users["one"], include_recovery=1)))

    def test_legacy_unbound_automatic_failure_remains_visible_but_cannot_gain_recovery_authorization(self):
        frappe.set_user(self.users["secretary"])
        notice = mail.queue_template(self.prefix + ":legacy-unbound", "account_status",
            recipient_user=self.users["one"], values={"display_name": "虚构学生", "status": "已启用",
                                                       "login_id": self.users["one"]})
        mail._update(notice["name"], status="failed", attempts=1)
        frappe.set_user(self.users["one"])
        status = mail.automatic_mail_status(include_recovery=1)
        self.assertEqual([row["name"] for row in status["events"]], [notice["name"]])
        self.assertEqual(status["recovery"], {})
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(notice["name"])
        self.assertEqual(self.event(notice["name"]).status, "failed")

    def test_legacy_unbound_deadline_reminder_is_cancelled_without_reconstructing_source(self):
        frappe.set_user(self.users["secretary"])
        notice = mail.queue_template(self.prefix + ":legacy-deadline", "deadline_reminder",
            recipient_user=self.users["one"], expires_at=now_datetime() + timedelta(hours=1),
            values={"title": "无法核实来源的旧提醒", "deadline": str(now_datetime() + timedelta(hours=1)),
                    "link": mail.get_url("/desk/rg-meals", allow_header_override=False)})
        with patch.object(mail, "_smtp_session") as transport:
            stored = self.process(notice["name"])
            transport.assert_not_called()
        self.assertEqual((stored.status, stored.error_class), ("cancelled", "source_unverified"))
        self.assertFalse(stored.related_doctype)
        self.assertFalse(stored.related_binding)

    def test_legacy_registration_binding_still_sends_current_key_but_not_changed_address(self):
        frappe.set_user(self.users["secretary"])
        registration = accounts._insert("RG Registration", display_name=self.prefix,
            student_id="L" + frappe.generate_hash(length=16), email=self.prefix + "-legacy@m.fudan.edu.cn",
            status="pending_verification", token_hash="a" * 64, token_version=1,
            expires_at=now_datetime() + timedelta(hours=1), requested_at=now_datetime())
        fields = frappe.db.sql("SELECT status,token_hash,token_version,expires_at FROM `tabRG Registration` "
            "WHERE name=%s", (registration.name,), as_dict=True)[0]
        legacy_binding = mail._digest(fields)
        values = {"display_name": self.prefix,
                  "link": mail.get_url("/rg_join?mode=verify#token=fictional", allow_header_override=False)}
        first = mail.queue_template(self.prefix + ":legacy-valid", "registration_verification",
            recipient_email=registration.email, related_doctype=registration.doctype,
            related_name=registration.name, values=values)
        self.assertEqual(self.event(first["name"]).related_binding, legacy_binding)
        self.assertEqual(self.process(first["name"]).status, "captured")
        second = mail.queue_template(self.prefix + ":legacy-address", "registration_verification",
            recipient_email=registration.email, related_doctype=registration.doctype,
            related_name=registration.name, values=values)
        frappe.db.set_value("RG Registration", registration.name, "email", self.prefix + "-changed@m.fudan.edu.cn")
        stored = self.process(second["name"])
        self.assertEqual((stored.status, stored.error_class), ("cancelled", "recipient_changed"))

    def test_automatic_recovery_rejects_unknown_submitted_expired_limit_and_changed_source_or_sender(self):
        notice = self.bound_notice()
        name = notice["name"]
        frappe.set_user(self.users["one"])
        for status in ("queued", "preparing", "sending", "unknown", "submitted", "captured", "cancelled"):
            mail._update(name, status=status, attempts=1)
            with self.assertRaises(frappe.ValidationError):
                mail.retry_event(name)
            self.assertEqual(mail.automatic_mail_status(include_recovery=1)["recovery"], {})
        mail._update(name, status="failed", attempts=3)
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(name)
        mail._update(name, status="failed", attempts=1, expires_at=now_datetime() - timedelta(seconds=1))
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(name)
        mail._update(name, expires_at=None)
        self.configuration["rg_mail_from"] = "different@example.invalid"
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(name)
        self.configuration["rg_mail_from"] = "notifications@example.invalid"
        frappe.set_user(self.users["secretary"])
        # account_status is allowed to inform a disabled member initially, but
        # a notice about an earlier enable state cannot be restored afterwards.
        frappe.db.set_value("User", self.users["one"], "enabled", 0)
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(name)
        self.assertEqual(mail.automatic_mail_status(self.users["one"], include_recovery=1)["recovery"], {})

    def test_changed_pending_contact_and_revoked_original_actor_cannot_be_recovered(self):
        frappe.set_user(self.users["one"])
        profile = frappe.get_doc("RG Account Profile", self.profiles["one"])
        profile.pending_email = self.prefix + "-pending@m.fudan.edu.cn"
        profile.pending_token_hash, profile.pending_expires = "e" * 64, now_datetime() + timedelta(hours=1)
        accounts._save(profile)
        notice = mail.queue_template(self.prefix + ":pending-recovery", "contact_verification",
            recipient_email=profile.pending_email, related_doctype=profile.doctype, related_name=profile.name,
            values={"display_name": "虚构学生", "link": mail.get_url("/rg_join?mode=contact#token=fictional",
                allow_header_override=False)})
        mail._update(notice["name"], status="failed", attempts=1)
        self.assertEqual(mail.automatic_mail_status(include_recovery=1)["recovery"], {notice["name"]: "retry"})
        profile.pending_token_hash = "f" * 64
        accounts._save(profile)
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(notice["name"])
        second = self.bound_notice("revoked-issuer")
        mail._update(second["name"], status="failed", attempts=1)
        frappe.set_user("Administrator")
        sender = frappe.get_doc("User", self.users["secretary"])
        sender.set("roles", [{"role": STUDENT}])
        sender.save(ignore_permissions=True)
        frappe.set_user(self.users["one"])
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(second["name"])
        self.assertEqual(mail.automatic_mail_status(include_recovery=1)["recovery"], {})

    def test_registration_recovery_is_secretary_only_reissues_key_and_preserves_old_audit(self):
        from research_group.tests.test_accounts import secret_from_link
        frappe.set_user(self.users["secretary"])
        with patch.object(accounts, "_mail", wraps=accounts._mail) as sent:
            accounts.request_registration(self.prefix, "R" + frappe.generate_hash(length=16),
                                          self.prefix + "-apply@m.fudan.edu.cn")
            old_token = secret_from_link(sent.call_args.kwargs["values"]["link"], "token")
        registration = frappe.get_doc("RG Registration", {"email": self.prefix + "-apply@m.fudan.edu.cn"})
        old = frappe.get_doc("RG Mail Event", {"related_doctype": "RG Registration", "related_name": registration.name})
        mail._update(old.name, status="failed", attempts=1)
        status = mail.registration_mail_status(registration.name, include_recovery=1)
        self.assertEqual(status["recovery"], {old.name: "reissue"})
        boot = accounts.bootstrap()
        displayed = next(row for row in boot["registrations"] if row.name == registration.name)
        self.assertEqual(displayed.mail_recovery, {old.name: "reissue"})
        for actor in ("Guest", self.users["one"], self.users["teacher"]):
            frappe.set_user(actor)
            with self.assertRaises(frappe.PermissionError):
                mail.registration_mail_status(registration.name, include_recovery=1)
            with self.assertRaises(frappe.PermissionError):
                mail.retry_event(old.name)
        frappe.set_user(self.users["secretary"])
        with patch.object(accounts, "_mail", wraps=accounts._mail) as sent:
            replacement = mail.retry_event(old.name)
            new_token = secret_from_link(sent.call_args.kwargs["values"]["link"], "token")
        self.assertNotEqual(replacement["name"], old.name)
        self.assertNotEqual(new_token, old_token)
        self.assertEqual((self.event(old.name).status, self.event(old.name).error_class), ("cancelled", "reissued"))
        self.assertTrue(frappe.db.exists("RG Account Audit", {"registration": registration.name,
            "action": "automatic_mail_reissued"}))
        with self.assertRaises(frappe.ValidationError):
            accounts.verify_registration(registration.name, old_token)
        self.assertTrue(accounts.verify_registration(registration.name, new_token)["ok"])
        self.assertNotIn(new_token, json.dumps(replacement))
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(old.name)

    def test_password_recovery_issues_new_current_link_and_rejects_expired_source(self):
        frappe.set_user(self.users["secretary"])
        notice = accounts._issue_password(self.users["one"])
        original_key = accounts._user_row(self.users["one"]).reset_password_key
        mail._update(notice["name"], status="unconfigured")
        frappe.set_user(self.users["one"])
        result = mail.retry_event(notice["name"])
        self.assertNotEqual(result["name"], notice["name"])
        self.assertNotEqual(accounts._user_row(self.users["one"]).reset_password_key, original_key)
        self.assertEqual(self.event(notice["name"]).status, "cancelled")
        mail._update(result["name"], status="failed", attempts=1)
        frappe.db.set_value("User", self.users["one"], "last_reset_password_key_generated_on",
                            now_datetime() - timedelta(days=1))
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(result["name"])
        self.assertEqual(mail.automatic_mail_status(include_recovery=1)["recovery"], {})

    def test_committed_backlog_continues_in_bounded_deduplicated_batch(self):
        notices = [self.bound_notice("batch-" + str(index)) for index in range(25)]
        created = now_datetime() - timedelta(days=36500)
        for notice in notices:
            frappe.db.set_value("RG Mail Event", notice["name"], "creation", created)
        frappe.db.commit()
        self.schedule_patch.stop()
        try:
            with patch("frappe.enqueue") as enqueue:
                mail.drain_events()
        finally:
            self.schedule_patch.start()
        rows = [self.event(notice["name"]) for notice in notices]
        self.assertEqual(sum(row.status == "captured" for row in rows), 20)
        pending = sorted(row.name for row in rows if row.status == "queued")
        self.assertEqual(len(pending), 5)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.kwargs["timeout"], 180)
        self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
        self.assertEqual(enqueue.call_args.kwargs["job_id"], "rg-mail:" + frappe.local.site + ":" + pending[0])

    def test_deadline_change_and_close_cancel_old_reminders_before_transport(self):
        from research_group import meals
        frappe.set_user(self.users["secretary"])
        draft = meals.create_round(self.prefix + "截止提醒", deadline=now_datetime() + timedelta(hours=1))
        round_ = meals.publish_round(draft["name"], draft["revision"])
        try:
            mail.send_deadline_reminders()
            old = frappe.get_doc("RG Mail Event", {"related_doctype": "RG Meal Round",
                "related_name": round_["name"], "template": "deadline_reminder", "recipient_user": self.users["one"]})
            changed = meals.set_deadline(round_["name"], now_datetime() + timedelta(hours=2), round_["revision"])
            self.assertEqual(self.process(old.name).error_class, "source_changed")
            frappe.set_user(self.users["secretary"])
            mail.send_deadline_reminders()
            fresh = frappe.db.get_value("RG Mail Event", {"related_doctype": "RG Meal Round",
                "related_name": round_["name"], "template": "deadline_reminder", "recipient_user": self.users["one"],
                "status": "queued"}, "name")
            self.assertTrue(fresh)
            meals.set_round_status(round_["name"], True, changed["revision"])
            with patch.object(mail, "_smtp_session") as transport:
                self.assertEqual(self.process(fresh).error_class, "source_changed")
                transport.assert_not_called()
            self.assertEqual((self.event(old.name).status, self.event(fresh).status), ("cancelled", "cancelled"))
        finally:
            frappe.db.delete("RG Meal Item", {"parent": round_["name"]})
            frappe.db.delete("RG Meal Round", {"name": round_["name"]})

    def test_queued_manual_notice_cannot_reach_former_group_member(self):
        confirmed = self.confirmed([self.users["one"]])
        frappe.set_user("Administrator")
        recipient = frappe.get_doc("User", self.users["one"])
        recipient.set("roles", [])
        recipient.save(ignore_permissions=True)
        with patch.object(mail, "_smtp_session") as transport:
            self.assertEqual(self.process(confirmed["events"][0]["name"]).status, "cancelled")
            transport.assert_not_called()

    def test_plain_text_preview_confirmation_and_wire_preserve_special_characters(self):
        # Existing/imported member names are plain text too. Build this fixture
        # directly so this test covers notification storage, not another form's
        # input normalization or legacy member-controller HTML behavior.
        names = {self.users["one"]: "虚构 <literal> & 姓名一",
                 self.users["two"]: "虚构 <img src=x onerror=alert(1)> & 姓名二"}
        global_lock()
        for user, display_name in names.items():
            frappe.db.set_value("RG Member", user, "display_name", display_name)
        frappe.set_user(self.users["secretary"])
        subject = self.prefix + " <literal> & 完整主题"
        body = "第一行 <literal> & 完整正文\n第二行 <img src=x onerror=alert(1)> & 纯文本\n最后一行"
        recipients = sorted(names)
        preview = mail.preview_message(subject, body, recipients=recipients)
        self.assertEqual(preview["subject"], subject)
        self.assertEqual(preview["body"], body)
        self.assertEqual({row["user"]: row["display_name"] for row in preview["recipients"]}, names)
        stored = frappe.get_doc("RG Message", preview["name"])
        self.assertEqual(stored.subject.encode("utf-8"), subject.encode("utf-8"))
        self.assertEqual(stored.body.encode("utf-8"), body.encode("utf-8"))
        selected = json.loads(stored.recipients_json)
        self.assertEqual({row["user"]: row["display_name"] for row in selected}, names)
        self.assertEqual(stored.selector_json, mail._json({"recipients": recipients,
                         "recipient_role": "", "all_members": False}))
        self.assertEqual(stored.confirmation_digest, mail._digest({
            "name": stored.name, "actor": self.users["secretary"], "subject": subject,
            "body": body, "kind": "important", "recipients": selected,
            "selector": json.loads(stored.selector_json), "authorization": stored.authorization_version,
            "expires": stored.expires_at, "from": stored.from_address,
        }))
        confirmed = mail.confirm_message(stored.name, preview["confirmation_digest"])
        replay = mail.confirm_message(stored.name, preview["confirmation_digest"])
        self.assertEqual(len(confirmed["events"]), 2)
        self.assertEqual([row["name"] for row in confirmed["events"]],
                         [row["name"] for row in replay["events"]])
        reread = mail.get_message(stored.name)
        self.assertEqual((reread["subject"], reread["body"]), (subject, body))
        self.assertEqual(reread["recipients"], preview["recipients"])
        self.configuration["rg_mail_mode"] = "smtp"
        transport = CaptureSMTP()
        with patch.object(mail, "_smtp_session", return_value=transport):
            for row in confirmed["events"]:
                event = self.event(row["name"])
                self.assertEqual(event.subject, subject)
                self.assertEqual(mail.decrypt(event.body_ciphertext), body)
                self.assertEqual(self.process(event.name).status, "submitted")
        self.assertEqual(len(transport.messages), 2)
        for _, recipients, raw in transport.messages:
            message = BytesParser(policy=policy.default).parsebytes(raw)
            self.assertEqual(message.get_content_type(), "text/plain")
            self.assertEqual(message.get_content_charset(), "utf-8")
            self.assertFalse(message.is_multipart())
            self.assertEqual(str(message["Subject"]), subject)
            self.assertEqual(message.get_content().replace("\r\n", "\n"), body + "\n")
            self.assertEqual(len(recipients), 1)

    def test_plain_text_fields_keep_token_guard_and_exact_event_key(self):
        preview = self.preview([self.users["one"]])
        untrusted = frappe.get_doc("RG Message", preview["name"])
        untrusted.body = "<script>this remains untrusted text</script> &"
        untrusted.flags.rg_notification = "TOKEN"
        with self.assertRaises(frappe.PermissionError):
            untrusted.save(ignore_permissions=True)
        self.assertEqual(frappe.db.get_value("RG Message", preview["name"], "body"), preview["body"])
        with self.assertRaises(frappe.PermissionError):
            native_get("RG Message", preview["name"])
        event_key = self.prefix + ":<literal> & exact event key"
        arguments = dict(event_key=event_key, template="account_status", recipient_user=self.users["one"],
            values={"display_name": "虚构 <literal> & 姓名", "status": "已启用", "login_id": self.users["one"]})
        first = mail.queue_template(**arguments)
        second = mail.queue_template(**arguments)
        self.assertEqual(first["name"], second["name"])
        self.assertEqual(self.event(first["name"]).event_key, event_key)
        self.assertIn("虚构 <literal> & 姓名", mail.decrypt(self.event(first["name"]).body_ciphertext))

    def test_confirm_is_owner_scoped_idempotent_and_private(self):
        preview = self.preview()
        frappe.set_user(self.users["teacher"])
        with self.assertRaises(frappe.PermissionError):
            mail.confirm_message(preview["name"], preview["confirmation_digest"])
        frappe.set_user(self.users["secretary"])
        with self.assertRaises(frappe.PermissionError):
            mail.confirm_message(preview["name"], "forged")
        first = mail.confirm_message(preview["name"], preview["confirmation_digest"])
        second = mail.confirm_message(preview["name"], preview["confirmation_digest"])
        self.assertEqual({row["name"] for row in first["events"]}, {row["name"] for row in second["events"]})
        self.assertEqual(frappe.db.count("RG Mail Event", {"message": preview["name"]}), 2)
        self.assertTrue(all("body_ciphertext" not in row for row in first["events"]))
        for name in (preview["name"], first["events"][0]["name"]):
            doctype = "RG Message" if name == preview["name"] else "RG Mail Event"
            with self.assertRaises(frappe.PermissionError):
                native_get(doctype, name)
        self.assertFalse(frappe.get_all("Version", filters={"ref_doctype": "RG Message", "docname": preview["name"]}))

    def test_contact_change_and_sender_revocation_invalidate_preview(self):
        preview = self.preview()
        profile = frappe.get_doc("RG Account Profile", self.profiles["one"])
        profile.contact_email = self.prefix + "-changed@fudan.edu.cn"
        profile.email_version += 1
        accounts._save(profile)
        with self.assertRaises(frappe.ValidationError):
            mail.confirm_message(preview["name"], preview["confirmation_digest"])
        self.assertEqual(frappe.db.count("RG Mail Event", {"message": preview["name"]}), 0)
        preview = self.preview()
        frappe.get_roles(self.users["secretary"])
        frappe.set_user("Administrator")
        native = frappe.get_doc("User", self.users["secretary"])
        native.set("roles", [{"role": STUDENT}])
        native.save(ignore_permissions=True)
        frappe.set_user(self.users["secretary"])
        with self.assertRaises(frappe.PermissionError):
            mail.confirm_message(preview["name"], preview["confirmation_digest"])

    def test_batch_failure_rolls_back_partial_events_and_publication(self):
        preview = self.preview()
        original, calls = mail._new_event, []

        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("injected second event failure")
            return original(*args, **kwargs)

        with patch.object(mail, "_new_event", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                mail.confirm_message(preview["name"], preview["confirmation_digest"])
        self.assertEqual(frappe.db.count("RG Mail Event", {"message": preview["name"]}), 0)
        self.assertEqual(frappe.db.get_value("RG Message", preview["name"], "status"), "preview")
        self.assertEqual(len(mail.confirm_message(preview["name"], preview["confirmation_digest"])["events"]), 2)

    def test_capture_and_disabled_states_do_not_claim_external_delivery(self):
        confirmed = self.confirmed([self.users["one"]])
        with patch.object(mail, "_smtp_session") as transport:
            event = self.process(confirmed["events"][0]["name"])
            transport.assert_not_called()
        self.assertEqual(event.status, "captured")
        self.assertFalse(event.submitted_at)
        confirmed = self.confirmed([self.users["two"]])
        self.configuration["rg_mail_mode"] = "disabled"
        event = self.process(confirmed["events"][0]["name"])
        self.assertEqual((event.status, event.error_class, event.attempts), ("unconfigured", "mail_disabled", 0))
        self.configuration["rg_mail_mode"] = "capture"
        frappe.set_user(self.users["secretary"])
        mail.retry_event(event.name)
        self.assertEqual(self.process(event.name).status, "captured")

    def test_smtp_uses_fixed_from_one_to_no_cc_and_truthful_acceptance(self):
        confirmed = self.confirmed()
        self.configuration["rg_mail_mode"] = "smtp"
        transport = CaptureSMTP()
        with patch.object(mail, "_smtp_session", return_value=transport):
            for row in confirmed["events"]:
                self.assertEqual(self.process(row["name"]).status, "submitted")
        self.assertEqual(len(transport.messages), 2)
        self.assertEqual(transport.closed, 2)
        for sender, recipients, raw in transport.messages:
            message = BytesParser(policy=policy.default).parsebytes(raw)
            self.assertEqual(sender, self.configuration["rg_mail_from"])
            self.assertEqual(message["From"], sender)
            self.assertIsNotNone(parsedate_to_datetime(message["Date"]))
            self.assertEqual(len(recipients), 1)
            self.assertEqual(message["To"], recipients[0])
            self.assertIsNone(message["Cc"])
            self.assertIsNone(message["Bcc"])
            self.assertNotIn("已送达", mail.STATUS_LABELS["submitted"])

    def test_unknown_is_never_automatically_or_manually_retried(self):
        confirmed = self.confirmed([self.users["one"]])
        self.configuration["rg_mail_mode"] = "smtp"
        transport = CaptureSMTP(TimeoutError("isolated lost final acknowledgement"))
        name = confirmed["events"][0]["name"]
        with patch.object(mail, "_smtp_session", return_value=transport):
            self.assertEqual(self.process(name).status, "unknown")
            mail.process_event(name)
            mail.drain_events()
        self.assertEqual(len(transport.messages), 1)
        frappe.set_user(self.users["secretary"])
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(name)

    def test_known_smtp_rejection_can_be_retried_with_bound(self):
        confirmed = self.confirmed([self.users["one"]])
        self.configuration["rg_mail_mode"] = "smtp"
        transport = CaptureSMTP(smtplib.SMTPDataError(550, b"isolated refusal"))
        name = confirmed["events"][0]["name"]
        with patch.object(mail, "_smtp_session", return_value=transport):
            event = self.process(name)
        self.assertEqual((event.status, event.error_class), ("failed", "SMTPDataError"))
        frappe.set_user(self.users["secretary"])
        mail.retry_event(name)
        self.configuration["rg_mail_mode"] = "capture"
        self.assertEqual(self.process(name).status, "captured")
        mail._update(name, status="failed", attempts=3)
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(name)

    def test_changed_recipient_and_changed_fixed_from_cancel_queued_mail(self):
        confirmed = self.confirmed([self.users["one"]])
        profile = frappe.get_doc("RG Account Profile", self.profiles["one"])
        profile.email_version += 1
        accounts._save(profile)
        event = self.process(confirmed["events"][0]["name"])
        self.assertEqual((event.status, event.error_class), ("cancelled", "recipient_changed"))
        confirmed = self.confirmed([self.users["two"]])
        self.configuration["rg_mail_from"] = "changed@example.invalid"
        event = self.process(confirmed["events"][0]["name"])
        self.assertEqual((event.status, event.error_class), ("cancelled", "sender_configuration_changed"))

    def test_source_key_rotation_cancels_old_verification_event(self):
        frappe.set_user(self.users["secretary"])
        registration = accounts._insert("RG Registration", display_name=self.prefix,
            student_id="V" + frappe.generate_hash(length=16), email=self.prefix + "@m.fudan.edu.cn",
            status="pending_verification", token_hash="a" * 64, token_version=1,
            expires_at=now_datetime() + timedelta(hours=1), requested_at=now_datetime())
        values = {"display_name": self.prefix,
                  "link": mail.get_url("/rg_join?mode=verify&token=fictional", allow_header_override=False)}
        first = mail.queue_template(self.prefix + ":verify", "registration_verification",
            recipient_email=registration.email, values=values,
            related_doctype=registration.doctype, related_name=registration.name)
        again = mail.queue_template(self.prefix + ":verify", "registration_verification",
            recipient_email=registration.email, values=values,
            related_doctype=registration.doctype, related_name=registration.name)
        self.assertEqual(first["name"], again["name"])
        self.assertNotIn("fictional", self.event(first["name"]).body_ciphertext)
        registration.token_hash, registration.token_version = "b" * 64, 2
        accounts._save(registration)
        event = self.process(first["name"])
        self.assertEqual((event.status, event.error_class), ("cancelled", "authentication_key_changed"))

    def test_status_notice_uses_verified_old_contact_after_disable(self):
        frappe.set_user(self.users["secretary"])
        frappe.db.set_value("RG Member", self.users["one"], "active", 0)
        frappe.db.set_value("User", self.users["one"], "enabled", 0)
        notice = mail.queue_template(self.prefix + ":disabled", "account_status", recipient_user=self.users["one"],
            values={"display_name": "虚构学生", "status": "已停用", "login_id": self.users["one"]})
        self.assertEqual(self.process(notice["name"]).status, "captured")

    def test_automatic_status_is_safe_owner_or_manageable_student_only(self):
        frappe.set_user(self.users["secretary"])
        notice = mail.queue_template(self.prefix + ":summary", "account_status", recipient_user=self.users["one"],
            values={"display_name": "虚构学生", "status": "已启用", "login_id": self.users["one"]})
        mail._update(notice["name"], status="unknown", attempts=1, last_attempt_at=now_datetime())
        # An unrelated manual announcement is not part of the automatic audit.
        self.confirmed([self.users["one"]])
        frappe.set_user(self.users["one"])
        result = mail.automatic_mail_status()
        self.assertEqual([row["name"] for row in result["events"]], [notice["name"]])
        expected = {"name", "template", "status", "status_label", "attempts", "last_attempt_at",
                    "submitted_at", "created_at"}
        self.assertEqual(set(result["events"][0]), expected)
        self.assertEqual(result["events"][0]["status"], "unknown")
        self.assertIsInstance(result["events"][0]["last_attempt_at"], str)
        self.assertIsInstance(result["events"][0]["created_at"], str)
        self.assertIsNone(result["events"][0]["submitted_at"])
        self.assertNotIn("body", json.dumps(result))
        with self.assertRaises(frappe.PermissionError):
            mail.automatic_mail_status(self.users["two"])
        frappe.set_user(self.users["secretary"])
        self.assertEqual(mail.automatic_mail_status(self.users["one"]), result)
        for target in (self.users["teacher"], self.users["other_teacher"], "Administrator"):
            with self.assertRaises(frappe.PermissionError):
                mail.automatic_mail_status(target)
        # A teacher can still inspect the status of their own automatic mail.
        frappe.set_user(self.users["teacher"])
        self.assertEqual(mail.automatic_mail_status()["events"], [])

    def test_automatic_summary_follows_profile_and_registration_without_exposing_links(self):
        frappe.set_user(self.users["secretary"])
        profile = frappe.get_doc("RG Account Profile", self.profiles["one"])
        profile.pending_email = self.prefix + "-pending@m.fudan.edu.cn"
        profile.pending_token_hash, profile.pending_expires = "d" * 64, now_datetime() + timedelta(hours=1)
        accounts._save(profile)
        contact = mail.queue_template(self.prefix + ":contact-summary", "contact_verification",
            recipient_email=profile.pending_email, related_doctype=profile.doctype, related_name=profile.name,
            values={"display_name": "虚构学生",
                    "link": mail.get_url("/rg_join?mode=contact#token=fictional", allow_header_override=False)})
        registration = accounts._insert("RG Registration", display_name=self.prefix,
            student_id="R" + frappe.generate_hash(length=16), email=self.prefix + "-registration@m.fudan.edu.cn",
            status="awaiting_approval", token_version=1, verified_at=now_datetime(), requested_at=now_datetime())
        rejection = mail.queue_template(self.prefix + ":registration-summary", "registration_rejected",
            recipient_email=registration.email, related_doctype=registration.doctype, related_name=registration.name,
            values={"display_name": self.prefix})
        result = mail.registration_mail_status(registration.name)
        self.assertEqual([row["name"] for row in result["events"]], [rejection["name"]])
        self.assertNotIn("fictional", json.dumps(result))
        frappe.set_user(self.users["one"])
        self.assertEqual([row["name"] for row in mail.automatic_mail_status()["events"]], [contact["name"]])
        with self.assertRaises(frappe.PermissionError):
            mail.registration_mail_status(registration.name)
        frappe.set_user(self.users["teacher"])
        with self.assertRaises(frappe.PermissionError):
            mail.registration_mail_status(registration.name)

    def test_automatic_summary_limits_twenty_per_authorized_target_and_bounded_batch(self):
        frappe.set_user(self.users["secretary"])
        for index in range(22):
            mail.queue_template(self.prefix + f":summary:{index}", "account_status",
                recipient_user=self.users["one"],
                values={"display_name": "虚构学生", "status": "虚构状态", "login_id": self.users["one"]})
        result = mail.automatic_status_rows([self.users["one"], self.users["two"]])
        self.assertEqual(len(result["users"][self.users["one"]]), 20)
        self.assertEqual(result["users"][self.users["two"]], [])
        with self.assertRaises(frappe.PermissionError):
            mail.automatic_status_rows([self.users["one"]] * 201)

    def test_after_commit_scheduling_coalesces_and_rollback_does_not_send(self):
        self.schedule_patch.stop()
        try:
            with patch("frappe.enqueue") as enqueue:
                confirmed = self.confirmed([self.users["one"]])
                mail._schedule()
                self.assertEqual(enqueue.call_count, 0)
                frappe.db.commit()
                self.assertEqual(enqueue.call_count, 1)
                self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
                preview = self.preview([self.users["two"]])
                rolled_back = mail.confirm_message(preview["name"], preview["confirmation_digest"])
                names = [row["name"] for row in rolled_back["events"]]
                frappe.db.rollback()
                self.assertEqual(enqueue.call_count, 1)
                with patch.object(mail, "_smtp_session") as transport:
                    for name in names:
                        mail.process_event(name)
                    transport.assert_not_called()
                self.assertEqual(self.event(confirmed["events"][0]["name"]).status, "queued")
        finally:
            self.schedule_patch.start()

    def test_lost_worker_becomes_unknown_without_resending(self):
        confirmed = self.confirmed([self.users["one"]])
        name = confirmed["events"][0]["name"]
        mail._update(name, status="sending", attempts=1, last_attempt_at=now_datetime() - timedelta(minutes=6))
        frappe.db.commit()
        with patch.object(mail, "_smtp_session") as transport:
            mail.drain_events()
            transport.assert_not_called()
        event = self.event(name)
        self.assertEqual((event.status, event.error_class), ("unknown", "worker_lost"))

    def test_lost_preparation_recovers_without_late_worker_sending_old_attempt(self):
        notice = self.bound_notice("lost-preparation")
        name = notice["name"]
        self.configuration["rg_mail_mode"] = "smtp"
        late, recovered = CaptureSMTP(), CaptureSMTP()
        process = mail.process_event

        def resume_after_recovery(settings):
            self.assertEqual((self.event(name).status, self.event(name).attempts), ("preparing", 1))
            mail._update(name, last_attempt_at=now_datetime() - timedelta(minutes=6))
            frappe.db.commit()
            with patch.object(mail, "process_event"):
                mail.drain_events()
            self.assertEqual((self.event(name).status, self.event(name).error_class),
                             ("failed", "preparation_lost"))
            frappe.set_user(self.users["one"])
            self.assertEqual(mail.retry_event(name)["status"], "queued")
            frappe.db.commit()
            with patch.object(mail, "_smtp_session", return_value=recovered):
                process(name)
            self.assertEqual((self.event(name).status, self.event(name).attempts), ("submitted", 2))
            return late

        with patch.object(mail, "_smtp_session", side_effect=resume_after_recovery):
            self.process(name)
        self.assertEqual(late.messages, [])
        self.assertEqual(late.closed, 1)
        self.assertEqual(len(recovered.messages), 1)
        self.assertEqual((self.event(name).status, self.event(name).attempts), ("submitted", 2))

    def test_published_meeting_metadata_replacements_bind_each_revision_and_deduplicate(self):
        from research_group.services import assign_reporter, publish_meeting
        frappe.set_user(self.users["teacher"])
        meeting = frappe.get_doc({"doctype": "RG Group Meeting", "title": self.prefix + "更新会议",
            "start_at": now_datetime() + timedelta(hours=4), "end_at": now_datetime() + timedelta(hours=6),
            "deadline": now_datetime() + timedelta(hours=1), "location": "虚构会议室"}).insert()
        assign_reporter(meeting.name, self.users["one"])
        publish_meeting(meeting.name)
        frappe.db.commit()

        def rows():
            return frappe.get_all("RG Mail Event", filters={"template": "meeting_published",
                "related_name": meeting.name}, fields=["name", "recipient_user", "related_binding", "status"])

        first = rows()
        recipients = {row.recipient_user for row in first}
        self.assertTrue(set(self.users.values()).issubset(recipients))
        count = len(first)
        previous = {row.name for row in first}
        for field, value in (("title", self.prefix + "会议更名"),
                             ("start_at", now_datetime() + timedelta(hours=5)),
                             ("deadline", now_datetime() + timedelta(hours=2))):
            frappe.set_user(self.users["teacher"])
            meeting.reload()
            meeting.set(field, value)
            meeting.save()
            frappe.db.commit()
            current = rows()
            fresh = [row for row in current if row.name not in previous]
            self.assertEqual(len(fresh), count)
            self.assertEqual({row.recipient_user for row in fresh}, recipients)
            binding = mail._source_binding("meeting_published", meeting.doctype, meeting.name)
            self.assertEqual({row.related_binding for row in fresh}, {binding})
            self.assertTrue({row.status for row in fresh}.issubset({"queued", "unconfigured"}))
            self.assertTrue(all(row.status == "queued" for row in fresh
                                if row.recipient_user in self.users.values()))
            before_save = {row.name for row in current}
            meeting.save()
            frappe.db.commit()
            self.assertEqual({row.name for row in rows()}, before_save)
            previous = before_save
        first_one = next(row.name for row in first if row.recipient_user == self.users["one"])
        self.assertEqual((self.process(first_one).status, self.event(first_one).error_class),
                         ("cancelled", "source_changed"))
        latest = next(row.name for row in fresh if row.recipient_user == self.users["one"])
        self.assertEqual(self.process(latest).status, "captured")

    def test_queued_deadline_reminders_recheck_actual_completed_work_before_transport(self):
        from research_group import meals, surveys
        from research_group.services import assign_reporter, publish_meeting, submit_material
        from research_group.tests.test_meals import rice
        frappe.set_user(self.users["secretary"])
        survey = frappe.get_doc({"doctype": "RG Survey", "title": self.prefix + "提醒问卷",
            "deadline": now_datetime() + timedelta(hours=1),
            "questions": [{"title": "虚构答复", "question_type": "简答", "required": 1}]}).insert()
        surveys.publish_survey(survey.name)
        survey.reload()
        draft = meals.create_round(self.prefix + "提醒工作餐", deadline=now_datetime() + timedelta(hours=1))
        round_ = meals.publish_round(draft["name"], draft["revision"])
        meeting = frappe.get_doc({"doctype": "RG Group Meeting", "title": self.prefix + "提醒报告",
            "start_at": now_datetime() + timedelta(hours=2), "end_at": now_datetime() + timedelta(hours=3),
            "deadline": now_datetime() + timedelta(hours=1), "location": "虚构会议室"}).insert()
        report = assign_reporter(meeting.name, self.users["one"])
        publish_meeting(meeting.name)
        frappe.db.commit()
        try:
            frappe.set_user(self.users["two"])
            voided = meals.submit_order(round_["name"], rice(), request_id=frappe.generate_hash(length=16))
            frappe.set_user(self.users["secretary"])
            meals.void_order(voided["name"], "虚构作废订单", voided["revision"])
            mail.send_deadline_reminders()
            frappe.db.commit()
            queued = []
            for doctype, name in ((survey.doctype, survey.name), ("RG Meal Round", round_["name"]),
                                   (meeting.doctype, meeting.name)):
                event = frappe.get_doc("RG Mail Event", {"template": "deadline_reminder",
                    "related_doctype": doctype, "related_name": name, "recipient_user": self.users["one"]})
                self.assertEqual(event.status, "queued")
                queued.append(event.name)
            frappe.set_user(self.users["one"])
            surveys.submit_response(survey.name, {survey.questions[0].name: "已真实提交"})
            meals.submit_order(round_["name"], rice(), request_id=frappe.generate_hash(length=16))
            submit_material(report["name"], "已真实提交报告", "reminder.pdf", core.pdf_bytes(self.prefix))
            frappe.db.commit()
            self.configuration["rg_mail_mode"] = "smtp"
            with patch.object(mail, "_smtp_session") as transport:
                for name in queued:
                    event = self.process(name)
                    self.assertEqual((event.status, event.error_class), ("cancelled", "work_completed"))
                transport.assert_not_called()
            # A voided order is not completed work and must neither suppress
            # creation nor cancel the actual queued reminder before capture.
            self.configuration["rg_mail_mode"] = "capture"
            void_reminder = frappe.get_doc("RG Mail Event", {"template": "deadline_reminder",
                "related_doctype": "RG Meal Round", "related_name": round_["name"],
                "recipient_user": self.users["two"]})
            self.assertEqual(self.process(void_reminder.name).status, "captured")
        finally:
            frappe.set_user("Administrator")
            responses = frappe.get_all("RG Survey Response", filters={"survey": survey.name}, pluck="name")
            if responses:
                frappe.db.delete("RG Survey Response", {"name": ["in", responses]})
            frappe.db.delete("RG Survey Question", {"parent": survey.name})
            frappe.db.delete("RG Survey", {"name": survey.name})
            orders = frappe.get_all("RG Meal Order", filters={"round": round_["name"]}, pluck="name")
            if orders:
                frappe.db.delete("RG Meal Order Item", {"parent": ["in", orders]})
                frappe.db.delete("RG Meal Order", {"name": ["in", orders]})
            frappe.db.delete("RG Meal Item", {"parent": round_["name"]})
            frappe.db.delete("RG Meal Round", {"name": round_["name"]})
            frappe.db.commit()

    def test_group_meeting_reminder_only_unsubmitted_reporter_and_deduplicated(self):
        from research_group.services import assign_reporter, publish_meeting
        frappe.set_user(self.users["secretary"])
        meeting = frappe.get_doc({"doctype": "RG Group Meeting", "title": self.prefix,
            "start_at": now_datetime() + timedelta(hours=2), "end_at": now_datetime() + timedelta(hours=3),
            "deadline": now_datetime() + timedelta(hours=1), "location": "虚构会议室"}).insert()
        submitted = assign_reporter(meeting.name, self.users["one"])
        assign_reporter(meeting.name, self.users["two"])
        frappe.db.set_value("RG Meeting Report", submitted["name"], "latest_version", 1)
        publish_meeting(meeting.name)
        frappe.set_user("Administrator")
        mail.send_deadline_reminders()
        mail.send_deadline_reminders()
        events = frappe.get_all("RG Mail Event", filters={"template": "deadline_reminder",
            "event_key": ["like", "deadline:RG Group Meeting:%"],
            "recipient_user": ["in", list(self.users.values())]}, fields=["recipient_user", "expires_at"])
        self.assertEqual([row.recipient_user for row in events], [self.users["two"]])
        self.assertTrue(events[0].expires_at)

    def test_business_producers_commit_bound_fixed_recipient_events_and_unchanged_saves_deduplicate(self):
        from research_group import meals
        from research_group.policy import APPROVED, save_internal
        from research_group.services import assign_reporter, decide_appointment, publish_meeting

        addresses = {user: frappe.db.get_value("RG Account Profile", self.profiles[key], "contact_email")
                     for key, user in self.users.items()}

        def produced(doctype, name, template, recipients, previous=()):
            frappe.db.commit()
            rows = frappe.get_all("RG Mail Event", filters={"related_doctype": doctype, "related_name": name},
                fields=["name", "template", "recipient_user", "recipient_email", "related_binding",
                        "status", "from_address", "message"], order_by="name")
            fresh = [row for row in rows if row.name not in previous]
            self.assertEqual(len(fresh), len(recipients))
            self.assertEqual({row.recipient_user for row in fresh}, set(recipients))
            binding = mail._source_binding(template, doctype, name)
            self.assertNotIn(binding, ("", "missing", "expired"))
            for row in fresh:
                self.assertEqual(row.template, template)
                self.assertEqual(row.related_binding, binding)
                self.assertEqual(row.from_address, self.configuration["rg_mail_from"])
                self.assertFalse(row.message)
                self.assertIn(row.status, {"queued", "unconfigured"})
                if row.recipient_user in addresses:
                    self.assertEqual((row.recipient_email, row.status), (addresses[row.recipient_user], "queued"))
                    self.assertNotIn("仅在站内保留的议题", mail.decrypt(self.event(row.name).body_ciphertext))
                    self.assertEqual(self.process(row.name).status, "captured")
            return {row.name for row in rows}

        frappe.set_user(self.users["teacher"])
        topic = frappe.get_doc({"doctype": "RG Topic", "student": self.users["one"],
            "title": self.prefix + "通知入口课题", "direction": "虚构方向", "priority": "中",
            "next_milestone": "初始里程碑", "status": "进行中"}).insert()
        frappe.set_user(self.users["one"])
        appointment = frappe.get_doc({"doctype": "RG Appointment", "topic": topic.name,
            "teacher": self.users["teacher"], "start_at": now_datetime() + timedelta(days=2),
            "end_at": now_datetime() + timedelta(days=2, hours=1), "agenda": "仅在站内保留的议题"}).insert()
        recipients = {self.users["one"], self.users["teacher"]}
        pending = produced(appointment.doctype, appointment.name, "appointment_status", recipients)
        # Appointment no-op saves are prohibited even on the trusted path.
        with self.assertRaises(frappe.ValidationError):
            save_internal(appointment.reload())
        frappe.db.commit()
        self.assertEqual(set(frappe.get_all("RG Mail Event", filters={"related_doctype": appointment.doctype,
            "related_name": appointment.name}, pluck="name")), pending)
        frappe.set_user(self.users["teacher"])
        decide_appointment(appointment.name, APPROVED, "已确认")
        produced(appointment.doctype, appointment.name, "appointment_status", recipients, previous=pending)

        frappe.set_user(self.users["secretary"])
        members = {row["user"] for row in mail.recipient_options()["members"]}
        self.assertTrue(set(self.users.values()).issubset(members))
        meeting = frappe.get_doc({"doctype": "RG Group Meeting", "title": self.prefix + "入口组会",
            "start_at": now_datetime() + timedelta(days=3), "end_at": now_datetime() + timedelta(days=3, hours=1),
            "deadline": now_datetime() + timedelta(days=2), "location": "虚构会议室"}).insert()
        assign_reporter(meeting.name, self.users["one"])
        publish_meeting(meeting.name)
        meeting_events = produced(meeting.doctype, meeting.name, "meeting_published", members)
        meeting.reload().save()
        frappe.db.commit()
        self.assertEqual(set(frappe.get_all("RG Mail Event", filters={"related_doctype": meeting.doctype,
            "related_name": meeting.name}, pluck="name")), meeting_events)

        draft = meals.create_round(self.prefix + "入口工作餐", deadline=now_datetime() + timedelta(days=1))
        try:
            round_ = meals.publish_round(draft["name"], draft["revision"])
            meal_events = produced("RG Meal Round", round_["name"], "meal_published", members)
            meals.set_deadline(round_["name"], round_["deadline"], round_["revision"])
            frappe.db.commit()
            self.assertEqual(set(frappe.get_all("RG Mail Event", filters={"related_doctype": "RG Meal Round",
                "related_name": round_["name"]}, pluck="name")), meal_events)
        finally:
            frappe.set_user("Administrator")
            frappe.db.delete("RG Meal Item", {"parent": draft["name"]})
            frappe.db.delete("RG Meal Round", {"name": draft["name"]})
            frappe.db.commit()

    def test_teacher_meeting_producer_failure_allows_owner_retry_until_teacher_role_is_revoked(self):
        from research_group.policy import TEACHER
        from research_group.services import assign_reporter, publish_meeting

        self.assertIn(TEACHER, frappe.get_roles(self.users["teacher"]))
        self.assertNotIn(SECRETARY, frappe.get_roles(self.users["teacher"]))
        frappe.set_user(self.users["teacher"])
        meeting = frappe.get_doc({"doctype": "RG Group Meeting", "title": self.prefix + "教师组会",
            "start_at": now_datetime() + timedelta(days=3), "end_at": now_datetime() + timedelta(days=3, hours=1),
            "deadline": now_datetime() + timedelta(days=2), "location": "虚构会议室"}).insert()
        assign_reporter(meeting.name, self.users["one"])
        publish_meeting(meeting.name)
        frappe.db.commit()
        events = frappe.get_all("RG Mail Event", filters={"related_doctype": meeting.doctype,
            "related_name": meeting.name, "recipient_user": ["in", [self.users["one"], self.users["two"]]]},
            fields=["name", "recipient_user", "template", "actor", "related_binding", "status"])
        self.assertEqual(len(events), 2)
        by_user = {row.recipient_user: row.name for row in events}
        self.configuration["rg_mail_mode"] = "smtp"
        for row in events:
            self.assertEqual((row.template, row.actor, row.status),
                             ("meeting_published", self.users["teacher"], "queued"))
            self.assertEqual(row.related_binding, mail._source_binding(row.template, meeting.doctype, meeting.name))
            transport = CaptureSMTP(smtplib.SMTPDataError(550, b"isolated known rejection"))
            with patch.object(mail, "_smtp_session", return_value=transport):
                self.assertEqual(self.process(row.name).status, "failed")

        first, second = by_user[self.users["one"]], by_user[self.users["two"]]
        frappe.set_user(self.users["one"])
        self.assertEqual(mail.automatic_mail_status(include_recovery=1)["recovery"].get(first), "retry")
        self.assertEqual(mail.retry_event(first)["status"], "queued")
        self.configuration["rg_mail_mode"] = "capture"
        self.assertEqual(self.process(first).status, "captured")

        frappe.set_user("Administrator")
        teacher = frappe.get_doc("User", self.users["teacher"])
        teacher.set("roles", [{"role": STUDENT}])
        teacher.save(ignore_permissions=True)
        frappe.db.commit()
        self.assertTrue(frappe.db.get_value("User", teacher.name, "enabled"))
        self.assertTrue(frappe.db.get_value("RG Member", teacher.name, "active"))
        frappe.set_user(self.users["two"])
        self.assertNotIn(second, mail.automatic_mail_status(include_recovery=1)["recovery"])
        with self.assertRaises(frappe.ValidationError):
            mail.retry_event(second)
        self.assertEqual((self.event(second).status, self.event(second).attempts), ("failed", 1))

    def _race(self, jobs):
        frappe.db.commit()
        parent = Path(os.environ.get("RG_NOTIFICATION_TEST_WORKDIR") or frappe.get_site_path(
            "private", "tasks", "20261108-fdu-accounts-meals", "scratch", "notification-races")).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        script = "from research_group.tests.test_notifications import notification_race_worker; notification_race_worker()"
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=parent) as folder:
            root, processes = Path(folder), []
            try:
                for index, job in enumerate(jobs):
                    processes.append(subprocess.Popen([
                        sys.executable, "-c", script, frappe.local.site, str(Path(frappe.local.sites_path).resolve()),
                        str(root), str(index), json.dumps({**job, "request_ip": self.prefix}),
                    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                deadline = time.monotonic() + 30
                while not all((root / f"{index}.ready").exists() for index in range(len(jobs))):
                    if time.monotonic() > deadline or any(proc.poll() is not None for proc in processes):
                        self.fail("independent mail connections did not reach the snapshot barrier")
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

    def test_independent_double_confirm_has_one_recipient_event_each(self):
        preview = self.preview()
        job = {"action": "confirm", "user": self.users["secretary"],
               "message": preview["name"], "digest": preview["confirmation_digest"]}
        results = self._race([job, job])
        self.assertEqual([row["ok"] for row in results], [True, True], results)
        self.assertEqual(results[0]["events"], results[1]["events"])
        self.assertEqual(frappe.db.count("RG Mail Event", {"message": preview["name"]}), 2)

    def test_independent_double_automatic_retry_has_one_requeue_and_audit(self):
        notice = self.bound_notice("retry-race")
        mail._update(notice["name"], status="failed", attempts=1)
        job = {"action": "retry", "user": self.users["one"], "event": notice["name"]}
        results = self._race([job, job])
        self.assertEqual(sum(row["ok"] for row in results), 1, results)
        self.assertEqual(self.event(notice["name"]).status, "queued")
        self.assertEqual(frappe.db.count("RG Account Audit", {"detail": notice["name"],
            "action": "automatic_mail_retried", "actor": self.users["one"]}), 1)

    def test_independent_recipient_revocation_while_worker_waits_cancels_manual_notice(self):
        confirmed = self.confirmed([self.users["one"]])
        event = confirmed["events"][0]["name"]
        results = self._race([
            {"action": "revoke_recipient", "user": "Administrator", "target": self.users["one"]},
            {"action": "process", "user": self.users["secretary"], "target": self.users["one"],
             "message": confirmed["name"], "event": event, "waiter": True},
        ])
        self.assertEqual([row["ok"] for row in results], [True, True], results)
        self.assertEqual(results[1]["status"], "cancelled")
        self.assertEqual(self.event(event).status, "cancelled")

    def test_independent_waiting_confirmation_cannot_use_cached_secretary_role(self):
        preview = self.preview()
        results = self._race([
            {"action": "revoke", "user": "Administrator", "target": self.users["secretary"]},
            {"action": "confirm", "user": self.users["secretary"], "message": preview["name"],
             "digest": preview["confirmation_digest"], "waiter": True},
        ])
        self.assertEqual([row["ok"] for row in results], [True, False], results)
        self.assertEqual(results[1]["error"], "PermissionError")
        self.assertEqual(frappe.db.count("RG Mail Event", {"message": preview["name"]}), 0)


def notification_race_worker():
    site, sites_path, folder, worker_id, job_json = sys.argv[1:]
    root, job = Path(folder), json.loads(job_json)
    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.flags.in_test = True
    configuration = {"rg_mail_mode": "capture", "rg_mail_from": "notifications@example.invalid"}
    try:
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        frappe.set_user(job["user"])
        frappe.local.request_ip = job["request_ip"]
        if job["action"] in {"revoke", "revoke_recipient"}:
            global_lock()
        else:
            # Cache the old role and establish an RR snapshot before the gate.
            frappe.get_roles(job["user"])
            if job.get("message"):
                frappe.db.sql("SELECT status FROM `tabRG Message` WHERE name=%s", (job["message"],))
            else:
                frappe.db.sql("SELECT status FROM `tabRG Mail Event` WHERE name=%s", (job["event"],))
            if job.get("target"):
                frappe.get_roles(job["target"])
        connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
        (root / f"{worker_id}.ready").write_text("ready", encoding="ascii")
        deadline = time.monotonic() + 30
        while not (root / "start").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("mail barrier")
            time.sleep(0.02)
        with patch.object(mail, "_configuration", return_value=configuration), patch.object(mail, "_schedule"):
            try:
                if job["action"] in {"revoke", "revoke_recipient"}:
                    while not (root / "waiter").exists():
                        if time.monotonic() > deadline:
                            raise TimeoutError("waiting confirmer barrier")
                        time.sleep(0.02)
                    native = frappe.get_doc("User", job["target"])
                    native.set("roles", [] if job["action"] == "revoke_recipient" else [{"role": STUDENT}])
                    native.save(ignore_permissions=True)
                    result = {"ok": True}
                else:
                    if job.get("waiter"):
                        (root / "waiter").write_text("waiting", encoding="ascii")
                    if job["action"] == "retry":
                        result = {"ok": True, "status": mail.retry_event(job["event"])["status"]}
                    elif job["action"] == "process":
                        mail.process_event(job["event"])
                        result = {"ok": True, "status": mail._row(job["event"]).status}
                    else:
                        confirmed = mail.confirm_message(job["message"], job["digest"])
                        result = {"ok": True, "events": sorted(row["name"] for row in confirmed["events"])}
                frappe.db.commit()
            except Exception as error:
                frappe.db.rollback()
                result = {"ok": False, "error": type(error).__name__}
        print(json.dumps({**result, "connection": connection}))
    finally:
        frappe.destroy()
