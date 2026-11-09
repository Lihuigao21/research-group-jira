"""窄账号服务：邮箱验证不授业务权，原生User身份和密码哈希保持不变。

所有写事务先拿既有AI授权闸门；网络投递及原生会话清理只在提交后运行。
令牌只保存摘要，GET不验证或消费任何账号令牌。
"""
import hashlib
import hmac
import json
import re
import secrets
from functools import wraps
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import frappe
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, get_url, now_datetime, today

from research_group.ai.identity import ResearchGroupUser
from research_group.ai.security import current_roles, global_lock, invalidate
from research_group.policy import SECRETARY, STUDENT, deny, save_internal

ACCOUNT_DOCTYPES = ("RG Registration", "RG Account Profile", "RG Account Audit")
TOKEN = object()
DOMAINS = {"m.fudan.edu.cn", "fudan.edu.cn"}
GENERIC = "若信息符合要求，验证或恢复邮件将发送到对应的已验证邮箱，请检查邮箱。"
SAFE_STUDENT_ROLES = {STUDENT, "All", "Desk User"}


def _atomic(method):
    """A caught service failure must not leave half an account or a mail event."""
    @wraps(method)
    def wrapped(*args, **kwargs):
        savepoint = "rg_account_" + frappe.generate_hash(length=12)
        frappe.db.savepoint(savepoint)
        try:
            return method(*args, **kwargs)
        except Exception:
            frappe.db.rollback(save_point=savepoint)
            raise
    return wrapped


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value, maximum, label, required=False):
    if not isinstance(value, str) or len(value) > maximum or len(value.encode("utf-8")) > maximum * 4:
        frappe.throw(f"{label}格式或长度不符合要求。")
    value = value.strip()
    if (required and not value) or any(ord(c) < 32 and c not in "\n\t" for c in value):
        frappe.throw(f"请填写有效的{label}。")
    if "<" in value or ">" in value:
        frappe.throw(f"{label}仅支持纯文本。")
    return value


def normalize_email(value):
    if not isinstance(value, str) or len(value) > 140:
        frappe.throw("请填写本人复旦官方邮箱。")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._+\-]{0,100}@[a-z0-9.\-]+", value):
        frappe.throw("请填写本人复旦官方邮箱，不含显示名称。")
    if value.rsplit("@", 1)[1] not in DOMAINS:
        frappe.throw("邮箱域名仅允许 m.fudan.edu.cn 或 fudan.edu.cn。")
    return value


def _student_id(value, optional=False):
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,31}", value.strip()):
        frappe.throw("请填写3至32位学号，仅支持字母、数字、下划线或连字符。")
    return value.strip().upper()


def no_permission(doc=None, ptype=None, user=None, **kwargs):
    return False


def no_query(user=None):
    return "1=0"


class AccountDocument(Document):
    def validate(self):
        if self.flags.get("rg_accounts") is not TOKEN:
            deny()
        if self.doctype == "RG Account Audit" and not self.is_new():
            deny()
        if self.doctype == "RG Account Profile" and not self.is_new():
            if self.user != self.get_doc_before_save().user:
                deny()

    def on_trash(self):
        # 账号生命周期保留历史，不让原生删除/Deleted Document复制秘密。
        if self.flags.get("rg_accounts") is not TOKEN:
            deny()

    def notify_update(self):
        return


def _save(doc, new=False):
    doc.flags.rg_accounts = TOKEN
    doc.flags.ignore_version = True
    return doc.insert(ignore_permissions=True) if new else doc.save(ignore_permissions=True)


def _insert(doctype, **values):
    return _save(frappe.get_doc({"doctype": doctype, **values}), new=True)


def _audit(action, target=None, application=None, detail=""):
    return _insert("RG Account Audit", actor=frappe.session.user, action=action,
                   target_user=target, registration=application, detail=detail[:1000], occurred_at=now_datetime())


def _throttle(kind, identity="", identity_limit=5, ip_limit=30, site_limit=500):
    """Redis原子计数，事务回滚不复原滥用计数；键不含邮箱或IP原文。"""
    request = getattr(frappe.local, "request", None)
    ip = getattr(frappe.local, "request_ip", None) or "internal"
    bucket = int(now_datetime().timestamp()) // 3600
    keys = [(f"identity:{identity}", identity_limit), (f"ip:{ip}", ip_limit), ("site", site_limit)]
    for scope, limit in keys:
        key = "rg-account-rate:" + _digest(f"{frappe.local.site}:{kind}:{bucket}:{scope}")
        count = frappe.cache.incr(key)
        if count == 1:
            frappe.cache.expire(key, 3700)
        if count > limit:
            frappe.throw("操作过于频繁，请稍后重试。", frappe.RateLimitExceededError)


def _guest_csrf_key(value):
    return "rg-account-csrf:" + _digest(f"{frappe.local.site}:{value}")


@frappe.whitelist(allow_guest=True, methods=["GET"])
def csrf_token():
    """Guest原生会话不持久化CSRF；给公开账号表单独立的双提交随机令牌。"""
    request = getattr(frappe.local, "request", None)
    if request is None:
        return {"token": ""}
    existing = request.cookies.get("rg_account_csrf") or ""
    if not existing or not frappe.cache.get_value(_guest_csrf_key(existing)):
        _throttle("csrf", identity_limit=200, ip_limit=200, site_limit=10000)
        existing = secrets.token_urlsafe(32)
        frappe.cache.set_value(_guest_csrf_key(existing), True, expires_in_sec=3600)
    frappe.local.cookie_manager.set_cookie("rg_account_csrf", existing, httponly=True, samesite="Lax", max_age=3600)
    return {"token": existing}


def browser_write(allowed=()):
    request = getattr(frappe.local, "request", None)
    if request is None:
        return
    if request.method != "POST" or set(frappe.form_dict) - set(allowed) - {"cmd", "_", "_lang"}:
        deny()
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    actual = urlsplit(origin)
    expected = urlsplit(request.host_url)
    if not origin or (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        deny()
    if frappe.session.user != "Guest":
        from research_group.ai.api import browser_write as authenticated_write
        authenticated_write(allowed)
        return
    cookie = request.cookies.get("rg_account_csrf") or ""
    supplied = request.headers.get("X-RG-CSRF-Token") or ""
    if not cookie or not supplied or not hmac.compare_digest(cookie, supplied):
        deny()
    if not frappe.cache.get_value(_guest_csrf_key(cookie)):
        deny()


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def block_native_link_login(email=None, key=None, login_token=None):
    """Close public legacy link/sid login without reading or consuming a key.

    System Settings hides/disables generation of email links, but the fixed
    framework's legacy GET consumer does not consult that setting. Both RPC
    versions resolve these hooks before dispatch, including for Guest. Native
    functions are not patched: explicit Administrator console tools remain
    available outside the HTTP dispatcher.
    """
    deny()


def validate_group_session():
    """Do not let a late native Session.update restore a revoked cache entry.

    Frappe resumes from Redis before consulting the Sessions table. Its updater
    can cache an already-running response even when the UPDATE matched no row.
    Dedicated business services separately lock and recheck their permissions.
    """
    request = getattr(frappe.local, "request", None)
    user = frappe.session.user
    if request is None or user in {"Guest", "Administrator"} or not frappe.db.table_exists("RG Member"):
        return
    sid = frappe.session.get("sid")
    # A fresh, short read is intentional: keeping User/Member share locks in
    # this request before a business service acquires the global gate would
    # invert the established gate→identity lock order. A new read-only
    # connection also cannot inherit HTTPRequest's REPEATABLE READ snapshot.
    connection = frappe.db.get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT u.enabled,m.active,EXISTS(SELECT 1 FROM `tabSessions` s "
                           "WHERE s.sid=%s AND s.user=u.name) "
                           "FROM `tabUser` u JOIN `tabRG Member` m ON m.user=u.name WHERE u.name=%s", (sid, user))
            rows = cursor.fetchall()
    finally:
        connection.close()
    if not rows:
        return  # Unrelated Frappe accounts keep their native authentication.
    if all(rows[0]):
        return
    if sid:
        frappe.cache.hdel("session", sid)
    frappe.local.session_obj = None
    if getattr(frappe.local, "cookie_manager", None):
        from frappe.auth import clear_cookies
        clear_cookies()
    frappe.throw("会话已失效，请重新登录；停用账号请联系管理员核实。", frappe.AuthenticationError)


def validate_group_api_identity():
    """Frappe validates API credentials after before_request; recheck activation."""
    request = getattr(frappe.local, "request", None)
    user = frappe.session.user
    if (request is None or not request.headers.get("Authorization")
            or user in {"Guest", "Administrator"} or not frappe.db.table_exists("RG Member")):
        return
    connection = frappe.db.get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT u.enabled,m.active FROM `tabUser` u "
                           "JOIN `tabRG Member` m ON m.user=u.name WHERE u.name=%s", (user,))
            rows = cursor.fetchall()
    finally:
        connection.close()
    if rows and not all(rows[0]):
        frappe.throw("此课题组账号已暂停。", frappe.AuthenticationError)


def _guard(secretary=False):
    global_lock()
    if frappe.session.user == "Administrator":
        return {SECRETARY, STUDENT}
    assigned = current_roles()
    if secretary and SECRETARY not in assigned:
        deny()
    return assigned


def _user_row(user):
    rows = frappe.db.sql("SELECT name,enabled,first_name,full_name,modified,reset_password_key,"
                         "last_reset_password_key_generated_on FROM `tabUser` WHERE name=%s FOR UPDATE",
                         (user,), as_dict=True)
    return rows[0] if rows else None


def _member_row(user):
    rows = frappe.db.sql("SELECT * FROM `tabRG Member` WHERE user=%s FOR UPDATE", (user,), as_dict=True)
    return rows[0] if rows else None


def _profile(user, create=False):
    rows = frappe.db.sql("SELECT name FROM `tabRG Account Profile` WHERE user=%s FOR UPDATE", (user,))
    if rows:
        return frappe.get_doc("RG Account Profile", rows[0][0], for_update=True)
    if create:
        return _insert("RG Account Profile", user=user, email_version=0, contact_verified=0)
    return None


def _assigned_roles(user):
    return {row[0] for row in frappe.db.sql("SELECT role FROM `tabHas Role` WHERE parent=%s "
                                          "AND parenttype='User' LOCK IN SHARE MODE", (user,))}


def _roles_digest(user):
    return _digest(json.dumps(sorted(_assigned_roles(user)), ensure_ascii=False, separators=(",", ":")))


def _valid_service_pause(user, native, member, profile):
    return bool(profile and profile.service_pause_audit and not native.enabled and not member.active
                and profile.pause_user_modified and profile.pause_member_modified
                and get_datetime(profile.pause_user_modified) == get_datetime(native.modified)
                and get_datetime(profile.pause_member_modified) == get_datetime(member.modified)
                and profile.pause_roles_sha256 == _roles_digest(user)
                and frappe.db.exists("RG Account Audit", {"name": profile.service_pause_audit,
                    "target_user": user, "action": "student_disabled"}))


def _clear_pause(profile):
    profile.service_pause_audit = profile.pause_user_modified = profile.pause_member_modified = None
    profile.pause_roles_sha256 = None


def identity_status_changed(doc, method=None):
    """An administrator or another app owns its own lock; the secretary cannot undo it."""
    if (doc.flags.get("rg_account_lifecycle") is TOKEN or not frappe.db.table_exists("RG Account Profile")
            or doc.doctype not in {"User", "RG Member"}):
        return
    old = doc.get_doc_before_save()
    if method != "on_trash":
        if not old:
            return
        if doc.doctype == "User":
            if (int(old.enabled or 0), sorted(row.role for row in old.roles)) == (
                    int(doc.enabled or 0), sorted(row.role for row in doc.roles)):
                return
        elif old.active == doc.active and old.user == doc.user:
            return
    user = doc.name if doc.doctype == "User" else doc.user
    profile = _profile(user)
    if profile and profile.service_pause_audit:
        _clear_pause(profile)
        _save(profile)


def _ordinary_student(user):
    if user in {"Guest", "Administrator"} or user == frappe.session.user:
        deny()
    native = _user_row(user)
    member = _member_row(user)
    assigned = _assigned_roles(user)
    if not native or not member or STUDENT not in assigned or assigned - SAFE_STUDENT_ROLES:
        deny()
    return native, member


def contact_address(user, require_active=True):
    """内部白名单解析器；旧User不自动成为已验证新申请。"""
    if not isinstance(user, str) or len(user) > 140:
        return None
    native = frappe.db.sql("SELECT enabled FROM `tabUser` WHERE name=%s LOCK IN SHARE MODE", (user,))
    member = frappe.db.sql("SELECT active FROM `tabRG Member` WHERE user=%s LOCK IN SHARE MODE", (user,))
    if not native or not member or (require_active and (not native[0][0] or not member[0][0])):
        return None
    rows = frappe.db.sql("SELECT contact_email,contact_verified,email_version FROM `tabRG Account Profile` "
                         "WHERE user=%s LOCK IN SHARE MODE", (user,), as_dict=True)
    if not rows or not rows[0].contact_verified or not rows[0].contact_email:
        return None
    return {"user": user, "email": rows[0].contact_email, "email_version": int(rows[0].email_version)}


def _link(mode, **values):
    # Secrets stay in the browser fragment, outside GET/access-log/referrer
    # parameters. The form sends them once by POST after explicit confirmation.
    secrets_in_fragment = {name: values.pop(name) for name in ("token", "key") if name in values}
    path = "/rg_join?" + urlencode({"mode": mode, **values})
    if secrets_in_fragment:
        path += "#" + urlencode(secrets_in_fragment)
    return get_url(path, allow_header_override=False)


def _mail(key, template, **kwargs):
    from research_group.notifications import queue_template
    return queue_template(key, template, **kwargs)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_atomic
def request_registration(display_name, student_id, email):
    browser_write({"display_name", "student_id", "email"})
    name = _text(display_name, 80, "姓名", required=True)
    identity, address = _student_id(student_id), normalize_email(email)
    _throttle("registration", address)
    global_lock()
    existing_user = _user_row(address)
    rows = frappe.db.sql("SELECT name FROM `tabRG Registration` WHERE status!='expired' "
                         "AND (email=%s OR student_id=%s) FOR UPDATE",
                         (address, identity))
    occupied = frappe.db.sql("SELECT name FROM `tabRG Account Profile` WHERE student_id=%s OR contact_email=%s "
                             "OR (pending_email=%s AND pending_expires>%s) FOR UPDATE",
                             (identity, address, address, now_datetime()))
    if existing_user or occupied:
        return {"ok": True, "message": GENERIC}
    active = []
    for name_row in rows:
        candidate = frappe.get_doc("RG Registration", name_row[0], for_update=True)
        if candidate.status == "rejected":
            # Older rejected rows may still own a unique reservation. Release
            # only that reservation; the application and its review stay intact.
            if candidate.reserved_email or candidate.reserved_student_id:
                candidate.reserved_email = candidate.reserved_student_id = None
                _save(candidate)
                _audit("registration_reservation_released", application=candidate.name)
        elif (candidate.status == "pending_verification" and candidate.expires_at
                and get_datetime(candidate.expires_at) <= now_datetime()):
            candidate.status, candidate.token_hash = "expired", None
            candidate.reserved_email = candidate.reserved_student_id = None
            _save(candidate)
            _audit("registration_expired", application=candidate.name)
        else:
            active.append(candidate)
    rows = active
    if rows:
        registration = rows[0]
        if (registration.email != address or registration.student_id != identity
                or registration.status != "pending_verification"):
            return {"ok": True, "message": GENERIC}
        if registration.requested_at and now_datetime() < get_datetime(registration.requested_at) + timedelta(seconds=60):
            return {"ok": True, "message": GENERIC}
    else:
        registration = frappe.get_doc({"doctype": "RG Registration", "display_name": name,
                                       "student_id": identity, "email": address, "status": "pending_verification",
                                       "reserved_email": address, "reserved_student_id": identity})
    raw = secrets.token_urlsafe(32)
    registration.token_hash = _digest(raw)
    registration.expires_at = now_datetime() + timedelta(hours=1)
    registration.requested_at = now_datetime()
    registration.token_version = int(registration.token_version or 0) + 1
    _save(registration, new=registration.is_new())
    _mail(f"registration:{registration.name}:{registration.token_version}", "registration_verification",
          recipient_email=address, related_doctype=registration.doctype, related_name=registration.name,
          expires_at=registration.expires_at,
          values={"link": _link("verify", application=registration.name, token=raw), "display_name": name})
    _audit("registration_requested", application=registration.name)
    return {"ok": True, "message": GENERIC}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@_atomic
def verify_registration(application, token):
    browser_write({"application", "token"})
    _throttle("verify", str(application)[:140], identity_limit=20, ip_limit=60)
    global_lock()
    if not isinstance(application, str) or len(application) > 140 or not isinstance(token, str) or len(token) > 100:
        deny()
    rows = frappe.db.sql("SELECT name FROM `tabRG Registration` WHERE name=%s FOR UPDATE", (application,))
    valid = False
    if rows:
        registration = frappe.get_doc("RG Registration", application, for_update=True)
        valid = (registration.status == "pending_verification" and registration.token_hash
                 and hmac.compare_digest(registration.token_hash, _digest(token))
                 and registration.expires_at and now_datetime() < get_datetime(registration.expires_at))
        if valid:
            registration.status, registration.token_hash = "awaiting_approval", None
            registration.verified_at = now_datetime()
            _save(registration)
            _audit("registration_verified", application=application)
    if not valid:
        frappe.throw("验证链接无效、已使用或已过期，请重新申请验证。")
    return {"ok": True, "message": "邮箱验证完成；秘书审核通过后，您会收到设置网站密码的邮件。"}


def password_link_expires(row):
    """Use the same server-side lifetime as the native password consumer."""
    generated = row.get("last_reset_password_key_generated_on")
    if not generated:
        return None
    seconds = cint(frappe.get_system_settings("reset_password_link_expiry_duration"))
    # The pinned native consumer treats zero as no time limit. Missing key
    # generation is checked separately from that configured native state.
    return get_datetime(generated) + timedelta(seconds=seconds) if seconds else None


def _issue_password(user, template="password_reset"):
    address = contact_address(user)
    if not address:
        frappe.throw("此账号没有可用的已验证联系邮箱，请由管理员核实恢复。")
    native = frappe.get_doc("User", user, for_update=True)
    link = native._reset_password(send_email=False)
    key = link.split("key=", 1)[1].split("&", 1)[0]
    return _mail(f"password:{user}:{native.reset_password_key}", template, recipient_user=user,
          related_doctype="User", related_name=user, expires_at=password_link_expires(_user_row(user)),
          values={"link": _link("password", key=key), "display_name": native.full_name, "login_id": user})


def _reissue_registration(registration):
    """Internal recovery of a still-current request, with a fresh one-use key."""
    raw = secrets.token_urlsafe(32)
    registration.token_hash = _digest(raw)
    registration.token_version = int(registration.token_version or 0) + 1
    registration.expires_at = now_datetime() + timedelta(hours=1)
    registration.requested_at = now_datetime()
    _save(registration)
    return _mail(f"registration:{registration.name}:{registration.token_version}", "registration_verification",
          recipient_email=registration.email, related_doctype=registration.doctype, related_name=registration.name,
          expires_at=registration.expires_at,
          values={"link": _link("verify", application=registration.name, token=raw),
                  "display_name": registration.display_name})


@frappe.whitelist(methods=["POST"])
@_atomic
def review_registration(application, decision, note=""):
    browser_write({"application", "decision", "note"})
    _guard(secretary=True)
    if decision not in {"approve", "reject"}:
        deny()
    note = _text(note, 1000, "审核说明")
    if not isinstance(application, str) or len(application) > 140:
        deny()
    rows = frappe.db.sql("SELECT name,email FROM `tabRG Registration` WHERE name=%s FOR UPDATE", (application,), as_dict=True)
    if not rows:
        deny()
    registration = frappe.get_doc("RG Registration", application, for_update=True)
    if registration.status == "approved":
        return {"ok": True, "name": application, "status": "approved", "user": registration.created_user}
    if registration.status == "rejected" and decision == "reject":
        return {"ok": True, "name": application, "status": "rejected"}
    if registration.status != "awaiting_approval" or not registration.verified_at:
        frappe.throw("仅可审核已验证、等待审核的申请。")
    if decision == "approve":
        if _user_row(registration.email) or frappe.db.exists("RG Account Profile", {"student_id": registration.student_id}):
            frappe.throw("此邮箱或学号已有身份，不能由申请接管；请管理员核实。")
        native = frappe.get_doc({"doctype": "User", "email": registration.email,
            "first_name": registration.display_name, "enabled": 1, "user_type": "System User",
            "language": "zh", "send_welcome_email": 0, "roles": [{"role": STUDENT}]})
        native.flags.no_welcome_mail = True
        native.insert(ignore_permissions=True)
        save_internal(frappe.get_doc({"doctype": "RG Member", "user": native.name,
            "display_name": registration.display_name, "active": 1, "is_demo": 0}), new=True)
        _insert("RG Account Profile", user=native.name, student_id=registration.student_id,
                contact_email=registration.email, contact_verified=1, email_version=1)
        registration.status, registration.created_user = "approved", native.name
        _issue_password(native.name, "registration_approved")
    else:
        registration.status = "rejected"
        registration.reserved_email = registration.reserved_student_id = None
    registration.reviewed_by, registration.reviewed_at = frappe.session.user, now_datetime()
    registration.review_note = note
    registration.token_hash = None
    _save(registration)
    if decision == "reject":
        _mail(f"registration-rejected:{registration.name}", "registration_rejected",
              recipient_email=registration.email, related_doctype=registration.doctype,
              related_name=registration.name, values={"display_name": registration.display_name})
    _audit("registration_" + decision, target=registration.created_user, application=application, detail=note)
    return {"ok": True, "name": application, "status": registration.status, "user": registration.created_user}


def _profile_dict(user, native, member, profile):
    return {"login_id": user, "display_name": member.display_name,
            "student_id": profile.student_id if profile else None,
            "contact_email": profile.contact_email if profile and profile.contact_verified else None,
            "contact_verified": bool(profile and profile.contact_verified),
            "email_version": int(profile.email_version or 0) if profile else 0,
            "phone": profile.phone if profile else "", "bio": profile.bio if profile else ""}


@frappe.whitelist()
def bootstrap():
    assigned = _guard()
    user = frappe.session.user
    native, member, profile = _user_row(user), _member_row(user), _profile(user)
    if not native or not member:
        deny()
    can_manage = SECRETARY in assigned
    result = {"profile": _profile_dict(user, native, member, profile), "can_manage": can_manage,
              "registrations": [], "members": [], "more_members": False, "more_registrations": False,
              "mail_status": [], "mail_recovery": {}}
    if can_manage:
        registrations = frappe.db.sql("SELECT name,display_name,student_id,email,status,creation,verified_at "
            "FROM `tabRG Registration` WHERE status IN ('pending_verification','awaiting_approval','rejected') "
            "ORDER BY CASE status WHEN 'awaiting_approval' THEN 0 WHEN 'pending_verification' THEN 1 ELSE 2 END,"
            "creation,name LIMIT 201 LOCK IN SHARE MODE", as_dict=True)
        members = frappe.db.sql("SELECT m.user,m.display_name,m.active,u.enabled,p.student_id,p.contact_email "
            "FROM `tabRG Member` m JOIN `tabUser` u ON u.name=m.user "
            "LEFT JOIN `tabRG Account Profile` p ON p.user=m.user ORDER BY m.display_name,m.user LIMIT 201 LOCK IN SHARE MODE", as_dict=True)
        result.update({"registrations": registrations[:200], "more_registrations": len(registrations) > 200,
                       "more_members": len(members) > 200})
        for row in members[:200]:
            assigned_target = _assigned_roles(row.user)
            row.manageable = (row.user != user and row.user not in {"Guest", "Administrator"}
                              and STUDENT in assigned_target and not assigned_target - SAFE_STUDENT_ROLES)
        result["members"] = members[:200]
    from research_group.notifications import automatic_status_rows
    manageable = [row.user for row in result["members"] if row.manageable and row.user != user]
    # Include the viewer within the fixed 200-identity bound without making an
    # N+1 query for every account in the management page.
    targets = [user, *manageable[:199]]
    mail = automatic_status_rows(targets, [row.name for row in result["registrations"]], include_recovery=True)
    result["mail_status"] = mail["users"].get(user, [])
    def recovery(events):
        return {row["name"]: mail["recovery"][row["name"]] for row in events if row["name"] in mail["recovery"]}
    result["mail_recovery"] = recovery(result["mail_status"])
    for row in result["members"]:
        row.mail_status = mail["users"].get(row.user, []) if row.manageable else []
        row.mail_recovery = recovery(row.mail_status)
    for row in result["registrations"]:
        row.mail_status = mail["registrations"].get(row.name, [])
        row.mail_recovery = recovery(row.mail_status)
    return result


def _sync_name(user, display_name):
    profile = _profile(user)
    preserve_pause = _valid_service_pause(user, _user_row(user), _member_row(user), profile)
    native = frappe.get_doc("User", user, for_update=True)
    native.first_name, native.middle_name, native.last_name = display_name, None, None
    native.flags.rg_account_lifecycle = TOKEN
    native.save(ignore_permissions=True)
    member = frappe.get_doc("RG Member", user, for_update=True)
    member.display_name = display_name
    member.flags.rg_account_lifecycle = TOKEN
    save_internal(member)
    if preserve_pause:
        profile.pause_user_modified, profile.pause_member_modified = _user_row(user).modified, _member_row(user).modified
        _save(profile)


@frappe.whitelist(methods=["POST"])
@_atomic
def update_profile(display_name, phone="", bio=""):
    browser_write({"display_name", "phone", "bio"})
    _guard()
    user = frappe.session.user
    _user_row(user)
    _member_row(user)
    display_name = _text(display_name, 80, "姓名", required=True)
    phone, bio = _text(phone, 32, "电话"), _text(bio, 2000, "个人简介")
    if phone and not re.fullmatch(r"[0-9+() \-]+", phone):
        frappe.throw("电话格式不符合要求。")
    profile = _profile(user, create=True)
    profile.phone, profile.bio = phone, bio
    _save(profile)
    _sync_name(user, display_name)
    _audit("self_profile_updated", target=user)
    return {"ok": True}


def _check_password(user, password):
    if not isinstance(password, str) or not password or len(password) > 1024:
        deny()
    # LoginManager.check_password calls fail(), which commits on rejection and
    # destroys this service's savepoint. Use the same native password verifier
    # directly; the public account operations apply their own Redis throttle.
    from frappe.utils.password import check_password
    check_password(user, password)


def _email_available(email, user):
    if email != user and _user_row(email):
        return False
    return not frappe.db.sql("SELECT name FROM `tabRG Account Profile` WHERE user!=%s "
        "AND (contact_email=%s OR (pending_email=%s AND pending_expires>%s)) FOR UPDATE",
        (user, email, email, now_datetime()))


@frappe.whitelist(methods=["POST"])
@_atomic
def request_contact_change(email, password):
    browser_write({"email", "password"})
    _guard()
    user, address = frappe.session.user, normalize_email(email)
    _throttle("contact", user)
    _user_row(user)
    _member_row(user)
    _check_password(user, password)
    profile = _profile(user, create=True)
    if not _email_available(address, user):
        frappe.throw("此邮箱暂不可用于联系邮箱，请管理员核实。")
    if profile.contact_verified and profile.contact_email == address:
        return {"ok": True, "message": "此邮箱已验证。"}
    if profile.pending_requested_at and now_datetime() < get_datetime(profile.pending_requested_at) + timedelta(seconds=60):
        frappe.throw("请等待一分钟后再发送验证邮件。")
    raw = secrets.token_urlsafe(32)
    profile.pending_email, profile.pending_token_hash = address, _digest(raw)
    profile.pending_expires = now_datetime() + timedelta(hours=1)
    profile.pending_requested_at = now_datetime()
    _save(profile)
    _mail(f"contact:{user}:{profile.pending_token_hash}", "contact_verification", recipient_email=address,
          related_doctype=profile.doctype, related_name=profile.name,
          expires_at=profile.pending_expires,
          values={"link": _link("contact", token=raw), "display_name": frappe.db.get_value("RG Member", user, "display_name")})
    _audit("contact_requested", target=user)
    return {"ok": True, "message": "验证邮件已排队；新邮箱验证完成前，原联系邮箱保持有效。"}


def _revoke_tokens(user, profile=None):
    frappe.db.set_value("User", user, {"reset_password_key": "", "last_reset_password_key_generated_on": None})
    if profile:
        profile.pending_email = profile.pending_token_hash = profile.pending_expires = profile.pending_requested_at = None
        _save(profile)


def _revoke_sessions_after_commit(user, audit_name, keep_current=False):
    current_sid = frappe.session.get("sid") if keep_current and user == frappe.session.user else None

    def purge():
        # Savepoint rollback does not discard native callbacks. Bind this
        # side effect to the successful audit in the committed transaction.
        if not frappe.db.exists("RG Account Audit", {"name": audit_name, "target_user": user}):
            return
        # 不调用会逐条commit的delete_session；一次有界查询+删除，缓存同步移除。
        # native Session仍负责登录、到期及请求校验，本服务只撤销已有会话。
        for _ in range(100):
            values = [user]
            condition = "user=%s"
            if current_sid:
                condition += " AND sid!=%s"
                values.append(current_sid)
            rows = frappe.db.sql("SELECT sid FROM `tabSessions` WHERE " + condition + " LIMIT 100 FOR UPDATE", values)
            if not rows:
                break
            for sid, in rows:
                frappe.db.delete("Sessions", {"sid": sid})
                frappe.cache.hdel("session", sid)
            frappe.db.commit()
        else:
            frappe.throw("账号会话数量异常，管理员需完成剩余会话撤销。")
        if not keep_current and user == frappe.session.user and getattr(frappe.local, "request", None):
            # sync_database queues Session.update after after_commit. Prevent
            # this very response from restoring the just-revoked cache entry.
            frappe.local.session_obj = None
            if getattr(frappe.local, "cookie_manager", None):
                from frappe.auth import clear_cookies
                clear_cookies()

    frappe.db.after_commit.add(purge)


@frappe.whitelist(methods=["POST"])
@_atomic
def verify_contact_change(token):
    browser_write({"token"})
    _guard()
    user = frappe.session.user
    native, member = _user_row(user), _member_row(user)
    profile = _profile(user)
    if (not profile or not isinstance(token, str) or len(token) > 100 or not profile.pending_token_hash
            or not hmac.compare_digest(profile.pending_token_hash, _digest(token))
            or not profile.pending_expires or now_datetime() >= get_datetime(profile.pending_expires)):
        frappe.throw("验证链接无效、已使用或已过期。")
    if not _email_available(profile.pending_email, user):
        frappe.throw("此邮箱暂不可用，请管理员核实。")
    old = profile.contact_email if profile.contact_verified else None
    profile.contact_email, profile.contact_verified = profile.pending_email, 1
    profile.email_version = int(profile.email_version or 0) + 1
    _revoke_tokens(user, profile)
    invalidate(user)
    if old and old != profile.contact_email:
        _mail(f"contact-changed:{user}:{profile.email_version}", "contact_changed", recipient_email=old,
              related_doctype=profile.doctype, related_name=profile.name,
              values={"display_name": member.display_name, "login_id": user, "contact_email": profile.contact_email})
    audit = _audit("contact_verified", target=user)
    _revoke_sessions_after_commit(user, audit.name, keep_current=True)
    return {"ok": True, "contact_email": profile.contact_email, "login_id": user, "email_version": profile.email_version}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request_password_reset(user):
    # The hooks replace a native endpoint used by non-group Frappe users too.
    # Keep its original UI, CSRF, identity and mail lifecycle; the outer site
    # response remains uniform for known and unknown recovery requests.
    # Native routines may commit internally, so never call them from the
    # group-only savepoint wrapper.
    global_lock()
    if isinstance(user, str) and not _group_password_identity(user.strip().lower()):
        from frappe.core.doctype.user.user import reset_password as native_reset_password
        native_reset_password(user)
        # The native function uses msgprint, including for unknown accounts.
        # Preserve mail behavior while keeping the outer response shape equal
        # to group recovery, without an identity-specific message channel.
        frappe.clear_messages()
        return {"ok": True, "message": GENERIC}
    return _request_group_password_reset(user)


def _group_password_identity(user):
    if not isinstance(user, str) or len(user) > 140:
        return False
    profiles = frappe.db.sql("SELECT user FROM `tabRG Account Profile` WHERE user=%s "
        "OR (contact_email=%s AND contact_verified=1) LOCK IN SHARE MODE", (user, user))
    members = frappe.db.sql("SELECT user FROM `tabRG Member` WHERE user=%s LOCK IN SHARE MODE", (user,))
    return bool(profiles or members)


@_atomic
def _request_group_password_reset(user):
    browser_write({"user"})
    if not isinstance(user, str) or len(user) > 140:
        return {"ok": True, "message": GENERIC}
    address = user.strip().lower()
    _throttle("reset", address, identity_limit=5, ip_limit=40)
    global_lock()
    rows = frappe.db.sql("SELECT user FROM `tabRG Account Profile` WHERE user=%s "
                         "OR (contact_email=%s AND contact_verified=1) FOR UPDATE", (address, address))
    if len(rows) == 1:
        identity = rows[0][0]
        native, member = _user_row(identity), _member_row(identity)
        if native and member and native.enabled and member.active and contact_address(identity):
            # 同一小时内生成次数有界，旧key由原生_reset_password立即失效。
            _issue_password(identity)
            _audit("password_reset_requested", target=identity)
    return {"ok": True, "message": GENERIC}


def _strong_password(password, user):
    from frappe.utils.password_strength import test_password_strength
    if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
        frappe.throw("网站密码须为12至128个字符，请使用难以猜测的密码。")
    data = frappe.db.get_value("User", user, ["first_name", "full_name", "email"]) or ()
    strength = test_password_strength(password, user_inputs=data)
    if int(strength.get("score") or 0) < 4:
        frappe.throw("网站密码强度不足，请使用更长且难以猜测的密码。")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def update_password(new_password, logout_all_sessions=0, key=None, old_password=None):
    global_lock()
    identity = frappe.session.user
    if key and isinstance(key, str) and len(key) <= 100:
        rows = frappe.db.sql("SELECT name FROM `tabUser` WHERE reset_password_key=%s LOCK IN SHARE MODE",
                             (_digest(key),))
        identity = rows[0][0] if len(rows) == 1 else None
    if identity and not _group_password_identity(identity):
        from frappe.core.doctype.user.user import update_password as native_update_password
        return native_update_password(new_password, logout_all_sessions=logout_all_sessions,
                                      key=key, old_password=old_password)
    return _update_group_password(new_password, logout_all_sessions, key, old_password)


@_atomic
def _update_group_password(new_password, logout_all_sessions=1, key=None, old_password=None):
    """保留原生摘要/期限/哈希算法；锁内消费key，提交后撤销会话并要求重新登录。"""
    browser_write({"new_password", "logout_all_sessions", "key", "old_password"})
    global_lock()
    if key:
        if not isinstance(key, str) or len(key) > 100:
            deny()
        _throttle("password_key", _digest(key), identity_limit=12, ip_limit=40)
        rows = frappe.db.sql("SELECT name FROM `tabUser` WHERE reset_password_key=%s FOR UPDATE", (_digest(key),))
        if len(rows) != 1:
            frappe.throw("设置密码链接无效、已使用或已过期。")
        user = rows[0][0]
        row = _user_row(user)
        expires = password_link_expires(row)
        if (not row.enabled or not row.last_reset_password_key_generated_on
                or (expires and now_datetime() >= expires)):
            frappe.throw("设置密码链接无效、已使用或已过期。")
    else:
        _guard()
        user = frappe.session.user
        row = _user_row(user)
        _throttle("password_change", user, identity_limit=10, ip_limit=40)
        _check_password(user, old_password)
    member = _member_row(user)
    if not member or not member.active or not row.enabled:
        deny()
    _strong_password(new_password, user)
    from frappe.utils.password import update_password as native_hash_password
    native_hash_password(user, new_password, logout_all_sessions=False)
    frappe.db.set_value("User", user, {"reset_password_key": "", "last_reset_password_key_generated_on": None,
                                        "last_password_reset_date": today()})
    invalidate(user)
    audit = _audit("password_changed", target=user)
    _revoke_sessions_after_commit(user, audit.name)
    return "/login"


@frappe.whitelist(methods=["POST"])
@_atomic
def change_password(old_password, new_password):
    browser_write({"old_password", "new_password"})
    return update_password(new_password, old_password=old_password)


@frappe.whitelist(methods=["POST"])
@_atomic
def update_student(user, display_name, student_id=""):
    browser_write({"user", "display_name", "student_id"})
    _guard(secretary=True)
    _ordinary_student(user)
    name, identity = _text(display_name, 80, "姓名", required=True), _student_id(student_id, optional=True)
    profile = _profile(user, create=True)
    # 不允许清空已有学号或将其移到另一个身份。
    if profile.student_id and identity and profile.student_id != identity:
        frappe.throw("已核实学号不可由秘书改写，请管理员核实。")
    if identity and frappe.db.sql("SELECT name FROM `tabRG Account Profile` WHERE student_id=%s AND user!=%s FOR UPDATE", (identity, user)):
        frappe.throw("该学号已有身份，请管理员核实。")
    profile.student_id = profile.student_id or identity
    _save(profile)
    _sync_name(user, name)
    _audit("student_profile_updated", target=user)
    return {"ok": True}


@frappe.whitelist(methods=["POST"])
@_atomic
def set_student_status(user, enabled):
    browser_write({"user", "enabled"})
    _guard(secretary=True)
    if enabled not in (0, 1, "0", "1"):
        deny()
    enabled = int(enabled)
    native, member = _ordinary_student(user)
    profile = _profile(user)
    if enabled and (not profile or not profile.contact_verified):
        frappe.throw("账号缺少已验证联系邮箱，恢复须由管理员核实。")
    if int(native.enabled) == enabled and int(member.active) == enabled:
        if not enabled and not _valid_service_pause(user, native, member, profile):
            frappe.throw("此账号由管理员或其他流程暂停，须由管理员核实恢复。")
        return {"ok": True, "enabled": enabled}
    if enabled:
        if not _valid_service_pause(user, native, member, profile):
            frappe.throw("只能恢复秘书在本站暂停、且未被管理员另行变更的普通学生。")
    elif not native.enabled or not member.active:
        frappe.throw("此账号已有其他暂停状态，不能由秘书接管。")
    user_doc = frappe.get_doc("User", user, for_update=True)
    user_doc.enabled = enabled
    user_doc.flags.rg_account_lifecycle = TOKEN
    user_doc.save(ignore_permissions=True)
    member_doc = frappe.get_doc("RG Member", member.name, for_update=True)
    member_doc.active = enabled
    member_doc.flags.rg_account_lifecycle = TOKEN
    save_internal(member_doc)
    _revoke_tokens(user, profile)
    invalidate(user)
    address = contact_address(user, require_active=False)
    if address:
        _mail(f"account-status:{user}:{int(enabled)}:{frappe.generate_hash(length=16)}", "account_status",
              recipient_user=user, related_doctype="User", related_name=user,
              values={"display_name": member.display_name, "status": "已恢复" if enabled else "已暂停",
                      "login_id": user})
    audit = _audit("student_enabled" if enabled else "student_disabled", target=user)
    if profile:
        if enabled:
            _clear_pause(profile)
        else:
            profile.service_pause_audit = audit.name
            profile.pause_user_modified = _user_row(user).modified
            profile.pause_member_modified = _member_row(user).modified
            profile.pause_roles_sha256 = _roles_digest(user)
        _save(profile)
    if not enabled:
        _revoke_sessions_after_commit(user, audit.name)
    return {"ok": True, "enabled": enabled}


@frappe.whitelist(methods=["POST"])
@_atomic
def resend_set_password(user):
    browser_write({"user"})
    _guard(secretary=True)
    native, member = _ordinary_student(user)
    if not native.enabled or not member.active:
        deny()
    _throttle("secretary_reset", user)
    _issue_password(user)
    _audit("set_password_resent", target=user)
    return {"ok": True, "message": "设置密码邮件已进入投递队列，请查看真实投递状态。"}


class ResearchGroupAccountUser(ResearchGroupUser):
    """保留既有AI闸门和全部原生User生命周期，仅限定组内邮箱/会话边界。"""
    def validate(self):
        if (self.get("new_password") and frappe.session.user not in {"Administrator", "Guest"}
                and "System Manager" not in frappe.get_roles()
                and frappe.db.table_exists("RG Member")
                and frappe.db.exists("RG Member", {"user": self.name})):
            frappe.throw("请通过账号与安全页面验证当前密码后修改网站密码。", frappe.PermissionError)
        return super().validate()

    def before_rename(self, old_name, new_name, merge=False):
        if frappe.db.table_exists("RG Member"):
            from research_group.ai.security import native_identity_lock
            native_identity_lock(self)
            if frappe.db.sql("SELECT name FROM `tabRG Member` WHERE user=%s LOCK IN SHARE MODE", (old_name,)):
                frappe.throw("课题组登录账号保持不变，请通过个人资料验证联系邮箱。")
        return super().before_rename(old_name, new_name, merge=merge)

    def check_enable_disable(self):
        if self.flags.get("rg_account_lifecycle") is not TOKEN:
            return super().check_enable_disable()
        # 原生logout内部commit会拆开账号/成员/审计事务；仅本服务令牌可延后它。
        manager = getattr(frappe.local, "login_manager", None)
        try:
            frappe.local.login_manager = None
            return super().check_enable_disable()
        finally:
            frappe.local.login_manager = manager

    def password_reset_mail(self, link):
        if frappe.db.table_exists("RG Account Profile") and frappe.db.exists("RG Member", {"user": self.name}):
            address = contact_address(self.name)
            if not address:
                return
            key = link.split("key=", 1)[1].split("&", 1)[0]
            return _mail(f"password:{self.name}:{self.reset_password_key}", "password_reset",
                         recipient_user=self.name, related_doctype="User", related_name=self.name,
                         expires_at=password_link_expires(_user_row(self.name)),
                         values={"link": _link("password", key=key), "display_name": self.full_name, "login_id": self.name})
        return super().password_reset_mail(link)
