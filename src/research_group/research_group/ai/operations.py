"""预览→本人确认→单事务消费；执行函数名和身份不来自模型或客户端。"""
import hashlib
from datetime import timedelta

import frappe
from frappe.utils import add_days, getdate, get_datetime, now_datetime, today

from research_group.ai.memory import activate_memory, own_item, update_plan
from research_group.ai.security import guard, invalidate
from research_group.ai.sources import capture, links, read_current, reject_cycle, union, validate
from research_group.ai.storage import clear_target, dumps, insert, journal_targets, loads, require_capacity, save
from research_group.policy import PENDING, STUDENT, TEACHER, deny

ACTIONS = {"memory_change", "plan_change", "progress", "appointment", "manuscript_suggestion"}
OPERATION_PROTOCOL = "rg-site-operation-2"


def text(value, label, maximum=5000, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        frappe.throw(f"{label}须为有效文本，最多{maximum}字。")
    if len(value.encode("utf-8")) > 60000 or "\x00" in value:
        frappe.throw(f"{label}超出存储范围。")
    return value.strip()


def exact(params, allowed, required):
    if not isinstance(params, dict) or set(params) - set(allowed) or set(required) - set(params):
        frappe.throw("工具参数结构无效，存在缺失或未允许字段。")


def normalize(action, params, user):
    if action not in ACTIONS:
        deny()
    if action == "manuscript_suggestion":
        exact(params, {"selection", "suggestion", "reason"}, {"selection", "suggestion"})
        return {"selection": text(params["selection"], "论文选区ID", 140),
                "suggestion": text(params["suggestion"], "AI修改建议", 10000),
                "reason": text(params.get("reason"), "AI建议理由", 4000, required=False)}
    if action == "memory_change":
        exact(params, {"change", "name", "kind", "content", "topic"}, {"change"})
        change = params["change"]
        if change not in {"add", "edit", "delete"}:
            deny()
        value = {"change": change}
        if change != "add":
            value["name"] = text(params.get("name"), "记忆ID", 140)
            own_item("RG AI Memory", value["name"], user)
        elif params.get("name"):
            deny()
        if change != "delete":
            if params.get("kind") not in {"preference", "background", "follow_up"}:
                frappe.throw("请选择偏好、本人背景或待跟进事项。")
            value.update(kind=params["kind"], content=text(params.get("content"), "记忆", 1000),
                         topic=text(params.get("topic"), "课题ID", 140, required=False))
        return value
    if action == "plan_change":
        exact(params, {"change", "name", "topic", "content", "status"}, {"change"})
        change = params["change"]
        if change not in {"add", "edit", "delete"}:
            deny()
        value = {"change": change}
        if change != "add":
            value["name"] = text(params.get("name"), "计划ID", 140)
            own_item("RG AI Plan", value["name"], user)
        elif params.get("name"):
            deny()
        if change != "delete":
            status = params.get("status") or "active"
            if status not in {"active", "completed"}:
                deny()
            value.update(content=text(params.get("content"), "计划", 12000), status=status,
                         topic=text(params.get("topic"), "课题ID", 140, required=False))
        return value
    if action == "progress":
        exact(params, {"topic", "progress_date", "summary", "blockers", "next_milestone"},
              {"topic", "progress_date", "summary", "next_milestone"})
        date = text(params["progress_date"], "进展日期", 10)
        if str(getdate(date)) != date or getdate(date) > getdate(today()):
            frappe.throw("进展日期应为有效的已发生日期。")
        return {"topic": text(params["topic"], "课题ID", 140), "progress_date": date,
                "summary": text(params["summary"], "进展"),
                "blockers": text(params.get("blockers"), "阻碍", required=False),
                "next_milestone": text(params["next_milestone"], "下一步")}
    exact(params, {"topic", "teacher", "start_at", "end_at", "agenda"},
          {"topic", "teacher", "start_at", "end_at", "agenda"})
    start = get_datetime(text(params["start_at"], "开始时间", 30))
    end = get_datetime(text(params["end_at"], "结束时间", 30))
    if start <= now_datetime() or end <= start:
        frappe.throw("预约须在未来，且结束时间晚于开始时间（Asia/Shanghai）。")
    return {"topic": text(params["topic"], "课题ID", 140), "teacher": text(params["teacher"], "教师", 140),
            "start_at": str(start), "end_at": str(end), "agenda": text(params["agenda"], "议题")}


def authorize(action, params, user, assigned):
    refs = []
    if action == "manuscript_suggestion":
        refs = capture(read_current("RG Manuscript Selection", params["selection"], user))
    elif action in {"progress", "appointment"}:
        if STUDENT not in assigned:
            deny()
        topic = read_current("RG Topic", params["topic"], user)
        if topic.student != user:
            deny()
        refs = capture(topic)
    elif params.get("topic"):
        refs = capture(read_current("RG Topic", params["topic"], user))
    if action == "appointment":
        # 与原预约协议一致，先锁教师User，再进入预约写入。
        from research_group.ai.security import current_roles
        frappe.db.sql("SELECT name FROM `tabUser` WHERE name=%s FOR UPDATE", (params["teacher"],))
        if TEACHER not in current_roles(params["teacher"]):
            deny()
    return refs


def propose(ctx, action, parameters):
    state = guard(ctx.user, ctx.conversation, ctx.versions, ctx.sources)
    params = normalize(action, parameters, ctx.user)
    refs = union(ctx.sources, authorize(action, params, ctx.user, state["roles"]))
    target = None
    target_version = None
    if action in {"memory_change", "plan_change"} and params["change"] != "add":
        dt = "RG AI Memory" if action == "memory_change" else "RG AI Plan"
        target = own_item(dt, params["name"], ctx.user)
        target_version = int(target.context_version)
        # 删除不回显已失权内容；编辑保留全部原依赖，不能换成无来源的个人文本。
        if params["change"] == "delete":
            refs = []
        else:
            refs = union(refs, loads(target.sources))
            refs = [ref for ref in refs if (ref["doctype"], ref["name"]) != (dt, target.name)]
            reject_cycle(dt, target.name, refs, ctx.user)
    validate(refs, ctx.user, strict=True)
    content = params.get("content", "")
    deleting = action in {"memory_change", "plan_change"} and params["change"] == "delete"
    if deleting:
        # Cleanup stays available at capacity without accumulating duplicate
        # preview bodies. Reuse never extends the original confirmation expiry.
        reusable = None
        rows = frappe.db.sql("SELECT name FROM `tabRG AI Operation` WHERE private_user=%s AND action=%s "
                             "AND target=%s AND status='pending' AND COALESCE(run_id,'')=%s FOR UPDATE",
                             (ctx.user, action, target.name, ctx.run or ""), as_dict=True)
        for row in rows:
            previous = own_operation(row.name, ctx.user)
            if loads(previous.parameters, {}).get("change") != "delete":
                continue
            expected = loads(previous.expected, {})
            expired = get_datetime(previous.expires_at) <= now_datetime()
            same_privacy = (expected.get("protocol") == OPERATION_PROTOCOL
                            and all(expected.get("versions", {}).get(key) == ctx.versions[key]
                                    for key in ("site", "user", "privacy", "authorization")))
            if not reusable and not expired and same_privacy and expected.get("target_version") == target_version:
                reusable = previous
                continue
            previous.status = "expired" if expired else "cancelled"
            previous.parameters = previous.preview = ""
            previous.sources = ""
            save(previous)
        if reusable:
            expected = loads(reusable.expected, {})
            if reusable.conversation != ctx.conversation or expected["versions"] != ctx.versions:
                reusable.conversation = ctx.conversation
                expected["versions"] = ctx.versions
                reusable.expected = dumps(expected)
                save(reusable)
            ctx.operations.append(reusable.name)
            return operation_dict(reusable, internal=True)
    if not deleting:
        require_capacity(ctx.user, len(dumps(params).encode("utf-8")) * 3 + len(dumps(refs).encode("utf-8")) * 3 + 4096)
    if action == "memory_change" and params["change"] == "add":
        target = insert("RG AI Memory", ctx.user, conversation=ctx.conversation, topic=params.get("topic") or "",
                        kind=params["kind"], content=content, status="candidate", context_version=1,
                        sources=dumps(refs), expires_at=add_days(now_datetime(), 7))
        target_version = 1
    labels = {"memory_change": "保存/更改本人记忆", "plan_change": "保存/更改本人计划",
              "progress": "新增正式进展", "appointment": "提交预约申请（待教师审批）",
              "manuscript_suggestion": "保存本人选区的AI修改建议（原稿保留）"}
    preview = {"label": labels[action], "parameters": params, "scope": "仅本人私有" if action.endswith("change")
               else "进入正式业务记录，沿用原有读取权限", "timezone": "Asia/Shanghai",
               "notice": "确认计划含义与个人状态；不等同科研成功或正式进展。" if action == "plan_change"
               else ("请确认进展内容真实，再提交。" if action == "progress" else "仅本次操作有效。")}
    if action == "manuscript_suggestion":
        selection = read_current("RG Manuscript Selection", params["selection"], ctx.user)
        preview.update(original_text=selection.original_text, manuscript_version=selection.manuscript_version,
                       source_sha256=selection.sha256, scope="仅论文参与学生和指定教师",
                       notice="这是AI辅助建议；确认后单独保存，保留原文和来源，不覆盖原稿或冒充教师意见。")
    expected = {"protocol": OPERATION_PROTOCOL, "versions": ctx.versions,
                "parameter_hash": hashlib.sha256(dumps(params).encode()).hexdigest(),
                "target_version": target_version}
    operation = insert("RG AI Operation", ctx.user, conversation=ctx.conversation, action=action,
                       parameters=dumps(params), preview=dumps(preview), expected=dumps(expected),
                       sources=dumps(refs), target=target.name if target else "", run_id=ctx.run or "", status="pending",
                       expires_at=now_datetime() + timedelta(minutes=15))
    ctx.operations.append(operation.name)
    return operation_dict(operation, internal=True)


def operation_dict(doc, internal=False):
    expired = doc.status == "pending" and get_datetime(doc.expires_at) <= now_datetime()
    state = "expired" if expired else doc.status
    refs = loads(doc.sources)
    readable = (state == "pending" and loads(doc.expected, {}).get("protocol") == OPERATION_PROTOCOL
                and validate(refs, doc.private_user))
    if doc.run_id and not internal:
        run_state = frappe.db.sql("SELECT status FROM `tabRG AI Run` WHERE name=%s FOR UPDATE", (doc.run_id,))
        if not run_state or run_state[0][0] != "completed":
            readable = False
    return {"name": doc.name, "action": doc.action, "status": state,
            "expires_at": str(doc.expires_at), "preview": loads(doc.preview, {}) if readable else {},
            "sources": links(refs) if readable else [], "result": loads(doc.result, {}) if state == "consumed" else {},
            "stale": state == "pending" and not readable}


def own_operation(name, user):
    if not isinstance(name, str) or len(name) > 140:
        deny()
    try:
        operation = frappe.get_doc("RG AI Operation", name, for_update=True)
    except frappe.DoesNotExistError:
        deny()
    if operation.private_user != user or operation.site != frappe.local.site:
        deny()
    return operation


def decide(operation_id, decision):
    if decision not in {"confirm", "cancel"}:
        deny()
    user = frappe.session.user
    guard(user)
    operation = own_operation(operation_id, user)
    if operation.status == "consumed":
        return {"name": operation.name, "status": "consumed", "result": loads(operation.result, {})}
    if operation.status != "pending":
        return {"name": operation.name, "status": operation.status}
    if decision == "cancel" or get_datetime(operation.expires_at) <= now_datetime():
        operation.status = "cancelled" if decision == "cancel" else "expired"
        operation.parameters = operation.preview = ""
        operation.sources = "[]"
        save(operation)
        return {"name": operation.name, "status": operation.status}
    expected = loads(operation.expected, {})
    if expected.get("protocol") != OPERATION_PROTOCOL:
        frappe.throw("操作协议已更新，旧预览已失效，请重新生成。", frappe.PermissionError)
    if operation.run_id:
        run_state = frappe.db.sql("SELECT status FROM `tabRG AI Run` WHERE name=%s FOR UPDATE", (operation.run_id,))
        if not run_state or run_state[0][0] != "completed":
            frappe.throw("本轮尚未成功完成或已经终止，不能确认旧提案。", frappe.PermissionError)
    refs = loads(operation.sources)
    state = guard(user, operation.conversation, expected["versions"], refs)
    params = loads(operation.parameters, {})
    if hashlib.sha256(dumps(params).encode()).hexdigest() != expected.get("parameter_hash"):
        deny()
    params = normalize(operation.action, params, user)
    authorize(operation.action, params, user, state["roles"])
    target = None
    if operation.target:
        doctype = "RG AI Memory" if operation.action == "memory_change" else "RG AI Plan"
        target = own_item(doctype, operation.target, user)
        if int(target.context_version) != expected["target_version"]:
            frappe.throw("目标已变化，请重新生成预览。", frappe.PermissionError)
        if params.get("change") != "delete":
            reject_cycle(doctype, target.name, refs, user)
    # 保存点保证内部调用者捕获错误时，也不会留下一半业务写入。
    marker = "ai_confirm_" + frappe.generate_hash(length=8)
    frappe.db.savepoint(marker)
    try:
        result = apply(operation, params, target, refs)
        operation.status, operation.result = "consumed", dumps(result)
        operation.parameters = operation.preview = ""
        operation.sources = "[]"
        save(operation)
        if operation.action in {"memory_change", "plan_change"}:
            invalidate(user)
    except Exception:
        frappe.db.rollback(save_point=marker)
        raise
    return {"name": operation.name, "status": "consumed", "result": result}


def apply(operation, params, target, refs):
    action, user = operation.action, operation.private_user
    deleting = action in {"memory_change", "plan_change"} and params["change"] == "delete"
    if not deleting:
        require_capacity(user, len(dumps(params).encode("utf-8")) + len(dumps(refs).encode("utf-8")))
    if action == "manuscript_suggestion":
        from research_group.manuscripts import save_ai_suggestion
        saved = save_ai_suggestion(params["selection"], params["suggestion"], params["reason"],
                                   operation_id=operation.name)
        # Consumed-operation receipts stay content-free; current manuscript
        # authorization is required to read the actual suggestion afterwards.
        return {"doctype": "RG Manuscript Review", "name": saved["name"], "status": "已保存AI建议",
                "url": "/desk/rg-manuscripts/" + saved["manuscript"]}
    if action in {"progress", "appointment"}:
        doctype = "RG Progress" if action == "progress" else "RG Appointment"
        values = dict(params)
        if action == "appointment":
            values["status"] = PENDING
        doc = frappe.get_doc({"doctype": doctype, **values}).insert()
        return {"doctype": doctype, "name": doc.name, "status": doc.get("status") or "已记录",
                "url": "/desk/" + frappe.scrub(doctype).replace("_", "-") + "/" + doc.name}
    doctype = "RG AI Memory" if action == "memory_change" else "RG AI Plan"
    if params["change"] == "delete":
        journal_targets(user, [(doctype, target.name)])
        clear_target(doctype, target.name, user)
        return {"doctype": doctype, "name": target.name, "status": "deleted"}
    if action == "memory_change":
        from research_group.ai.config import limits
        rows = frappe.db.sql("SELECT name FROM `tabRG AI Memory` WHERE private_user=%s AND status='active' "
                             "AND (expires_at IS NULL OR expires_at>NOW()) FOR UPDATE", (user,))
        if target.name not in {row[0] for row in rows} and len(rows) >= limits()["memory_count"]:
            frappe.throw("有效记忆数量已满，请先删除不再需要的记忆。")
        target.sources = dumps(refs)
        activate_memory(target, params)
    else:
        if target is None:
            target = insert("RG AI Plan", user, conversation=operation.conversation, context_version=0,
                            content="", sources="[]", status="active")
        target.sources = dumps(refs)
        update_plan(target, params)
    # A new target adds another graph node/depth beyond its validated inputs.
    # Revalidate its complete saved graph inside the confirmation savepoint.
    read_current(doctype, target.name, user)
    return {"doctype": doctype, "name": target.name, "status": target.status,
            "version": target.context_version}
