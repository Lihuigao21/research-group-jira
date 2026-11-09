"""A bounded single-run LangGraph; Frappe remains the only persistent store.

The graph can read and propose. Confirmation is deliberately absent from both
the graph and its tool inventory. No checkpointer, Store, or tracing callback is
installed, so deleting Frappe content cannot resurrect a graph snapshot.
"""
import re
from typing import TypedDict

import frappe

from research_group.ai.config import limits
from research_group.ai.context import assemble
from research_group.ai.storage import loads
from research_group.ai.tools import available, dispatch, validate_arguments

GRAPH_VERSION = "rg-site-graph-1"
SCOPE_VERSION = "rg-site-scope-2"
OUTSIDE = "我只协助本站的课题组工作：材料提交、预约、问卷、工作餐、论文选段修改、记录查询和私人计划。请在对应工作台选择任务。"
CLARIFY = "请说明要办理的站内事项，或先选择课题、论文选区及对应工作台。"
PREVIEW = "已生成待本人确认的操作卡。请核对原文、来源和变更内容，再在网页确认；当前尚未写入正式记录。"

SITE_TERMS = re.compile(r"本站|站内|课题组|课题|进展|讨论|反馈|组会|排期|日程|预约|问卷|投票|工作餐|订餐|论文|选区|选段|稿件|材料|PPT|计划|下一步|安排工作|记忆|记住|账号|账户|邮箱|密码|通知|公告|工作台|方向|里程碑", re.I)
# Generic website/platform wording is not a task. Require an explicit reference
# to this site or one of the bounded workflows; semantic routing remains fallible.
# A prefix such as 'prepare to upload' does not turn an external task into a
# site task. This is a conservative product router, not a semantic proof.
EXTERNAL_TASK = re.compile(r"写(?:一段|个|一个)?(?:Python|JavaScript|代码|程序)|编写.*(?:代码|脚本)|解(?:答|一道)?.*(?:物理题|数学题)|天气预报|股票(?:预测|推荐)|讲(?:个|一个)?笑话|推荐电影|旅游攻略|通用聊天|自由闲聊|\b(?:write code|weather forecast|stock pick|tell a joke)\b", re.I)
DANGEROUS = re.compile(r"(?:执行|运行|调用|开放|添加).{0,16}(?:SQL|Shell|bash|powershell|浏览器|MCP)|(?:忽略|绕过|取消).{0,12}(?:权限|白名单|系统指令|范围限制)|(?:提升|修改|授予).{0,10}(?:权限|角色)|(?:自动|直接).{0,8}(?:群发|发送邮件|批准|发布)", re.I)


class GraphState(TypedDict, total=False):
    route: str
    answer: str
    calls: list
    results: list
    finished: bool


def route_scope(prompt):
    if EXTERNAL_TASK.search(prompt) or DANGEROUS.search(prompt):
        return "outside"
    return "site" if SITE_TERMS.search(prompt) else "clarify"


def answer_text(value, *, structured=True):
    """Reject malformed/out-of-scope output before the runner publishes it."""
    if structured:
        try:
            decoded = loads(value, {}) if isinstance(value, str) else {}
        except frappe.PermissionError:
            # Provider text is untrusted; malformed/duplicate-key JSON is a
            # bounded clarification, never content to publish or execute.
            return CLARIFY
        if (not isinstance(decoded, dict) or set(decoded) != {"scope", "answer"}
                or decoded.get("scope") != "site" or not isinstance(decoded.get("answer"), str)):
            return CLARIFY
        value = decoded["answer"]
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or len(value) > limits()["output_tokens"] * 8):
        frappe.throw("模型输出无效或超出上限。")
    if (EXTERNAL_TASK.search(value) or DANGEROUS.search(value)
            or re.search(r"https?://|<script\b|javascript:|```", value, re.I)
            or re.search(r"(?:已|成功)(?:群发|发送邮件|发布公告|批准预约|启用账号|提升权限)", value)):
        return OUTSIDE
    return value


def run_graph(ctx, prompt, provider):
    from research_group.ai.providers import disable_tracing
    disable_tracing()
    from langgraph.graph import END, START, StateGraph

    # Per-run closures only. Neither user content nor provider objects are
    # serialized to a remote tracer, checkpoint, cache, or shared graph state.
    session = None

    def guard_request(state):
        ctx.check()
        return {}

    def scope_route(state):
        route = route_scope(prompt)
        if route == "clarify" and ctx.selected_selections:
            route = "site"
        if route != "site":
            ctx.usage = 0
        return {"route": route, "answer": OUTSIDE if route == "outside" else CLARIFY}

    def assemble_allowed_context(state):
        nonlocal session
        if len(ctx.selected_selections) > 1:
            frappe.throw("本轮只处理一个本人明确选择的论文段落。")
        assemble(ctx, prompt)
        frappe.db.commit()
        session = provider.session(ctx, prompt) if hasattr(provider, "session") else _SingleResult(provider, ctx, prompt)
        return {}

    def call_model(state):
        ctx.check()
        step = session.step(state.get("results", []))
        if not isinstance(step, dict) or set(step) - {"calls", "answer", "finished", "structured"}:
            frappe.throw("模型回执结构无效。")
        return {"calls": step.get("calls", []), "answer": step.get("answer", ""),
                "finished": bool(step.get("finished")), "results": []}

    def validate_tool_calls(state):
        ctx.check()
        calls = state.get("calls", [])
        if not isinstance(calls, list) or not calls or len(calls) > limits()["tool_calls"]:
            frappe.throw("模型工具回执无效。")
        names = available(ctx.check()["roles"])
        for call in calls:
            if (not isinstance(call, dict) or set(call) != {"name", "args", "id"}
                    or call["name"] not in names or not isinstance(call["id"], str)
                    or not 1 <= len(call["id"]) <= 200):
                frappe.throw("模型请求了未允许工具。", frappe.PermissionError)
            validate_arguments(call["name"], call["args"])
        return {}

    def dispatch_fixed_tool(state):
        results = []
        for call in state["calls"]:
            ctx.check()
            before = len(ctx.operations)
            value = dispatch(ctx, call["name"], call["args"])
            results.append({"id": call["id"], "name": call["name"], "data": value})
            # A proposal ends this run. It can only be consumed by the separate
            # browser confirmation API; graph loops can never repeat the write.
            if len(ctx.operations) > before:
                return {"results": results, "finished": True, "answer": PREVIEW}
        return {"results": results}

    def validate_answer(state):
        ctx.check()
        if state["route"] != "site" or state.get("answer") == PREVIEW:
            answer = state["answer"]
        else:
            answer = answer_text(state.get("answer"), structured=bool(getattr(session, "structured", False)))
        return {"answer": answer}

    graph = StateGraph(GraphState)
    for name, function in (("guard_request", guard_request), ("scope_route", scope_route),
                           ("assemble_allowed_context", assemble_allowed_context), ("call_model", call_model),
                           ("validate_tool_calls", validate_tool_calls), ("dispatch_fixed_tool", dispatch_fixed_tool),
                           ("validate_answer", validate_answer)):
        graph.add_node(name, function)
    graph.add_edge(START, "guard_request")
    graph.add_edge("guard_request", "scope_route")
    graph.add_conditional_edges("scope_route", lambda state: "assemble_allowed_context" if state["route"] == "site" else "validate_answer")
    graph.add_edge("assemble_allowed_context", "call_model")
    graph.add_conditional_edges("call_model", lambda state: "validate_tool_calls" if state.get("calls") else "validate_answer")
    graph.add_edge("validate_tool_calls", "dispatch_fixed_tool")
    graph.add_conditional_edges("dispatch_fixed_tool", lambda state: "validate_answer" if state.get("finished") else "call_model")
    graph.add_edge("validate_answer", END)
    # Explicitly no checkpointer/Store. Budget checks inside ctx remain primary.
    compiled = graph.compile(checkpointer=None, store=None)
    try:
        from langsmith import tracing_context
        # Graph callbacks as well as model callbacks remain local. A process
        # environment/global tracing context cannot export private node state.
        with tracing_context(enabled=False):
            result = compiled.invoke({}, config={"recursion_limit": 12 + 4 * (limits()["tool_calls"] + limits()["model_calls"]),
                                                 "callbacks": [], "tags": [GRAPH_VERSION]})
        return result["answer"]
    finally:
        if session is not None and hasattr(session, "close"):
            session.close()


class _SingleResult:
    """Python-only adapter for bounded test providers, not a web extension hook."""
    structured = False

    def __init__(self, provider, ctx, prompt):
        self.provider, self.ctx, self.prompt = provider, ctx, prompt

    def step(self, results):
        return {"answer": self.provider.run(self.ctx, self.prompt), "finished": True}
