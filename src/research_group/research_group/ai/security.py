"""短事务闸门：全站授权→身份→本人隐私→会话→来源→操作。

网络等待前提交；原生User/Member撤权也先锁同一闸门。禁止缓存身份查询。
"""
import hashlib

import frappe
from frappe.utils import now_datetime

from research_group.ai.storage import insert
from research_group.policy import ROLES, deny

GLOBAL = "rg-ai-site"


def install_state():
    if not frappe.db.exists("RG AI State", GLOBAL):
        insert("RG AI State", "", name=GLOBAL, context_version=1, auth_version=1, memory_enabled=0)


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


def user_state(user):
    name = hashlib.sha256(("user:" + user).encode()).hexdigest()[:32]
    rows = frappe.db.sql("SELECT * FROM `tabRG AI State` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
    if not rows:
        return insert("RG AI State", user, name=name, context_version=1, memory_enabled=1,
                      auth_version=0).as_dict()
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


def conversation_row(name, user):
    if not isinstance(name, str) or len(name) > 140:
        deny()
    rows = frappe.db.sql("SELECT * FROM `tabRG AI Conversation` WHERE name=%s FOR UPDATE", (name,), as_dict=True)
    if not rows or rows[0].private_user != user or rows[0].site != frappe.local.site or rows[0].status != "active":
        deny()
    return rows[0]


def guard(user=None, conversation=None, expected=None, sources=None):
    user = user or frappe.session.user
    if user != frappe.session.user:
        deny()
    site = global_lock()
    assigned = current_roles(user)
    state = user_state(user)
    conv = conversation_row(conversation, user) if conversation else None
    versions = {"site": frappe.local.site, "user": user, "privacy": int(state.context_version),
                "authorization": int(site.auth_version),
                "conversation": int(conv.context_version) if conv else 0}
    if expected is not None and versions != expected:
        frappe.throw("隐私、权限或会话已变化，本轮和旧预览已失效，请重新发起。", frappe.PermissionError)
    if sources:
        from research_group.ai.sources import validate
        validate(sources, user, strict=True)
    return {"versions": versions, "roles": assigned, "state": state, "conversation": conv}


def invalidate(user, conversation=None):
    # 调用方先锁全站闸门；不触碰任何正式业务记录。
    state = user_state(user)
    frappe.db.sql("UPDATE `tabRG AI State` SET context_version=context_version+1 WHERE name=%s", (state.name,))
    if conversation:
        frappe.db.sql("UPDATE `tabRG AI Conversation` SET context_version=context_version+1 WHERE name=%s",
                      (conversation,))
    frappe.db.sql("UPDATE `tabRG AI Operation` SET status='cancelled', parameters='', preview='', sources='[]' "
                  "WHERE private_user=%s AND status='pending'", (user,))
    # Logical invalidation never releases the worker's execution locks.
    frappe.db.sql("UPDATE `tabRG AI Run` SET status='cancelled', error_class='context_changed', finished_at=%s "
                  "WHERE private_user=%s AND status IN ('queued','running')", (now_datetime(), user))


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
