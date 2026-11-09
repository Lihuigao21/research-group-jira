"""同源会话入口；模型分发器不能调用确认和隐私管理接口。"""
import hmac
from urllib.parse import urlsplit

import frappe

from research_group.ai.config import public_configuration
from research_group.ai.context import RunContext
from research_group.ai.memory import delete_conversation, list_items, set_enabled
from research_group.ai.operations import decide, operation_dict, own_operation, propose, text
from research_group.ai.runner import expire_stale, schedule_next, submit
from research_group.ai.security import guard
from research_group.ai.sources import links, union, validate
from research_group.ai.storage import dumps, insert, loads, require_capacity
from research_group.policy import STUDENT, TEACHER, deny


def browser_write(allowed=()):
    request = getattr(frappe.local, "request", None)
    if request is None:
        return  # 仅Python内部/真实数据库测试；不跳过任何身份或版本检查。
    if set(frappe.form_dict) - set(allowed) - {"cmd", "_", "_lang"}:
        frappe.throw("请求含未允许的参数。", frappe.PermissionError)
    if request.method != "POST":
        deny()
    sid = request.cookies.get("sid") or ""
    expected_sid = frappe.session.get("sid") or ""
    token = request.headers.get("X-Frappe-CSRF-Token") or ""
    expected_token = (frappe.session.get("data") or {}).get("csrf_token") or ""
    if not sid or not expected_sid or not hmac.compare_digest(sid, expected_sid):
        deny()
    if not token or not expected_token or not hmac.compare_digest(token, expected_token):
        deny()
    header = request.headers.get("Origin") or request.headers.get("Referer")
    if header:
        origin = urlsplit(header)
        current = urlsplit(request.host_url)
        if (origin.scheme, origin.netloc) != (current.scheme, current.netloc):
            deny()


def pagination(offset=0, limit=20):
    try:
        start, count = int(offset), int(limit)
    except (TypeError, ValueError):
        deny()
    if start < 0 or start > 10000 or count < 1 or count > 50:
        deny()
    return start, count


def conversations(user, offset=0, limit=20):
    rows = frappe.db.sql("SELECT name,title,modified FROM `tabRG AI Conversation` WHERE private_user=%s AND status='active' "
                         "ORDER BY modified DESC,name LIMIT %s OFFSET %s FOR UPDATE", (user, limit + 1, offset), as_dict=True)
    return {"items": [dict(row) for row in rows[:limit]], "has_more": len(rows) > limit, "offset": offset}


@frappe.whitelist()
def bootstrap():
    state = guard()
    user = frappe.session.user
    topics = []
    if state["roles"] & {STUDENT, TEACHER}:
        condition = "1=1" if TEACHER in state["roles"] else "student=%s"
        values = () if condition == "1=1" else (user,)
        topics = frappe.db.sql("SELECT name,title FROM `tabRG Topic` WHERE " + condition +
                               " ORDER BY modified DESC LIMIT 201 FOR UPDATE", values, as_dict=True)
    return {"configuration": public_configuration(), "roles": sorted(state["roles"]),
            "memory_enabled": bool(state["state"].memory_enabled), "conversations": conversations(user),
            "topics": [dict(row) for row in topics[:200]], "more_topics": len(topics) > 200,
            "storage": require_capacity(user), "privacy_notice": "聊天、记忆和计划仅本人可见；运维仍有数据库维护权限。"}


@frappe.whitelist(methods=["POST"])
def create_conversation():
    browser_write()
    guard()
    require_capacity(frappe.session.user, 512)
    doc = insert("RG AI Conversation", frappe.session.user, title="新会话", status="active", context_version=1)
    return {"name": doc.name, "title": doc.title}


@frappe.whitelist()
def list_conversations(offset=0, limit=20):
    guard()
    return conversations(frappe.session.user, *pagination(offset, limit))


def message_dict(row, user):
    refs = loads(row.sources)
    readable = validate(refs, user)
    return {"name": row.name, "role": row.role, "sequence": row.sequence, "request_id": row.request_id,
            "content": row.content if readable else "[来源已失权、删除或更新，该条派生内容已屏蔽]",
            "sources": links(refs) if readable else [], "blocked": not readable, "created_at": str(row.creation)}


@frappe.whitelist()
def get_conversation(name, before=None, limit=30):
    state = guard(conversation=name)
    user = frappe.session.user
    _, count = pagination(0, limit)
    bound = int(before) if before is not None else 2147483647
    if bound < 1:
        deny()
    rows = frappe.db.sql("SELECT name,role,content,sources,sequence,request_id,creation FROM `tabRG AI Message` "
                         "WHERE conversation=%s AND private_user=%s AND sequence<%s ORDER BY sequence DESC "
                         "LIMIT %s FOR UPDATE", (name, user, bound, count + 1), as_dict=True)
    operations = frappe.db.sql("SELECT name FROM `tabRG AI Operation` WHERE conversation=%s AND private_user=%s "
                               "ORDER BY creation DESC LIMIT 20 FOR UPDATE", (name, user), as_dict=True)
    return {"name": name, "title": state["conversation"].title,
            "messages": [message_dict(row, user) for row in reversed(rows[:count])],
            "has_older": len(rows) > count, "before": rows[count - 1].sequence if len(rows) >= count else None,
            "operations": [operation_dict(own_operation(row.name, user)) for row in reversed(operations)]}


@frappe.whitelist(methods=["POST"])
def send_message(conversation, prompt, topic=None, selections=None):
    browser_write({"conversation", "prompt", "topic", "selections"})
    return submit(conversation, prompt, topic, selections=selections)


@frappe.whitelist()
def get_run(run_id):
    guard()
    user = frappe.session.user
    try:
        run = frappe.get_doc("RG AI Run", run_id, for_update=True)
    except frappe.DoesNotExistError:
        deny()
    if run.private_user != user or run.site != frappe.local.site:
        deny()
    expire_stale()
    run.reload()
    # 会话删除后不再回放，即便运行元数据仍作为额度账本保留。
    guard(conversation=run.conversation)
    result = {"run_id": run.name, "conversation": run.conversation, "status": run.status,
              "mode": run.mode, "error_class": run.error_class, "usage_known": bool(run.usage_known),
              "tokens": run.used_tokens if run.usage_known else None, "reserved_tokens": run.reserved_tokens}
    if run.status == "completed":
        rows = frappe.db.sql("SELECT name,role,content,sources,sequence,request_id,creation FROM `tabRG AI Message` "
                             "WHERE request_id=%s AND role='assistant' FOR UPDATE", (run.name,), as_dict=True)
        result["message"] = message_dict(rows[0], user) if rows else None
    return result


@frappe.whitelist(methods=["POST"])
def cancel_run(run_id):
    browser_write({"run_id"})
    guard()
    run = frappe.get_doc("RG AI Run", run_id, for_update=True)
    if run.private_user != frappe.session.user:
        deny()
    if run.status in {"queued", "running"}:
        frappe.db.set_value("RG AI Run", run.name, {"status": "cancelled", "error_class": "user_cancelled"}, update_modified=False)
        # 本轮已落盘提案仍不得在取消后继续确认。
        frappe.db.sql("UPDATE `tabRG AI Operation` SET status='cancelled',parameters='',preview='',sources='[]' "
                      "WHERE run_id=%s AND private_user=%s AND status='pending'", (run.name, run.private_user))
    return {"run_id": run.name, "status": "cancelled" if run.status in {"queued", "running"} else run.status}


@frappe.whitelist()
def private_items(kind, offset=0, limit=20):
    guard()
    if kind not in {"memory", "plan"}:
        deny()
    start, count = pagination(offset, limit)
    doctype = "RG AI Memory" if kind == "memory" else "RG AI Plan"
    return {"kind": kind, "items": list_items(doctype, frappe.session.user, start, count), "offset": start}


@frappe.whitelist(methods=["POST"])
def set_memory_enabled(enabled):
    browser_write({"enabled"})
    if isinstance(enabled, str):
        enabled = loads(enabled, None)
    return set_enabled(enabled)


@frappe.whitelist(methods=["POST"])
def propose_change(conversation, action, parameters):
    browser_write({"conversation", "action", "parameters"})
    state = guard(conversation=conversation)
    params = loads(parameters, {}) if isinstance(parameters, str) else parameters
    ctx = RunContext(frappe.session.user, conversation, "", state["versions"])
    # 手动“另存”也继承本会话可见的实际派生来源，不能借编辑卡片洗掉依赖。
    messages = frappe.db.sql("SELECT sources FROM `tabRG AI Message` WHERE conversation=%s ORDER BY sequence DESC "
                             "LIMIT 40 FOR UPDATE", (conversation,), as_dict=True)
    for message in messages:
        refs = loads(message.sources)
        if validate(refs, ctx.user):
            ctx.sources = union(ctx.sources, refs)
    return propose(ctx, action, params)


@frappe.whitelist(methods=["POST"])
def decide_operation(operation_id, decision):
    browser_write({"operation_id", "decision"})
    return decide(operation_id, decision)


@frappe.whitelist()
def get_operation(operation_id):
    guard()
    return operation_dict(own_operation(operation_id, frappe.session.user))


@frappe.whitelist()
def conversation_memory(name):
    guard(conversation=name)
    rows = frappe.db.sql("SELECT name,kind,status FROM `tabRG AI Memory` WHERE conversation=%s AND private_user=%s "
                         "AND status<>'deleted' FOR UPDATE", (name, frappe.session.user), as_dict=True)
    return [dict(row) for row in rows]


@frappe.whitelist(methods=["POST"])
def remove_conversation(name, keep_memory=None):
    browser_write({"name", "keep_memory"})
    keep = loads(keep_memory) if isinstance(keep_memory, str) else keep_memory
    return delete_conversation(name, keep)
