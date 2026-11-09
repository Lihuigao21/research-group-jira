"""Private, committed mail events; SMTP acceptance is never called delivery.

The queue contains one recipient per event.  No native Email Queue is created:
its automatic retry cannot distinguish a lost SMTP acknowledgement from a
rejection.  This worker makes one transport attempt and preserves uncertainty.
"""
import hashlib
import json
import re
import smtplib
import time
from contextlib import contextmanager
from datetime import timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
from urllib.parse import urlsplit

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, get_url, now_datetime
from frappe.utils.password import decrypt, encrypt

from research_group.ai.security import current_roles, global_lock
from research_group.policy import ROLES, SECRETARY, TEACHER, deny

TOKEN = object()
DOCTYPES = ("RG Message", "RG Mail Event")
KINDS = ("important", "meeting", "discussion", "activity")
MAX_RECIPIENTS = 500
STATUS_LABELS = {
    "queued": "待处理", "preparing": "准备投递", "sending": "投递中",
    "captured": "隔离捕获，未向外发送", "unconfigured": "未配置可用邮件服务",
    "failed": "已知失败，未被SMTP接受", "unknown": "投递结果未知，禁止自动重发",
    "submitted": "SMTP已接受，未验证实际收件", "cancelled": "已取消，未发送",
}
RETRYABLE = {"failed", "unconfigured"}
LINK_REISSUE = {"registration_verification", "registration_approved", "password_reset"}
STATUS_FIELDS = ("name", "template", "status", "attempts", "last_attempt_at", "submitted_at", "creation",
                 "actor", "message", "recipient_user", "recipient_email", "email_version", "expires_at",
                 "from_address", "related_doctype", "related_name", "related_binding")
_MAILBOX = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}\Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value, label, maximum, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        frappe.throw(f"{label}须填写有效文本，最多{maximum}字。")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        frappe.throw(f"{label}含有不允许的控制字符。")
    return value.strip()


def _address(value, fdu_only=False):
    if not isinstance(value, str) or len(value) > 254 or not _MAILBOX.fullmatch(value):
        frappe.throw("邮箱地址无效。")
    value = value.lower()
    if fdu_only and value.rsplit("@", 1)[1] not in {"fudan.edu.cn", "m.fudan.edu.cn"}:
        frappe.throw("请使用复旦官方邮箱。")
    return value


class NotificationDocument(Document):
    # This private controller stores plain text and serialized identities. Only
    # those DocFields opt out of HTML rewriting; this token/site guard and all
    # other native validation still run before persistence. Public views render
    # text nodes, and the worker emits text/plain without an HTML alternative.
    def validate(self):
        if self.flags.get("rg_notification") is not TOKEN:
            deny()
        if self.site != frappe.local.site:
            deny()
        if not self.is_new():
            previous = self.get_doc_before_save()
            fields = ("site", "actor", "subject", "body", "kind", "selector_json", "recipients_json",
                      "confirmation_digest", "authorization_version", "expires_at", "from_address") \
                if self.doctype == "RG Message" else (
                    "site", "actor", "message", "event_key", "template", "recipient_user",
                    "recipient_email", "email_version", "subject", "body_ciphertext", "payload_digest",
                    "expires_at", "from_address",
                    "related_doctype", "related_name", "related_binding",
                )
            if any(self.get(field) != previous.get(field) for field in fields):
                frappe.throw("通知正文、名单和事件身份不可修改，请重新预览。")

    def on_trash(self):
        if self.flags.get("rg_notification") is not TOKEN:
            deny()

    def notify_update(self):
        # Native doctype rooms must not reveal recipient identities or token mail.
        return


def _save(doc, new=False):
    doc.flags.rg_notification = TOKEN
    doc.flags.ignore_version = True
    if new:
        return doc.insert(ignore_permissions=True, ignore_links=True, set_name=doc.get("name"))
    return doc.save(ignore_permissions=True)


def _insert(doctype, **values):
    return _save(frappe.get_doc({"doctype": doctype, "site": frappe.local.site, **values}), new=True)


def _configuration():
    from frappe.config import get_site_config
    return get_site_config(cached=False)


def _settings():
    config = _configuration()
    mode = config.get("rg_mail_mode", "disabled")
    if mode not in {"disabled", "capture", "smtp"}:
        frappe.throw("邮件服务端配置无效。")
    sender = _address(config.get("rg_mail_from") or "noreply@example.invalid")
    return {"mode": mode, "from_address": sender}


def _actor():
    state = global_lock()
    if not current_roles().intersection({TEACHER, SECRETARY}):
        deny()
    return state


def _write(allowed):
    from research_group.accounts import browser_write
    browser_write(allowed)


@contextmanager
def _atomic():
    savepoint = "rg_mail_" + frappe.generate_hash(length=12)
    frappe.db.savepoint(savepoint)
    try:
        yield
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


def _contact(user, require_active=True):
    from research_group.accounts import contact_address
    value = contact_address(user, require_active=require_active)
    if not value:
        return None
    return {"email": _address(value["email"], fdu_only=True), "email_version": int(value["email_version"])}


def _members():
    # Caller holds the authorization gate; every identity read is a current read.
    rows = frappe.db.sql(
        "SELECT m.user,m.display_name FROM `tabRG Member` m INNER JOIN `tabUser` u ON u.name=m.user "
        "WHERE m.active=1 AND u.enabled=1 ORDER BY m.user LIMIT %s LOCK IN SHARE MODE",
        (MAX_RECIPIENTS + 1,), as_dict=True,
    )
    if len(rows) > MAX_RECIPIENTS:
        frappe.throw(f"此通知入口目前支持最多{MAX_RECIPIENTS}名启用组员，超出规模请联系管理员。")
    members = []
    for row in rows:
        assigned = frappe.db.sql(
            "SELECT role FROM `tabHas Role` WHERE parent=%s AND parenttype='User' LOCK IN SHARE MODE",
            (row.user,),
        )
        assigned = sorted({item[0] for item in assigned}.intersection(ROLES))
        if not assigned:
            continue
        contact = _contact(row.user)
        members.append({"user": row.user, "display_name": row.display_name, "roles": assigned,
                        "email": contact["email"] if contact else None,
                        "email_version": contact["email_version"] if contact else 0,
                        "eligible": bool(contact),
                        "unavailable_reason": "" if contact else "联系邮箱尚未验证"})
    return members


@frappe.whitelist(methods=["GET", "POST"])
def recipient_options():
    _actor()
    return {"members": _members(), "roles": list(ROLES), "configuration": _settings()}


def _selector(recipients, recipient_role, all_members):
    if isinstance(recipients, str):
        try:
            recipients = json.loads(recipients)
        except ValueError:
            frappe.throw("收件人名单格式无效。")
    if all_members in ("0", "1"):
        all_members = all_members == "1"
    if type(all_members) not in {bool, int} or all_members not in {0, 1}:
        frappe.throw("请选择有效的收件范围。")
    if recipients is None:
        recipients = []
    if (not isinstance(recipients, list) or len(recipients) > MAX_RECIPIENTS
            or any(not isinstance(item, str) or len(item) > 140 for item in recipients)
            or len(recipients) != len(set(recipients))):
        frappe.throw("收件人名单无效或有重复。")
    recipient_role = recipient_role or ""
    if recipient_role and recipient_role not in ROLES:
        frappe.throw("收件人角色无效。")
    if int(bool(recipients)) + int(bool(recipient_role)) + int(bool(all_members)) != 1:
        frappe.throw("请选择全部成员、一个角色或具体成员中的一种范围。")
    return {"recipients": sorted(recipients), "recipient_role": recipient_role, "all_members": bool(all_members)}


def _recipients(selector):
    members = _members()
    if selector["recipients"]:
        wanted = set(selector["recipients"])
        members = [member for member in members if member["user"] in wanted]
        if len(members) != len(wanted):
            frappe.throw("名单含未启用或不在课题组的成员，请重新选择。")
    elif selector["recipient_role"]:
        members = [member for member in members if selector["recipient_role"] in member["roles"]]
    if not members:
        frappe.throw("收件范围内没有已启用成员。")
    if any(not member["eligible"] for member in members):
        frappe.throw("名单中有成员尚未验证联系邮箱，请先完成邮箱验证。")
    return members


def _preview_limits(actor):
    recent = now_datetime() - timedelta(minutes=1)
    count = frappe.db.sql("SELECT COUNT(*) FROM `tabRG Message` WHERE actor=%s AND creation>=%s LOCK IN SHARE MODE",
                         (actor, recent))[0][0]
    if count >= 10:
        frappe.throw("预览请求过于频繁，请稍后再试。", frappe.TooManyRequestsError)


def _preview_output(doc, include_events=True):
    recipients = json.loads(doc.recipients_json)
    output = {"name": doc.name, "subject": doc.subject, "body": doc.body, "kind": doc.kind,
              "status": doc.status, "recipients": [
                  {key: row[key] for key in ("user", "display_name", "email")} for row in recipients],
              "recipient_count": len(recipients), "confirmation_digest": doc.confirmation_digest,
              "expires_at": doc.expires_at, "published_at": doc.published_at,
              "from_address": doc.from_address}
    if include_events and doc.status == "published":
        events = frappe.db.sql("SELECT * FROM `tabRG Mail Event` WHERE message=%s ORDER BY recipient_user "
                               "LOCK IN SHARE MODE", (doc.name,), as_dict=True)
        output["events"] = [_event_output(event) for event in events]
    return output


@frappe.whitelist(methods=["POST"])
def preview_message(subject, body, recipients=None, recipient_role=None, all_members=False, kind="important"):
    _write({"subject", "body", "recipients", "recipient_role", "all_members", "kind"})
    state = _actor()
    subject = _text(subject, "主题", 200)
    if "\n" in subject or "\t" in subject:
        frappe.throw("主题不能含换行。")
    body = _text(body, "正文", 4000)
    if kind not in KINDS:
        frappe.throw("通知类型无效。")
    selector = _selector(recipients, recipient_role, all_members)
    selected = _recipients(selector)
    _preview_limits(frappe.session.user)
    name = frappe.generate_hash(length=32)
    expires = now_datetime() + timedelta(minutes=15)
    sender = _settings()["from_address"]
    digest = _digest({"name": name, "actor": frappe.session.user, "subject": subject, "body": body,
                      "kind": kind, "recipients": selected, "selector": selector,
                      "authorization": state.auth_version, "expires": expires, "from": sender})
    doc = _insert("RG Message", name=name, actor=frappe.session.user, subject=subject, body=body, kind=kind,
                  selector_json=_json(selector), recipients_json=_json(selected), status="preview",
                  confirmation_digest=digest, authorization_version=state.auth_version,
                  expires_at=expires, from_address=sender)
    return _preview_output(doc)


def _message(name):
    if not isinstance(name, str) or len(name) > 140:
        deny()
    rows = frappe.db.sql("SELECT name FROM `tabRG Message` WHERE name=%s FOR UPDATE", (name,))
    if not rows:
        deny()
    doc = frappe.get_doc("RG Message", name, for_update=True)
    if doc.actor != frappe.session.user or doc.site != frappe.local.site:
        deny()
    return doc


def _event_limits(actor, addition):
    start = now_datetime().replace(hour=0, minute=0, second=0, microsecond=0)
    total, own = frappe.db.sql("SELECT COUNT(*),COALESCE(SUM(actor=%s),0) FROM `tabRG Mail Event` "
                              "WHERE creation>=%s LOCK IN SHARE MODE", (actor, start))[0]
    if total + addition > 10000 or own + addition > 2000:
        frappe.throw("今日通知额度已用完，请稍后再试。", frappe.TooManyRequestsError)


def _schedule(first_event=None):
    # Savepoint rollback may leave a callback registered.  The worker only reads
    # committed rows; a discarded event can therefore never reach transport.
    if frappe.flags.get("rg_mail_enqueue_pending"):
        return
    frappe.flags.rg_mail_enqueue_pending = True

    def clear():
        frappe.flags.rg_mail_enqueue_pending = False

    def enqueue():
        clear()
        frappe.enqueue("research_group.notifications.drain_events", queue="short", timeout=180,
                       job_id="rg-mail:" + frappe.local.site + ((":" + first_event) if first_event else ""),
                       deduplicate=True)

    frappe.db.after_commit.add(enqueue)
    frappe.db.after_rollback.add(clear)


def _event_output(row):
    return {"name": row.name, "recipient_user": row.recipient_user, "recipient_email": row.recipient_email,
            "status": row.status, "status_label": STATUS_LABELS.get(row.status, row.status),
            "attempts": int(row.attempts or 0), "error_class": row.error_class or "",
            "last_attempt_at": row.last_attempt_at, "submitted_at": row.submitted_at,
            "retry_allowed": row.status in RETRYABLE and int(row.attempts or 0) < 3}


def _new_event(event_key, actor, subject, body, recipient_user="", recipient_email="", email_version=0,
               template="", message="", expires_at=None, from_address="", related_doctype="",
               related_name="", related_binding=""):
    event_key = _text(event_key, "事件标识", 1000)
    if len(event_key) > 200:
        event_key = "sha256:" + hashlib.sha256(event_key.encode("utf-8")).hexdigest()
    name = hashlib.sha256((frappe.local.site + ":" + event_key).encode("utf-8")).hexdigest()[:32]
    payload = {"actor": actor, "subject": subject, "body": body, "recipient_user": recipient_user,
               "recipient_email": recipient_email, "email_version": email_version,
               "template": template, "message": message, "expires_at": expires_at,
               "from_address": from_address, "related_doctype": related_doctype,
               "related_name": related_name, "related_binding": related_binding}
    existing = frappe.db.sql("SELECT name,payload_digest FROM `tabRG Mail Event` WHERE name=%s FOR UPDATE",
                             (name,), as_dict=True)
    if existing:
        if existing[0].payload_digest != _digest(payload):
            frappe.throw("同一通知事件不能改变内容或收件人。")
        return frappe.get_doc("RG Mail Event", name, for_update=True)
    _event_limits(actor, 1)
    return _insert("RG Mail Event", name=name, actor=actor, event_key=event_key, message=message,
                   template=template, recipient_user=recipient_user, recipient_email=recipient_email,
                   email_version=email_version, subject=subject, body_ciphertext=encrypt(body),
                   payload_digest=_digest(payload), status="queued" if recipient_email else "unconfigured",
                   attempts=0, error_class="" if recipient_email else "contact_unverified",
                   expires_at=expires_at, from_address=from_address, related_doctype=related_doctype,
                   related_name=related_name, related_binding=related_binding)


@frappe.whitelist(methods=["POST"])
def confirm_message(name, confirmation_digest):
    _write({"name", "confirmation_digest"})
    state = _actor()
    doc = _message(name)
    if not isinstance(confirmation_digest, str) or confirmation_digest != doc.confirmation_digest:
        deny()
    if doc.status == "published":
        return _preview_output(doc)
    if doc.status != "preview" or get_datetime(doc.expires_at) <= now_datetime():
        frappe.throw("预览已过期，请重新检查正文和名单。")
    if int(state.auth_version) != int(doc.authorization_version):
        frappe.throw("组员或权限已变化，请重新预览。", frappe.PermissionError)
    selected = _recipients(json.loads(doc.selector_json))
    if _json(selected) != doc.recipients_json or _settings()["from_address"] != doc.from_address:
        frappe.throw("收件名单、联系邮箱或发件身份已变化，请重新预览。")
    _event_limits(doc.actor, len(selected))
    with _atomic():
        for recipient in selected:
            _new_event(f"message:{doc.name}:{recipient['user']}", doc.actor, doc.subject, doc.body,
                       recipient_user=recipient["user"], recipient_email=recipient["email"],
                       email_version=recipient["email_version"], template="manual", message=doc.name,
                       from_address=doc.from_address)
        doc.status, doc.published_at = "published", now_datetime()
        _save(doc)
        _schedule()
    return _preview_output(doc)


@frappe.whitelist(methods=["GET", "POST"])
def get_message(name):
    _actor()
    return _preview_output(_message(name))


@frappe.whitelist(methods=["GET", "POST"])
def list_messages(limit=20):
    _actor()
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        frappe.throw("查询数量无效。")
    if not 1 <= limit <= 50:
        frappe.throw("每次最多查询50条。")
    rows = frappe.db.sql("SELECT name,subject,kind,status,creation,published_at,recipients_json "
                         "FROM `tabRG Message` WHERE actor=%s ORDER BY creation DESC LIMIT %s LOCK IN SHARE MODE",
                         (frappe.session.user, limit), as_dict=True)
    return {"messages": [{**{key: row.get(key) for key in (
        "name", "subject", "kind", "status", "creation", "published_at")},
        "recipient_count": len(json.loads(row.recipients_json))} for row in rows]}


def _automatic_status(row):
    # Authentication mail may contain a one-use secret. Even the sender gets
    # only delivery state through this view, without address or source metadata.
    result = {"name": row.name, "template": row.template, "status": row.status,
            "status_label": STATUS_LABELS.get(row.status, row.status), "attempts": int(row.attempts or 0),
    }
    # This small public view is plain JSON even before Frappe's HTTP encoder;
    # internal account bootstrap and privacy checks use the same contract.
    for field, value in (("last_attempt_at", row.last_attempt_at), ("submitted_at", row.submitted_at),
                         ("created_at", row.creation)):
        result[field] = str(value) if value is not None else None
    return result


def automatic_status_rows(user_ids=(), registration_ids=(), include_recovery=False):
    """Internal bounded batch for an account service that already authorized its targets."""
    if (not isinstance(user_ids, (list, tuple)) or not isinstance(registration_ids, (list, tuple))
            or len(user_ids) > 200 or len(registration_ids) > 200
            or any(not isinstance(item, str) or not 1 <= len(item) <= 140
                   for item in (*user_ids, *registration_ids))):
        deny()
    users, registrations = sorted(set(user_ids)), sorted(set(registration_ids))
    result = {"users": {user: [] for user in users},
              "registrations": {name: [] for name in registrations}}
    if include_recovery:
        result["recovery"] = {}

    def append_status(destination, row):
        destination.append(_automatic_status(row))
        if include_recovery:
            kind = _automatic_retry_kind(row)
            if kind:
                result["recovery"][row.name] = kind
    if users:
        placeholders = ",".join(["%s"] * len(users))
        target = ("COALESCE(NULLIF(e.recipient_user,''),CASE "
                  "WHEN e.related_doctype='User' THEN e.related_name "
                  "WHEN e.related_doctype='RG Account Profile' THEN p.user "
                  "WHEN e.related_doctype='RG Registration' THEN r.created_user END,'')")
        rows = frappe.db.sql(
            "SELECT * FROM (SELECT " + ",".join("e." + field for field in STATUS_FIELDS) + "," +
            target + " AS target,ROW_NUMBER() OVER(PARTITION BY " + target +
            " ORDER BY e.creation DESC,e.name DESC) AS position FROM `tabRG Mail Event` e "
            "LEFT JOIN `tabRG Account Profile` p ON e.related_doctype='RG Account Profile' AND p.name=e.related_name "
            "LEFT JOIN `tabRG Registration` r ON e.related_doctype='RG Registration' AND r.name=e.related_name "
            "WHERE e.site=%s AND e.template!='manual' AND COALESCE(e.message,'')='' AND "
            f"(e.recipient_user IN ({placeholders}) OR (e.related_doctype='User' AND e.related_name IN ({placeholders})) "
            f"OR p.user IN ({placeholders}) OR r.created_user IN ({placeholders}))) ranked "
            "WHERE ranked.position<=20 ORDER BY ranked.target,ranked.position",
            (frappe.local.site, *users, *users, *users, *users), as_dict=True,
        )
        for row in rows:
            if row.target in result["users"]:
                append_status(result["users"][row.target], row)
    if registrations:
        placeholders = ",".join(["%s"] * len(registrations))
        rows = frappe.db.sql(
            "SELECT * FROM (SELECT " + ",".join(STATUS_FIELDS) + ","
            "related_name AS target,ROW_NUMBER() OVER(PARTITION BY related_name ORDER BY creation DESC,name DESC) "
            "AS position FROM `tabRG Mail Event` WHERE site=%s AND template!='manual' "
            "AND COALESCE(message,'')='' AND related_doctype='RG Registration' "
            f"AND related_name IN ({placeholders})) ranked WHERE ranked.position<=20 "
            "ORDER BY ranked.target,ranked.position", (frappe.local.site, *registrations), as_dict=True,
        )
        for row in rows:
            append_status(result["registrations"][row.target], row)
    return result


@frappe.whitelist(methods=["GET", "POST"])
def automatic_mail_status(user=None, include_recovery=0):
    from research_group.accounts import _guard, _ordinary_student
    _guard()
    target = user or frappe.session.user
    if not isinstance(target, str) or not 1 <= len(target) <= 140:
        deny()
    if target != frappe.session.user:
        _guard(secretary=True)
        _ordinary_student(target)
    include_recovery = _recovery_flag(include_recovery)
    status = automatic_status_rows([target], include_recovery=include_recovery)
    result = {"events": status["users"][target]}
    if include_recovery:
        result["recovery"] = status["recovery"]
    return result


@frappe.whitelist(methods=["GET", "POST"])
def registration_mail_status(application, include_recovery=0):
    from research_group.accounts import _guard, _ordinary_student
    _guard(secretary=True)
    if not isinstance(application, str) or not 1 <= len(application) <= 140:
        deny()
    rows = frappe.db.sql("SELECT created_user FROM `tabRG Registration` WHERE name=%s FOR UPDATE",
                         (application,), as_dict=True)
    if not rows:
        deny()
    if rows[0].created_user and rows[0].created_user != frappe.session.user:
        _ordinary_student(rows[0].created_user)
    include_recovery = _recovery_flag(include_recovery)
    status = automatic_status_rows(registration_ids=[application], include_recovery=include_recovery)
    result = {"events": status["registrations"][application]}
    if include_recovery:
        result["recovery"] = status["recovery"]
    return result


def _recovery_flag(value):
    if value not in (0, 1, "0", "1", False, True):
        deny()
    return value in (1, "1", True)


def _local_link(value):
    value = _text(value, "链接", 2048)
    expected, actual = urlsplit(get_url(allow_header_override=False)), urlsplit(value)
    if (actual.scheme not in {"http", "https"} or actual.scheme != expected.scheme
            or actual.netloc != expected.netloc or actual.username or actual.password
            or not actual.path.startswith(("/rg_join", "/desk/", "/app/"))):
        frappe.throw("通知链接必须为服务器配置的本站地址。")
    return value


def _template(template, values):
    if not isinstance(values, dict):
        frappe.throw("通知模板参数无效。")
    contracts = {
        "registration_verification": ({"link", "display_name"}, "验证课题组入组邮箱",
            "{display_name}，请打开以下本站链接确认邮箱。验证后仍需秘书审核。\n{link}"),
        "contact_verification": ({"link", "display_name"}, "验证课题组联系邮箱",
            "{display_name}，请打开以下本站链接确认新的联系邮箱。\n{link}"),
        "registration_approved": ({"link", "display_name", "login_id"}, "课题组入组审核已通过",
            "{display_name}，入组审核已通过。固定登录ID：{login_id}。请通过本站链接设置网站密码。\n{link}"),
        "password_reset": ({"link", "display_name", "login_id"}, "重设课题组网站密码",
            "{display_name}，固定登录ID：{login_id}。如由本人发起，请通过本站链接重设网站密码。\n{link}"),
        "contact_changed": ({"display_name", "login_id", "contact_email"}, "课题组联系邮箱已更新",
            "{display_name}，网站联系邮箱已更新为 {contact_email}。固定登录ID仍为 {login_id}。"
            "如非本人操作，请联系管理员核实。"),
        "registration_rejected": ({"display_name"}, "课题组入组申请审核结果",
            "{display_name}，入组申请未通过审核。如需核实，请联系课题组秘书。"),
        "account_status": ({"display_name", "status", "login_id"}, "课题组网站账号状态更新",
            "{display_name}，网站账号 {login_id} 的状态已更新为：{status}。如需核实，请联系课题组秘书。"),
        "appointment_status": ({"status", "start_at", "link"}, "课题组讨论预约状态更新",
            "讨论预约状态：{status}。开始时间（Asia/Shanghai）：{start_at}。请登录网站查看。\n{link}"),
        "meeting_published": ({"title", "start_at", "link"}, "课题组组会安排已发布",
            "组会：{title}。开始时间（Asia/Shanghai）：{start_at}。请登录网站查看安排。\n{link}"),
        "meal_published": ({"title", "deadline", "link"}, "课题组工作餐已开放",
            "工作餐：{title}。截止时间（Asia/Shanghai）：{deadline}。请登录网站填写。\n{link}"),
        "deadline_reminder": ({"title", "deadline", "link"}, "课题组填写截止提醒",
            "{title} 将于 {deadline}（Asia/Shanghai）截止。请登录网站检查本人填写状态。\n{link}"),
    }
    if template not in contracts or set(values) != contracts[template][0]:
        frappe.throw("通知模板或参数不在固定清单中。")
    clean = {key: (_local_link(value) if key == "link" else _text(value, "模板字段", 300))
             for key, value in values.items()}
    if "contact_email" in clean:
        clean["contact_email"] = _address(clean["contact_email"], fdu_only=True)
    _, subject, body = contracts[template]
    return subject, body.format(**clean)


def queue_template(event_key, template, recipient_user=None, recipient_email=None, values=None,
                   related_doctype=None, related_name=None, expires_at=None):
    """Internal only; callers supply fixed-template values, never SMTP settings.

    A missing verified contact is recorded without aborting the business event.
    Explicit email is limited to verification/security notices, never bulk mail.
    """
    global_lock()
    subject, body = _template(template, values or {})
    if bool(recipient_user) == bool(recipient_email):
        frappe.throw("模板通知必须指定唯一收件身份。")
    version, address = 0, ""
    if recipient_email:
        if template not in {"registration_verification", "contact_verification", "contact_changed",
                             "registration_rejected"}:
            frappe.throw("此模板须发给已验证的成员联系邮箱。")
        address = _address(recipient_email, fdu_only=True)
    else:
        contact = _contact(recipient_user, require_active=template != "account_status")
        if contact:
            address, version = contact["email"], contact["email_version"]
    related_doctype, related_name = related_doctype or "", related_name or ""
    binding = _source_binding(template, related_doctype, related_name)
    if binding in {"missing", "expired"}:
        deny()
    with _atomic():
        doc = _new_event(event_key, frappe.session.user, subject, body,
                         recipient_user=recipient_user or "", recipient_email=address, email_version=version,
                         template=template, expires_at=expires_at, from_address=_settings()["from_address"],
                         related_doctype=related_doctype, related_name=related_name,
                         related_binding=binding)
        _schedule()
    return {"name": doc.name, "status": doc.status}


def _source_binding(template, doctype, name):
    """Bind fixed templates to bounded current source metadata, never its body."""
    if not doctype and not name:
        return ""
    if not isinstance(name, str) or not 1 <= len(name) <= 140:
        deny()
    contracts = {
        "registration_verification": {"RG Registration": ("status", "token_hash", "token_version", "expires_at")},
        "registration_rejected": {"RG Registration": ("status", "email", "reviewed_by", "reviewed_at")},
        "contact_verification": {"RG Account Profile": ("user", "pending_email", "pending_token_hash", "pending_expires")},
        "contact_changed": {"RG Account Profile": ("user", "contact_email", "contact_verified", "email_version")},
        "registration_approved": {"User": ("enabled", "reset_password_key", "last_reset_password_key_generated_on")},
        "password_reset": {"User": ("enabled", "reset_password_key", "last_reset_password_key_generated_on")},
        "account_status": {"User": ("enabled",)},
        "appointment_status": {"RG Appointment": ("student", "teacher", "status", "start_at")},
        "meeting_published": {"RG Group Meeting": ("title", "status", "start_at", "deadline")},
        "meal_published": {"RG Meal Round": ("title", "status", "deadline")},
        "deadline_reminder": {source: ("title", "status", "deadline") for source in (
            "RG Survey", "RG Meal Round", "RG Group Meeting")},
    }
    fields = contracts.get(template, {}).get(doctype)
    if not fields:
        deny()
    columns = ",".join(f"`{field}`" for field in fields)
    rows = frappe.db.sql(f"SELECT {columns} FROM `tab{doctype}` WHERE name=%s LOCK IN SHARE MODE",
                         (name,), as_dict=True)
    if not rows:
        return "missing"
    data = rows[0]
    expiry = data.get("expires_at") or data.get("pending_expires")
    if template in {"registration_approved", "password_reset"}:
        from research_group.accounts import password_link_expires
        expiry = password_link_expires(data)
        if not data.enabled or not data.reset_password_key or not data.last_reset_password_key_generated_on:
            return "expired"
    elif template == "registration_verification":
        if data.status != "pending_verification" or not data.token_hash or not expiry:
            return "expired"
    elif template == "contact_verification":
        if not data.pending_email or not data.pending_token_hash or not expiry:
            return "expired"
    elif template == "deadline_reminder":
        expiry = data.deadline
        if data.status != "已发布" or not expiry:
            return "expired"
    if expiry and get_datetime(expiry) <= now_datetime():
        return "expired"
    return _digest(data)


def _row(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 140:
        deny()
    rows = frappe.db.sql("SELECT * FROM `tabRG Mail Event` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
    if not rows or rows[0].site != frappe.local.site:
        return None
    return rows[0]


def _update(name, **values):
    # Only service-owned constant field names reach this method.
    frappe.db.set_value("RG Mail Event", name, values, update_modified=True)


def _check_event(row):
    if row.expires_at and get_datetime(row.expires_at) <= now_datetime():
        return "expired"
    if row.template == "deadline_reminder" and (not row.related_binding or not row.related_doctype or not row.related_name):
        # Pre-binding reminders cannot prove their current deadline/status.
        # Keep the receipt and cancel rather than infer a source from a URL.
        return "source_unverified"
    if row.message:
        if not current_roles(row.actor).intersection({TEACHER, SECRETARY}):
            return "sender_revoked"
        # A published message is still private to current group members, even
        # if a former member retains an enabled User and a verified mailbox.
        if not current_roles(row.recipient_user):
            return "recipient_revoked"
        messages = frappe.db.sql("SELECT status FROM `tabRG Message` WHERE name=%s LOCK IN SHARE MODE",
                                  (row.message,))
        if not messages or messages[0][0] != "published":
            return "message_unpublished"
    # Parent/source locks precede recipient identity locks, as in the original
    # survey and material services. A deadline edit/close invalidates queued
    # reminders rather than merely changing their eventual expiry.
    if row.related_binding and row.related_binding != _source_binding(
            row.template, row.related_doctype, row.related_name):
        return "authentication_key_changed" if row.template in LINK_REISSUE | {"contact_verification"} else "source_changed"
    if row.template == "deadline_reminder" and _reminder_completed(row):
        return "work_completed"
    if row.template == "registration_verification" and row.related_doctype == "RG Registration":
        address = frappe.db.sql("SELECT email FROM `tabRG Registration` WHERE name=%s LOCK IN SHARE MODE",
                                (row.related_name,))
        if not address or address[0][0] != row.recipient_email:
            return "recipient_changed"
    if row.recipient_user:
        contact = _contact(row.recipient_user, require_active=row.template != "account_status")
        if not contact or contact["email"] != row.recipient_email or contact["email_version"] != row.email_version:
            return "recipient_changed"
    return ""


def _reminder_completed(row):
    """The bound parent is already locked, before recipient identity checks."""
    if row.related_doctype == "RG Survey":
        return bool(frappe.db.sql("SELECT name FROM `tabRG Survey Response` "
            "WHERE survey=%s AND respondent=%s LIMIT 1 LOCK IN SHARE MODE",
            (row.related_name, row.recipient_user)))
    if row.related_doctype == "RG Meal Round":
        from research_group.meals import ACTIVE
        return bool(frappe.db.sql("SELECT name FROM `tabRG Meal Order` "
            "WHERE `round`=%s AND member=%s AND status=%s LIMIT 1 LOCK IN SHARE MODE",
            (row.related_name, row.recipient_user, ACTIVE)))
    if row.related_doctype == "RG Group Meeting":
        return bool(frappe.db.sql("SELECT name FROM `tabRG Meeting Report` "
            "WHERE meeting=%s AND student=%s AND COALESCE(latest_version,0)>0 LIMIT 1 LOCK IN SHARE MODE",
            (row.related_name, row.recipient_user)))
    return False


def _automatic_target(row):
    if row.recipient_user:
        return row.recipient_user, ""
    if row.related_doctype == "RG Account Profile":
        rows = frappe.db.sql("SELECT user FROM `tabRG Account Profile` WHERE name=%s LOCK IN SHARE MODE",
                             (row.related_name,))
        return (rows[0][0], "") if rows else ("", "")
    if row.related_doctype == "RG Registration":
        rows = frappe.db.sql("SELECT created_user FROM `tabRG Registration` WHERE name=%s LOCK IN SHARE MODE",
                             (row.related_name,))
        return ((rows[0][0] or ""), row.related_name) if rows else ("", "")
    return "", ""


def _automatic_retry_kind(row):
    if (row.message or row.status not in RETRYABLE or int(row.attempts or 0) >= 3
            or not row.related_binding or not row.from_address):
        return ""
    try:
        if row.from_address != _settings()["from_address"] or _check_event(row):
            return ""
        target, registration = _automatic_target(row)
        if not target and not registration:
            return ""
        if target:
            if row.template == "account_status":
                from research_group.accounts import _assigned_roles
                if not _assigned_roles(target).intersection(ROLES):
                    return ""
            else:
                current_roles(target)
        if row.actor not in {"Guest", "Administrator"}:
            assigned = current_roles(row.actor)
            if row.template == "meeting_published" and not assigned.intersection({TEACHER, SECRETARY}):
                return ""
            if row.template in {"registration_approved", "registration_rejected", "account_status",
                                "meal_published"} and SECRETARY not in assigned:
                return ""
    except (frappe.PermissionError, frappe.ValidationError):
        return ""
    return "reissue" if row.template in LINK_REISSUE else "retry"


def _retry_automatic(row):
    from research_group import accounts
    accounts._guard()
    target, registration = _automatic_target(row)
    if target != frappe.session.user:
        accounts._guard(secretary=True)
        if target:
            accounts._ordinary_student(target)
        elif not registration:
            deny()
    kind = _automatic_retry_kind(row)
    if not kind:
        frappe.throw("此事件不允许恢复；过期、未知、已接受、撤权或来源变化的通知不得重发。")
    accounts._throttle("automatic_mail_retry", target or registration, identity_limit=5, ip_limit=20)
    with _atomic():
        if kind == "reissue":
            if row.template == "registration_verification":
                source = frappe.get_doc("RG Registration", registration, for_update=True)
                issued = accounts._reissue_registration(source)
            else:
                issued = accounts._issue_password(target, row.template)
            _update(row.name, status="cancelled", error_class="reissued")
            accounts._audit("automatic_mail_reissued", target=target or None, application=registration or None,
                            detail=row.name + ":" + issued["name"])
            return _automatic_status(_row(issued["name"]))
        _update(row.name, status="queued", error_class="")
        accounts._audit("automatic_mail_retried", target=target or None, application=registration or None,
                        detail=row.name)
        _schedule()
    return _automatic_status(_row(row.name))


@frappe.whitelist(methods=["POST"])
def retry_event(name):
    _write({"name"})
    global_lock()
    row = _row(name)
    if not row:
        deny()
    if not row.message:
        return _retry_automatic(row)
    _actor()
    if row.actor != frappe.session.user:
        deny()
    _message(row.message)
    if row.status not in RETRYABLE or int(row.attempts or 0) >= 3:
        frappe.throw("此事件不允许重试；未知或已接受的投递不得重复发送。")
    if _check_event(row):
        frappe.throw("通知授权或联系邮箱已变化，请重新预览。")
    _update(name, status="queued", error_class="")
    _schedule()
    return _event_output(_row(name))


def _smtp_session(settings):
    from frappe.email.doctype.email_account.email_account import EmailAccount
    if frappe.are_emails_muted():
        return None
    account = EmailAccount.find_outgoing(match_by_email=settings["from_address"], _raise_error=False)
    if (not account or _address(account.email_id) != settings["from_address"]
            or account.service == "Frappe Mail"):
        return None
    server = account.get_smtp_server()
    # Explicit bounded transport; no native Email Queue / automatic retry.
    server.timeout = 20
    return server.session


def _close_transport(session):
    try:
        session.close()
    except Exception:
        pass  # Closing a socket cannot change the persisted DATA outcome.


def _finish_preparation(name, attempt, status, error):
    global_lock()
    row = _row(name)
    if row and row.status == "preparing" and int(row.attempts or 0) == attempt:
        _update(name, status=status, error_class=error)
    frappe.db.commit()


def process_event(name):
    """Worker entry; never exposed as a whitelisted HTTP method."""
    global_lock()
    row = _row(name)
    if not row or row.status != "queued":
        frappe.db.commit()
        return
    try:
        invalid = _check_event(row)
    except frappe.PermissionError:
        invalid = "identity_revoked"
    if invalid:
        _update(name, status="cancelled", error_class=invalid)
        frappe.db.commit()
        return
    settings = _settings()
    if settings["mode"] == "disabled":
        _update(name, status="unconfigured", error_class="mail_disabled")
        frappe.db.commit()
        return
    if row.from_address and row.from_address != settings["from_address"]:
        _update(name, status="cancelled", error_class="sender_configuration_changed")
        frappe.db.commit()
        return
    attempt = int(row.attempts or 0) + 1
    _update(name, status="preparing", attempts=attempt, last_attempt_at=now_datetime(),
            error_class="")
    frappe.db.commit()
    if settings["mode"] == "capture":
        global_lock()
        row = _row(name)
        if not row or row.status != "preparing" or int(row.attempts or 0) != attempt:
            frappe.db.commit()
            return
        try:
            invalid = _check_event(row)
        except frappe.PermissionError:
            invalid = "identity_revoked"
        _update(name, status="cancelled" if invalid else "captured", error_class=invalid)
        frappe.db.commit()
        return
    try:
        session = _smtp_session(settings)
        if session is None:
            _finish_preparation(name, attempt, "unconfigured", "smtp_account_unavailable")
            return
    except Exception as error:
        _finish_preparation(name, attempt, "failed", type(error).__name__[:100])
        return
    global_lock()
    row = _row(name)
    if not row or row.status != "preparing" or int(row.attempts or 0) != attempt:
        frappe.db.commit()
        _close_transport(session)
        return
    try:
        invalid = _check_event(row)
    except frappe.PermissionError:
        invalid = "identity_revoked"
    if invalid or _settings() != settings:
        _update(name, status="cancelled", error_class=invalid or "mail_configuration_changed")
        frappe.db.commit()
        _close_transport(session)
        return
    try:
        message = EmailMessage(policy=SMTP)
        message["From"] = settings["from_address"]
        message["To"] = row.recipient_email
        message["Subject"] = row.subject
        message["Date"] = formatdate(localtime=False, usegmt=True)
        message["Message-ID"] = f"<rg-{row.name}@{settings['from_address'].split('@')[1]}>"
        message.set_content(decrypt(row.body_ciphertext), subtype="plain", charset="utf-8")
        wire = message.as_bytes()
    except Exception as error:
        _update(name, status="failed", error_class=type(error).__name__[:100])
        frappe.db.commit()
        _close_transport(session)
        return
    _update(name, status="sending")
    frappe.db.commit()  # No database transaction remains open during SMTP DATA.
    try:
        refused = session.sendmail(settings["from_address"], [row.recipient_email], wire)
        if refused:
            _update(name, status="failed", error_class="SMTPRecipientsRefused")
        else:
            _update(name, status="submitted", error_class="", submitted_at=now_datetime())
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
        _update(name, status="failed", error_class=type(error).__name__[:100])
    except Exception as error:
        # DATA may have been accepted even when the final acknowledgement was
        # lost.  Persist uncertainty and do not place this event back in queue.
        _update(name, status="unknown", error_class=type(error).__name__[:100])
    finally:
        _close_transport(session)
    frappe.db.commit()


def drain_events():
    """At most 20 events / 120 seconds; continue committed backlog in a new job."""
    if not frappe.db.table_exists("RG Mail Event"):
        return
    global_lock()
    stale = now_datetime() - timedelta(minutes=5)
    # No SMTP DATA can have started before the committed sending state. The
    # attempt/status gate below prevents a late preparation worker from sending
    # after a controlled retry has claimed a newer attempt.
    frappe.db.sql("UPDATE `tabRG Mail Event` SET status='failed',error_class='preparation_lost' "
                  "WHERE status='preparing' AND last_attempt_at<%s", (stale,))
    frappe.db.sql("UPDATE `tabRG Mail Event` SET status='unknown',error_class='worker_lost' "
                  "WHERE status='sending' AND last_attempt_at<%s", (stale,))
    names = frappe.db.sql("SELECT name FROM `tabRG Mail Event` WHERE status='queued' "
                          "ORDER BY creation,name LIMIT 20 FOR UPDATE", pluck=True)
    frappe.db.commit()
    started = time.monotonic()
    for name in names:
        if time.monotonic() - started >= 120:
            break
        process_event(name)
    global_lock()
    remaining = frappe.db.sql("SELECT name FROM `tabRG Mail Event` WHERE status='queued' "
                              "ORDER BY creation,name LIMIT 1 LOCK IN SHARE MODE", pluck=True)
    if remaining:
        # Site-wide initial enqueue may already be the running job. The first
        # remaining immutable event identifies the next bounded batch and lets
        # concurrent drains coalesce without waiting for the five-minute cron.
        _schedule(remaining[0])
    frappe.db.commit()


def notify_appointment(doc, method=None):
    """Fixed status-only content; no agenda or research discussion is mailed."""
    old = doc.get_doc_before_save()
    if old and old.status == doc.status:
        return
    if doc.status not in {"待审批", "已批准", "已拒绝", "已取消", "已归档"}:
        return
    values = {"status": doc.status, "start_at": str(doc.start_at),
              "link": get_url(f"/desk/rg-appointment/{doc.name}", allow_header_override=False)}
    for user in sorted({doc.student, doc.teacher}):
        queue_template(f"appointment:{doc.name}:{doc.status}:{doc.modified}:{user}",
                       "appointment_status", recipient_user=user, values=values,
                       related_doctype=doc.doctype, related_name=doc.name)


def notify_meeting(doc, method=None):
    old = doc.get_doc_before_save()
    if doc.status != "已发布":
        return
    schedule_fields = ("title", "status", "start_at", "deadline")
    global_lock()
    binding = _source_binding("meeting_published", doc.doctype, doc.name)
    if old and _digest({field: old.get(field) for field in schedule_fields}) == binding:
        return
    values = {"title": doc.title, "start_at": str(doc.start_at),
              "link": get_url(f"/desk/rg-group-meeting/{doc.name}", allow_header_override=False)}
    for member in _members():
        queue_template(f"meeting:{doc.name}:{binding}:{member['user']}", "meeting_published",
                       recipient_user=member["user"], values=values,
                       related_doctype=doc.doctype, related_name=doc.name)


def notify_meal_round(doc, method=None):
    old = doc.get_doc_before_save()
    if doc.status != "已发布" or (old and old.status != "草稿"):
        return
    values = {"title": doc.title, "deadline": str(doc.deadline or "手动关闭前"),
              "link": get_url("/desk/rg-meals", allow_header_override=False)}
    global_lock()
    for member in _members():
        queue_template(f"meal:{doc.name}:{member['user']}", "meal_published",
                       recipient_user=member["user"], values=values,
                       related_doctype=doc.doctype, related_name=doc.name)


def send_deadline_reminders():
    """Asia/Shanghai site time; <=100 new events and <=30 seconds per invocation."""
    if not frappe.db.table_exists("RG Mail Event") or not frappe.db.table_exists("RG Account Profile"):
        return
    started = time.monotonic()
    global_lock()
    instant, until = now_datetime(), now_datetime() + timedelta(hours=24)
    # Existing survey submissions lock parent -> User and do not take the AI
    # gate. Acquire every survey parent this invocation will need before any
    # member/User locks, then reuse this bounded list throughout the transaction.
    survey_rounds = frappe.db.sql(
        "SELECT name,title,deadline FROM `tabRG Survey` WHERE status='已发布' "
        "AND deadline>%s AND deadline<=%s ORDER BY deadline LIMIT 50 LOCK IN SHARE MODE",
        (instant, until), as_dict=True,
    ) if frappe.db.table_exists("RG Survey") else []
    members = _members()
    emitted = 0
    for doctype, route, response_type, parent_field, user_field in (
        ("RG Survey", "rg-survey", "RG Survey Response", "survey", "respondent"),
        ("RG Meal Round", "rg-meals", "RG Meal Order", "round", "member"),
    ):
        if not frappe.db.table_exists(doctype):
            continue
        rounds = survey_rounds if doctype == "RG Survey" else frappe.db.sql(
            f"SELECT name,title,deadline FROM `tab{doctype}` WHERE status='已发布' "
            "AND deadline>%s AND deadline<=%s ORDER BY deadline LIMIT 50 LOCK IN SHARE MODE",
            (instant, until), as_dict=True,
        )
        for round_ in rounds:
            # Column names come only from this fixed table, never an HTTP input.
            submitted = {row[0] for row in frappe.db.sql(
                f"SELECT `{user_field}` FROM `tab{response_type}` WHERE `{parent_field}`=%s"
                + (" AND status='有效'" if doctype == "RG Meal Round" else "") + " LOCK IN SHARE MODE",
                (round_.name,),
            )}
            for member in members:
                if time.monotonic() - started >= 30:
                    return
                if member["user"] in submitted:
                    continue
                event_key = f"deadline:{doctype}:{round_.name}:{round_.deadline}:{member['user']}"
                stored_key = event_key if len(event_key) <= 200 else (
                    "sha256:" + hashlib.sha256(event_key.encode("utf-8")).hexdigest())
                name = hashlib.sha256((frappe.local.site + ":" + stored_key).encode("utf-8")).hexdigest()[:32]
                if frappe.db.sql("SELECT name FROM `tabRG Mail Event` WHERE name=%s LOCK IN SHARE MODE", (name,)):
                    continue
                queue_template(event_key,
                               "deadline_reminder", recipient_user=member["user"],
                               values={"title": round_.title, "deadline": str(round_.deadline),
                                       "link": get_url(f"/desk/{route}", allow_header_override=False)},
                               expires_at=round_.deadline, related_doctype=doctype, related_name=round_.name)
                emitted += 1
                if emitted >= 100:
                    return
    # Group-meeting reminders concern the assigned student's own material, not
    # every member. Already submitted reports are excluded with a current read.
    meetings = frappe.db.sql(
        "SELECT name,title,deadline FROM `tabRG Group Meeting` WHERE status='已发布' "
        "AND deadline>%s AND deadline<=%s ORDER BY deadline LIMIT 50 LOCK IN SHARE MODE",
        (instant, until), as_dict=True,
    )
    for meeting in meetings:
        reports = frappe.db.sql(
            "SELECT student FROM `tabRG Meeting Report` WHERE meeting=%s "
            "AND COALESCE(latest_version,0)=0 ORDER BY student LIMIT 500 LOCK IN SHARE MODE",
            (meeting.name,), pluck=True,
        )
        for user in reports:
            if time.monotonic() - started >= 30 or emitted >= 100:
                return
            key = f"deadline:RG Group Meeting:{meeting.name}:{meeting.deadline}:{user}"
            normalized = key if len(key) <= 200 else "sha256:" + hashlib.sha256(key.encode()).hexdigest()
            name = hashlib.sha256((frappe.local.site + ":" + normalized).encode()).hexdigest()[:32]
            if frappe.db.sql("SELECT name FROM `tabRG Mail Event` WHERE name=%s LOCK IN SHARE MODE", (name,)):
                continue
            queue_template(key, "deadline_reminder", recipient_user=user,
                           values={"title": meeting.title, "deadline": str(meeting.deadline),
                                   "link": get_url(f"/desk/rg-group-meeting/{meeting.name}",
                                                   allow_header_override=False)},
                           expires_at=meeting.deadline, related_doctype="RG Group Meeting", related_name=meeting.name)
            emitted += 1


def business_event(doc, method=None):
    """Install-safe, transaction-local hook; external work only after commit."""
    if not frappe.db.table_exists("RG Mail Event") or not frappe.db.table_exists("RG Account Profile"):
        return
    handlers = {"RG Appointment": notify_appointment, "RG Group Meeting": notify_meeting,
                "RG Meal Round": notify_meal_round}
    handler = handlers.get(doc.doctype)
    if handler:
        handler(doc, method)


def enqueue_deadline_reminders():
    if not frappe.db.table_exists("RG Mail Event") or not frappe.db.table_exists("RG Account Profile"):
        return
    frappe.enqueue("research_group.notifications.send_deadline_reminders", queue="short", timeout=180,
                   enqueue_after_commit=True, job_id="rg-mail-deadlines:" + frappe.local.site, deduplicate=True)
