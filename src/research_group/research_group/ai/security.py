"""短事务闸门：全站授权→身份→本人隐私→会话→来源→操作。

网络等待前提交；原生User/Member撤权也先锁同一闸门。禁止缓存身份查询。
"""
import hashlib

import frappe
from frappe.utils import now_datetime

from research_group.ai.storage import insert
from research_group.policy import ROLES, SECRETARY, STUDENT, TEACHER, deny

GLOBAL = "rg-ai-site"
PORTAL_ROLES = {"teacher": TEACHER, "secretary": SECRETARY, "student": STUDENT}
LEGACY = "legacy"


def install_state():
    if not frappe.db.exists("RG AI State", GLOBAL):
        insert("RG AI State", "", name=GLOBAL, portal_role="site", context_version=1,
               auth_version=1, memory_enabled=0)


def migrate_legacy_roles():
    """Classify pre-workspace rows without rewriting their private text.

    Run after DocType synchronization. A blank role is never interpreted as a
    current workspace, even if this user's present role list has one entry.
    """
    for doctype in ("RG AI Conversation", "RG AI Message", "RG AI Memory", "RG AI Plan",
                    "RG AI Operation", "RG AI Run", "RG AI Tombstone", "RG AI State"):
        frappe.db.sql(f"UPDATE `tab{doctype}` SET portal_role=%s WHERE portal_role IS NULL OR portal_role=''",
                      (LEGACY,))
    frappe.db.sql("UPDATE `tabRG AI State` SET portal_role='site' WHERE name=%s", (GLOBAL,))
    # A pre-upgrade queued worker cannot publish into a new role workspace.
    frappe.db.sql("UPDATE `tabRG AI Run` SET status='cancelled',error_class='legacy_read_only',finished_at=%s "
                  "WHERE portal_role=%s AND status IN ('queued','running')", (now_datetime(), LEGACY))


def global_lock():
    rows = frappe.db.sql("SELECT * FROM `tabRG AI State` WHERE name=%s FOR UPDATE", (GLOBAL,), as_dict=True)
    if not rows:
        frappe.throw("AI数据库尚未迁移。")
    return rows[0]


def execution_key(user=None):
    identity = frappe.local.site + (":" + user if user else "")
    return "rg-ai-execution-" + hashlib.sha256(identity.encode()).hexdigest()[:40]


def execution_busy(user=None):
    # Connection-scoped locks survive commits and logical cancellation, but are
    # released if a worker exits or loses its connection, including hard exits.
    return frappe.db.sql("SELECT IS_USED_LOCK(%s)", (execution_key(user),))[0][0] is not None


def acquire_execution(user):
    site = execution_key()
    if frappe.db.sql("SELECT GET_LOCK(%s,0)", (site,))[0][0] != 1:
        return False
    if frappe.db.sql("SELECT GET_LOCK(%s,0)", (execution_key(user),))[0][0] != 1:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (site,))
        return False
    return True


def release_execution(user):
    frappe.db.sql("SELECT RELEASE_LOCK(%s)", (execution_key(user),))
    frappe.db.sql("SELECT RELEASE_LOCK(%s)", (execution_key(),))


def user_state(user, portal):
    if type(portal) is not str or portal not in PORTAL_ROLES:
        deny()
    name = hashlib.sha256(("user:" + user + ":" + portal).encode()).hexdigest()[:32]
    rows = frappe.db.sql("SELECT * FROM `tabRG AI State` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
    if not rows:
        legacy_name = hashlib.sha256(("user:" + user).encode()).hexdigest()[:32]
        legacy = frappe.db.sql("SELECT memory_enabled FROM `tabRG AI State` WHERE name=%s "
                               "AND private_user=%s AND portal_role=%s FOR UPDATE",
                               (legacy_name, user, LEGACY))
        return insert("RG AI State", user, name=name, portal_role=portal,
                      context_version=1, memory_enabled=int(bool(legacy[0][0])) if legacy else 1,
                      auth_version=0).as_dict()
    if rows[0].portal_role != portal:
        deny()
    return rows[0]


def _roles(user, lock_clause):
    if user in {"Guest", "Administrator"}:
        # 运维不是业务助手身份，也不替请求人执行工具。
        deny()
    enabled = frappe.db.sql("SELECT enabled FROM `tabUser` WHERE name=%s" + lock_clause, (user,))
    member = frappe.db.sql("SELECT active FROM `tabRG Member` WHERE user=%s" + lock_clause, (user,))
    assigned = frappe.db.sql(
        "SELECT role FROM `tabHas Role` WHERE parent=%s AND parenttype='User'" + lock_clause, (user,),
    )
    found = {row[0] for row in assigned}.intersection(ROLES)
    if not enabled or not enabled[0][0] or not member or not member[0][0] or not found:
        deny()
    return found


def current_roles(user=None):
    return _roles(user or frappe.session.user, " LOCK IN SHARE MODE")


def prefilter_roles(user=None):
    """Unlocked roles for rejecting a request before expensive byte work.

    Never authoritative: the snapshot may be stale. It takes no row locks, so it
    cannot invert the gate→identity lock order; callers must still pass the
    global gate and current_roles() before reading or writing protected data.
    """
    return _roles(user or frappe.session.user, "")


def conversation_row(name, user, legacy=False):
    if not isinstance(name, str) or len(name) > 140:
        deny()
    rows = frappe.db.sql("SELECT * FROM `tabRG AI Conversation` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
    if not rows or rows[0].private_user != user or rows[0].site != frappe.local.site or rows[0].status != "active":
        deny()
    if (rows[0].portal_role == LEGACY) != legacy:
        deny()
    if not legacy and rows[0].portal_role not in PORTAL_ROLES:
        deny()
    return rows[0]


def guard(user=None, conversation=None, expected=None, sources=None, portal=None):
    user = user or frappe.session.user
    if user != frappe.session.user:
        deny()
    if conversation is not None and (type(conversation) is not str or len(conversation) > 140):
        deny()
    site = global_lock()
    assigned = current_roles(user)
    selected = frappe.db.get_value("RG AI Conversation", conversation, "portal_role") if conversation else portal
    if type(selected) is not str or selected not in PORTAL_ROLES or (portal is not None and portal != selected):
        deny()
    if PORTAL_ROLES[selected] not in assigned:
        deny()
    state = user_state(user, selected)
    conv = conversation_row(conversation, user) if conversation else None
    if conv and conv.portal_role != selected:
        deny()
    versions = {"site": frappe.local.site, "user": user, "privacy": int(state.context_version),
                "authorization": int(site.auth_version),
                "conversation": int(conv.context_version) if conv else 0, "portal": selected}
    if expected is not None and versions != expected:
        frappe.throw("隐私、权限或会话已变化，本轮和旧预览已失效，请重新发起。", frappe.PermissionError)
    if sources:
        from research_group.ai.sources import validate
        validate(sources, user, strict=True, portal_role=selected)
    return {"versions": versions, "roles": {PORTAL_ROLES[selected]}, "portal": selected,
            "state": state, "conversation": conv}


def invalidate(user, conversation=None, portal=None):
    # 调用方先锁全站闸门；不触碰任何正式业务记录。
    if portal is not None and (type(portal) is not str or portal not in PORTAL_ROLES):
        deny()
    if portal:
        state = user_state(user, portal)
        frappe.db.sql("UPDATE `tabRG AI State` SET context_version=context_version+1 WHERE name=%s", (state.name,))
    else:
        frappe.db.sql("UPDATE `tabRG AI State` SET context_version=context_version+1 "
                      "WHERE private_user=%s AND name<>%s", (user, GLOBAL))
    if conversation:
        frappe.db.sql("UPDATE `tabRG AI Conversation` SET context_version=context_version+1 WHERE name=%s",
                      (conversation,))
    condition = " AND portal_role=%s" if portal else ""
    values = (user, portal) if portal else (user,)
    frappe.db.sql("UPDATE `tabRG AI Operation` SET status='cancelled', parameters='', preview='', sources='[]' "
                  "WHERE private_user=%s AND status='pending'" + condition, values)
    # Logical invalidation never releases the worker's execution locks.
    frappe.db.sql("UPDATE `tabRG AI Run` SET status='cancelled', error_class='context_changed', finished_at=%s "
                  "WHERE private_user=%s AND status IN ('queued','running')" + condition,
                  (now_datetime(), *values))


def native_identity_lock(doc=None, method=None):
    # migrate/install期间尚无表时不阻塞原生账号初始化。
    if frappe.db.table_exists("RG AI State") and frappe.db.exists("RG AI State", GLOBAL):
        global_lock()


def identity_changed(doc=None, method=None):
    if doc and method != "on_trash" and not doc.is_new():
        old = doc.get_doc_before_save()
        if old:
            if doc.doctype == "User":
                before = (old.enabled, sorted(row.role for row in old.get("roles", [])))
                after = (doc.enabled, sorted(row.role for row in doc.get("roles", [])))
                if before == after:
                    return
            elif doc.doctype == "RG Member" and old.active == doc.active and old.user == doc.user:
                return
    if frappe.db.table_exists("RG AI State") and frappe.db.exists("RG AI State", GLOBAL):
        global_lock()
        frappe.db.sql("UPDATE `tabRG AI State` SET auth_version=auth_version+1 WHERE name=%s", (GLOBAL,))


def lock_before_save(doc):
    native_identity_lock(doc)
