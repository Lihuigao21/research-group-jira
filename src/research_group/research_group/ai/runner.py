"""数据库原子预占、有界队列、单用户/单站点执行及最终发布闸门。"""
from datetime import timedelta
import time

import frappe
from frappe.utils import get_datetime, now_datetime

from research_group.ai.config import configuration_version, limits, mode
from research_group.ai.context import RunContext
from research_group.ai.graph import run_graph
from research_group.ai.operations import text
from research_group.ai.providers import get_provider
from research_group.ai.security import acquire_execution, execution_busy, global_lock, guard, release_execution
from research_group.ai.sources import read_current
from research_group.ai.storage import dumps, insert, loads, require_capacity, save

ACTIVE = {"queued", "running"}


def expire_stale():
    cap = limits()
    current = now_datetime()
    frappe.db.sql("UPDATE `tabRG AI Run` SET status='expired',error_class='queue_timeout',finished_at=%s "
                  "WHERE status='queued' AND creation<%s", (current, current - timedelta(seconds=cap["queue_seconds"])))
    frappe.db.sql("UPDATE `tabRG AI Run` SET status='failed',error_class='worker_timeout',finished_at=%s "
                  "WHERE status='running' AND started_at<%s",
                  (current, current - timedelta(seconds=cap["run_seconds"] + 15)))
    frappe.db.sql("UPDATE `tabRG AI Operation` o JOIN `tabRG AI Run` r ON r.name=o.run_id "
                  "SET o.status='cancelled',o.parameters='',o.preview='',o.sources='[]' "
                  "WHERE o.status='pending' AND r.status IN ('failed','cancelled','expired')")
    # 无法知道进程是否已发出请求，超时用量保持未知和预占，不重放。


def submit(conversation, prompt, topic=None, enqueue_job=True, selections=None):
    state = guard(conversation=conversation)
    if mode() == "disabled":
        frappe.throw("AI助手当前已关闭。")
    user = frappe.session.user
    cap = limits()
    prompt = text(prompt, "消息", cap["input_chars"])
    selections = loads(selections, []) if isinstance(selections, str) else selections
    if selections is None:
        selections = []
    if (not isinstance(selections, list) or len(selections) > 1
            or any(not isinstance(name, str) or not 1 <= len(name) <= 140 for name in selections)):
        frappe.throw("本轮只能明确选择一个本人有效论文选区。")
    for selection in selections:
        read_current("RG Manuscript Selection", selection, user)
    if topic:
        read_current("RG Topic", text(topic, "课题ID", 140))
    expire_stale()
    current = now_datetime()
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    runs = frappe.db.sql("SELECT private_user,status,creation,reserved_tokens,used_tokens,usage_known "
                         "FROM `tabRG AI Run` WHERE creation>=%s OR status IN ('queued','running') FOR UPDATE",
                         (min(day, current - timedelta(minutes=1)),), as_dict=True)
    own = [row for row in runs if row.private_user == user]
    daily = [row for row in runs if get_datetime(row.creation) >= day]
    own_daily = [row for row in daily if row.private_user == user]
    if execution_busy(user) or any(row.status in ACTIVE for row in own):
        frappe.throw("本人已有一轮排队或生成中，请等待完成或取消。")
    if len(own_daily) >= cap["daily_runs"] or sum(get_datetime(row.creation) >= current - timedelta(minutes=1) for row in own) >= cap["minute_runs"]:
        frappe.throw("本人AI轮次额度已达上限，请稍后再试。")
    waiting = sum(row.status == "queued" for row in runs)
    if waiting >= cap["queue_size"]:
        frappe.throw("AI等待队列已满，请稍后再试。")
    reserved = cap["model_calls"] * (cap["context_tokens"] + cap["output_tokens"])
    cost = lambda row: int(row.used_tokens or 0) if row.usage_known else int(row.reserved_tokens or 0)
    if sum(cost(row) for row in own_daily) + reserved > cap["user_daily_tokens"] or sum(cost(row) for row in daily) + reserved > cap["site_daily_tokens"]:
        frappe.throw("AI token预算不足；未知用量仍保留预占，请稍后或联系运维。")
    require_capacity(user, len(prompt.encode("utf-8")) + 1024)
    run = insert("RG AI Run", user, conversation=conversation, queue_topic=topic or "", selection_ids=dumps(selections),
                 expected=dumps(state["versions"]), config_version=configuration_version(), mode=mode(), status="queued",
                 reserved_tokens=reserved, usage_known=0, used_tokens=0, tool_order="[]", model_calls=0)
    sequence = frappe.db.sql("SELECT COALESCE(MAX(sequence),0)+1 FROM `tabRG AI Message` WHERE conversation=%s FOR UPDATE",
                             (conversation,))[0][0]
    insert("RG AI Message", user, conversation=conversation, sequence=sequence, role="user", content=prompt,
           sources="[]", request_id=run.name)
    if not state["conversation"].title or state["conversation"].title == "新会话":
        frappe.db.set_value("RG AI Conversation", conversation, "title", prompt[:60], update_modified=False)
    frappe.db.set_value("RG AI Conversation", conversation, "modified", current, update_modified=False)
    frappe.db.commit()
    if enqueue_job:
        try:
            enqueue(run.name)
        except Exception:
            global_lock()
            frappe.db.set_value("RG AI Run", run.name, {"status": "failed", "error_class": "queue_unavailable",
                                                       "finished_at": now_datetime()}, update_modified=False)
            frappe.db.commit()
    return {"run_id": run.name, "conversation": conversation, "status": frappe.db.get_value("RG AI Run", run.name, "status"),
            "mode": run.mode}


def enqueue(run_id):
    frappe.enqueue("research_group.ai.runner.process_run", queue="short", timeout=limits()["run_seconds"] + 15,
                   job_id="rg-ai-" + run_id, run_id=run_id)


def process_run(run_id):
    # 只接收服务端生成的ID，队列参数不含prompt、私密上下文或凭据。
    execute(run_id)


def execute(run_id, provider=None):
    global_lock()
    try:
        run = frappe.get_doc("RG AI Run", run_id, for_update=True)
    except frappe.DoesNotExistError:
        frappe.db.rollback()
        return
    if run.site != frappe.local.site or run.status != "queued":
        frappe.db.rollback()
        return
    expire_stale()
    run.reload()
    if run.status != "queued":
        frappe.db.commit()
        return
    busy = frappe.db.sql("SELECT name FROM `tabRG AI Run` WHERE status='running' LIMIT 1 FOR UPDATE")
    first = frappe.db.sql("SELECT name FROM `tabRG AI Run` WHERE status='queued' ORDER BY creation,name LIMIT 1 FOR UPDATE")
    if busy or not first or first[0][0] != run_id or not acquire_execution(run.private_user):
        frappe.db.commit()
        return
    previous_user = frappe.session.user
    ctx = None
    try:
        frappe.set_user(run.private_user)
        guard(run.private_user, run.conversation, loads(run.expected, {}))
        if run.config_version != configuration_version() or run.mode != mode():
            frappe.throw("共享模型配置已变化，请重新发起。", frappe.PermissionError)
        run.status, run.started_at = "running", now_datetime()
        save(run)
        ctx = RunContext(run.private_user, run.conversation, run.name, loads(run.expected, {}), topic=run.queue_topic or None)
        ctx.selected_selections = set(loads(run.selection_ids, []))
        prompt = frappe.db.sql("SELECT content FROM `tabRG AI Message` WHERE request_id=%s AND role='user' FOR UPDATE",
                               (run.name,))[0][0]
        frappe.db.commit()
        answer = run_graph(ctx, prompt, provider or get_provider())
        if not isinstance(answer, str) or len(answer) > limits()["output_tokens"] * 8 or "\x00" in answer:
            frappe.throw("模型输出无效或超出上限。")
        ctx.check()
        require_capacity(ctx.user, len(answer.encode("utf-8")) + len(dumps(ctx.sources).encode("utf-8")) + 512)
        sequence = frappe.db.sql("SELECT COALESCE(MAX(sequence),0)+1 FROM `tabRG AI Message` WHERE conversation=%s FOR UPDATE",
                                 (ctx.conversation,))[0][0]
        insert("RG AI Message", ctx.user, conversation=ctx.conversation, sequence=sequence, role="assistant",
               content=answer, sources=dumps(ctx.sources), request_id=ctx.run)
        run = frappe.get_doc("RG AI Run", run.name, for_update=True)
        run.status, run.finished_at = "completed", now_datetime()
        run.model_calls, run.tool_order, run.elapsed = ctx.model_calls, dumps(ctx.tool_order), time.monotonic() - ctx.started
        run.usage_known = int(ctx.usage is not None)
        run.used_tokens = int(ctx.usage or 0)
        save(run)
        frappe.db.commit()
    except Exception as error:
        frappe.db.rollback()
        global_lock()
        current = frappe.get_doc("RG AI Run", run_id, for_update=True)
        if current.status in ACTIVE:
            current.status = "cancelled" if isinstance(error, frappe.PermissionError) else "failed"
            current.error_class = "context_changed" if isinstance(error, frappe.PermissionError) else (
                "timeout" if isinstance(error, TimeoutError) else "bounded_failure")
            current.finished_at = now_datetime()
            current.model_calls = ctx.model_calls if ctx else 0
            current.tool_order = dumps(ctx.tool_order) if ctx else "[]"
            current.usage_known = int(current.mode == "mock")
            current.used_tokens = 0
            save(current)
        if ctx:
            for name in ctx.operations:
                frappe.db.set_value("RG AI Operation", name, {"status": "cancelled", "parameters": "", "preview": "",
                                                              "sources": "[]"}, update_modified=False)
        # 不保存异常正文或traceback，避免provider错误回显prompt/密钥。
        frappe.db.commit()
    finally:
        release_execution(run.private_user)
        frappe.set_user(previous_user)
        schedule_next()


def schedule_next():
    global_lock()
    expire_stale()
    busy = frappe.db.sql("SELECT name FROM `tabRG AI Run` WHERE status='running' LIMIT 1 FOR UPDATE")
    rows = frappe.db.sql("SELECT name FROM `tabRG AI Run` WHERE status='queued' ORDER BY creation,name LIMIT 1 FOR UPDATE")
    frappe.db.commit()
    if rows and not busy and not execution_busy():
        try:
            enqueue(rows[0][0])
        except Exception:
            # 不重试供应商请求；下次状态查询明确结束过期等待。
            pass
