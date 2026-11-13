"""私人记忆/计划生命周期；只有确认事务能激活或修改内容。"""
import frappe
from frappe.utils import add_days, get_datetime, now_datetime

from research_group.ai.security import PORTAL_ROLES, guard, invalidate
from research_group.ai.sources import links, validate
from research_group.ai.storage import clear_target, dumps, journal_targets, loads, save
from research_group.policy import deny


def own_item(doctype, name, user, portal=None):
    if doctype not in {"RG AI Memory", "RG AI Plan"} or not isinstance(name, str) or len(name) > 140:
        deny()
    try:
        doc = frappe.get_doc(doctype, name, for_update=True)
    except frappe.DoesNotExistError:
        deny()
    if doc.private_user != user or doc.site != frappe.local.site or doc.status == "deleted":
        deny()
    if portal is not None and doc.portal_role != portal:
        deny()
    if doc.portal_role not in PORTAL_ROLES:
        deny()
    return doc


def item_dict(doc):
    refs = loads(doc.sources)
    readable = validate(refs, doc.private_user, portal_role=doc.portal_role)
    expired = bool(doc.expires_at and get_datetime(doc.expires_at) <= now_datetime())
    status = "expired" if expired else doc.status
    return {"name": doc.name, "kind": doc.get("kind"), "topic": doc.get("topic"),
            "content": doc.content if readable else "[来源已失权、删除或更新，内容已屏蔽]",
            "status": status, "stale": not readable or expired, "source_valid": readable,
            "expired": expired, "version": doc.context_version,
            "expires_at": str(doc.expires_at or ""), "updated_at": str(doc.modified),
            "sources": links(refs, portal_role=doc.portal_role) if readable else [],
            "conversation": doc.conversation}


def list_items(doctype, user, portal, offset=0, limit=20):
    if doctype not in {"RG AI Memory", "RG AI Plan"}:
        deny()
    rows = frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE private_user=%s AND portal_role=%s AND status<>'deleted' "
                         "ORDER BY modified DESC LIMIT %s OFFSET %s FOR UPDATE", (user, portal, limit, offset), as_dict=True)
    return [item_dict(own_item(doctype, row.name, user, portal)) for row in rows]


def set_enabled(enabled, portal):
    if type(enabled) is not bool:
        frappe.throw("记忆开关必须为布尔值。")
    state = guard(portal=portal)
    if bool(state["state"].memory_enabled) != enabled:
        frappe.db.set_value("RG AI State", state["state"].name, "memory_enabled", int(enabled), update_modified=False)
        invalidate(frappe.session.user, portal=state["portal"])
    return {"memory_enabled": enabled}


def delete_conversation(name, keep_memory=None):
    state = guard(conversation=name)
    user = frappe.session.user
    keep_memory = keep_memory or []
    if (not isinstance(keep_memory, list) or len(keep_memory) > 200
            or any(not isinstance(item, str) for item in keep_memory)):
        deny()
    memories = frappe.db.sql(
        "SELECT name FROM `tabRG AI Memory` WHERE private_user=%s AND portal_role=%s AND conversation=%s "
        "AND status<>'deleted' FOR UPDATE", (user, state["portal"], name), as_dict=True,
    )
    known = {row.name for row in memories}
    if set(keep_memory) - known:
        deny()
    targets = [("RG AI Conversation", name)]
    for row in memories:
        if row.name in keep_memory:
            # 仅断开源会话；全部正式/派生来源仍保留，不成为无来源的新记忆。
            frappe.db.set_value("RG AI Memory", row.name, "conversation", "", update_modified=False)
        else:
            targets.append(("RG AI Memory", row.name))
    journal_targets(user, targets)
    for doctype, identifier in targets:
        clear_target(doctype, identifier, user)
    invalidate(user, portal=state["portal"])
    return {"deleted": name, "deleted_memory": len(memories) - len(keep_memory), "kept_memory": keep_memory}


def activate_memory(doc, values):
    doc.kind, doc.content = values["kind"], values["content"]
    doc.topic = values.get("topic") or ""
    doc.status = "active"
    doc.expires_at = None if doc.kind == "preference" else add_days(now_datetime(), 30)
    doc.context_version = int(doc.context_version or 0) + 1
    save(doc)


def update_plan(doc, values):
    doc.topic, doc.content = values.get("topic") or "", values["content"]
    doc.status = values.get("status", "active")
    doc.expires_at = add_days(now_datetime(), 30)
    doc.context_version = int(doc.context_version or 0) + 1
    save(doc)
