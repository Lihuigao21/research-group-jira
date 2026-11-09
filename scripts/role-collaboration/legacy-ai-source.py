"""独立HTTP验收；只使用本机虚构账号，凭据不写回执。"""
import concurrent.futures
import importlib.util
import json
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
loader = importlib.util.spec_from_file_location("ai_http_core", ROOT / "scripts/verify-http.py")
core = importlib.util.module_from_spec(loader)
loader.loader.exec_module(core)
CHECKS = []
core.RESULTS = CHECKS
check = core.check
PREFIX = "/api/method/research_group.ai.api."


def browser_client():
    client = core.Client()
    # Match a real browser: every AJAX POST includes the exact port-bearing origin.
    client.opener.addheaders = [("Origin", core.BASE)]
    return client


def rpc(client, method, **values):
    return client.request(PREFIX + method, "POST", values)


def ok(label, response):
    status, data, _ = response
    check(label, status == 200 and isinstance(data, dict) and "message" in data, status=status)
    return data["message"]


def denied(label, response):
    status, _, _ = response
    check(label, status in {400, 401, 403, 404, 417}, status=status)


def ask(client, conversation, prompt, topic=None):
    run = ok("提交模拟聊天", rpc(client, "send_message", conversation=conversation, prompt=prompt, topic=topic))
    identifier = run.get("run_id") or run.get("name")
    check("服务端生成运行ID", bool(identifier))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        value = ok("读取本人运行状态", rpc(client, "get_run", run_id=identifier))
        if value["status"] not in {"queued", "running"}:
            check("模拟聊天完成", value["status"] == "completed", state=value["status"])
            check("明确标识离线模拟", value.get("mode") == "mock" and
                  "离线模拟" in (value.get("message") or {}).get("content", ""))
            return value
        time.sleep(0.25)
    raise AssertionError("模拟回合超时，未把等待当完成")


def proposal(client, conversation, action, parameters):
    value = ok("生成待确认预览", rpc(client, "propose_change", conversation=conversation,
                                   action=action, parameters=parameters))
    check("预览尚未提交", value["status"] == "pending" and bool(value.get("preview")))
    return value["name"]


def main():
    passwords = json.loads((ROOT / "runtime/secrets/demo-credentials.json").read_bytes())
    clients = {key: browser_client().login(key, passwords) for key in core.ACCOUNTS}
    one, two, teacher, secretary = (clients[k] for k in ("one", "two", "teacher", "secretary"))
    boot = {key: ok("读取AI配置 " + key, rpc(client, "bootstrap")) for key, client in clients.items()}
    check("所有人共用模拟配置", all(v["configuration"]["mode"] == "mock" for v in boot.values()))
    conv = {key: ok("创建本人会话 " + key, rpc(client, "create_conversation"))["name"]
            for key, client in clients.items()}
    marker = "AI独立HTTP虚构-" + secrets.token_hex(4)
    result = ask(one, conv["one"], marker)
    check("本人会话可回读", marker in str(ok("读取本人历史", rpc(one, "get_conversation", name=conv["one"]))))
    for key in ("two", "teacher", "secretary"):
        denied("拒绝跨人会话 " + key, rpc(clients[key], "get_conversation", name=conv["one"]))
        denied("拒绝跨人运行 " + key, rpc(clients[key], "get_run", run_id=result["run_id"]))
        listing = ok("本人会话列表 " + key, rpc(clients[key], "list_conversations"))
        check("列表不含其他人会话 " + key, conv["one"] not in {r["name"] for r in listing["items"]})
    denied("未登录不可访问AI", rpc(browser_client(), "bootstrap"))
    for doctype in ("RG AI Conversation", "RG AI Message", "RG AI Memory", "RG AI Operation", "RG AI Run"):
        path = "/api/resource/" + urllib.parse.quote(doctype)
        response = teacher.request(path)
        status, body, _ = response
        check("原生列表不回显AI内容 " + doctype,
              status in {403, 417} or (status == 200 and not body.get("data")), status=status)
    denied("本人也不能原生写AI", one.request("/api/resource/RG%20AI%20Memory", "POST", {
        "private_user": core.ACCOUNTS["two"], "content": marker, "status": "active"}))
    denied("模型工具不能作为公共RPC直调", one.request(
        "/api/method/research_group.ai.tools.dispatch", "POST", {"name": "get_group_discussion_overview"}))
    denied("禁止未知提案动作", rpc(one, "propose_change", conversation=conv["one"],
                                  action="approve_appointment", parameters={}))
    denied("提案不接收伪造身份", rpc(one, "propose_change", conversation=conv["one"], action="memory_change",
                                   parameters={"change": "add", "kind": "preference", "content": marker,
                                               "actor": core.ACCOUNTS["teacher"]}))
    memory = proposal(one, conv["one"], "memory_change", {"change": "add", "kind": "preference", "content": marker})
    denied("他人不可读取操作", rpc(teacher, "get_operation", operation_id=memory))
    denied("他人不可确认操作", rpc(two, "decide_operation", operation_id=memory, decision="confirm"))
    one.opener.addheaders = [("Origin", "https://outside.example.invalid")]
    denied("确认拒绝跨站Origin", rpc(one, "decide_operation", operation_id=memory, decision="confirm"))
    one.opener.addheaders = [("Origin", "http://localhost:8089")]
    denied("确认拒绝其它端口Origin", rpc(one, "decide_operation", operation_id=memory, decision="confirm"))
    one.opener.addheaders = [("Referer", "https://outside.example.invalid/forged")]
    denied("无Origin时拒绝跨站Referer", rpc(one, "decide_operation", operation_id=memory, decision="confirm"))
    one.opener.addheaders = [("Origin", core.BASE)]
    saved_csrf = one.csrf
    one.csrf = None
    denied("确认必须有CSRF", rpc(one, "decide_operation", operation_id=memory, decision="confirm"))
    one.csrf = saved_csrf
    denied("确认拒绝额外改参", rpc(one, "decide_operation", operation_id=memory, decision="confirm",
                                 parameters={"content": "篡改的正文"}))
    confirmed = ok("本人确认记忆", rpc(one, "decide_operation", operation_id=memory, decision="confirm"))
    replay = ok("重复确认返回原回执", rpc(one, "decide_operation", operation_id=memory, decision="confirm"))
    check("重复确认不重复创建", confirmed["result"] == replay["result"])
    memory_name = confirmed["result"]["name"]
    items = ok("本人记忆列表", rpc(one, "private_items", kind="memory"))["items"]
    check("确认后记忆可用", any(r["name"] == memory_name and r["status"] == "active" for r in items))
    for key in ("two", "teacher", "secretary"):
        items = ok("他人只见自己记忆 " + key, rpc(clients[key], "private_items", kind="memory"))["items"]
        check("记忆不串人 " + key, memory_name not in {r["name"] for r in items})
    recalled = ask(one, conv["one"], "查看我的记忆")
    check("跨回合读取本人有效记忆", marker in recalled["message"]["content"])
    cancelled = proposal(one, conv["one"], "plan_change", {"change": "add", "content": marker + "未保存计划"})
    ok("取消计划提案", rpc(one, "decide_operation", operation_id=cancelled, decision="cancel"))
    plans = ok("读取私人计划", rpc(one, "private_items", kind="plan"))["items"]
    check("取消无计划写入", not any(marker + "未保存计划" in r["content"] for r in plans))
    plan = proposal(one, conv["one"], "plan_change", {"change": "add", "content": marker + "本人核对计划"})
    plan_result = ok("确认保存计划", rpc(one, "decide_operation", operation_id=plan, decision="confirm"))
    check("计划有独立服务器ID", plan_result["result"]["doctype"] == "RG AI Plan")
    topic = boot["one"]["topics"][0]["name"]
    progress = proposal(one, conv["one"], "progress", {"topic": topic, "progress_date": datetime.now().date().isoformat(),
                         "summary": marker + "虚构追加进展", "blockers": "", "next_milestone": "仅验收，不代表科研结果"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: rpc(one, "decide_operation", operation_id=progress, decision="confirm"), range(2)))
    progress_replies = [ok("并发重复确认", response) for response in replies]
    check("并发确认仅一条进展", progress_replies[0]["result"]["name"] == progress_replies[1]["result"]["name"])
    progress_name = progress_replies[0]["result"]["name"]
    check("教师可读取正式进展", teacher.get("RG Progress", progress_name)[0] == 200)
    denied("其他学生不可读取进展", two.get("RG Progress", progress_name))
    start = (datetime.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    appointment = proposal(one, conv["one"], "appointment", {"topic": topic, "teacher": core.ACCOUNTS["teacher"],
                            "start_at": str(start), "end_at": str(start + timedelta(minutes=30)), "agenda": marker})
    applied = ok("确认提交预约申请", rpc(one, "decide_operation", operation_id=appointment, decision="confirm"))
    check("AI预约仍待教师审批", applied["result"]["status"] == "待审批")
    deletion = proposal(one, conv["one"], "memory_change", {"change": "delete", "name": memory_name})
    ok("确认删除记忆", rpc(one, "decide_operation", operation_id=deletion, decision="confirm"))
    items = ok("删除后记忆列表", rpc(one, "private_items", kind="memory"))["items"]
    check("已删记忆不再列出", memory_name not in {r["name"] for r in items})
    history = ok("删除后读取旧历史", rpc(one, "get_conversation", name=conv["one"]))
    target = next(r for r in history["messages"] if r["request_id"] == recalled["run_id"] and r["role"] == "assistant")
    check("旧派生答复屏蔽已删记忆", target["blocked"] and marker not in target["content"])
    ok("删除本人会话", rpc(one, "remove_conversation", name=conv["one"]))
    denied("已删会话不回放", rpc(one, "get_conversation", name=conv["one"]))
    denied("已删会话的运行不回显", rpc(one, "get_run", run_id=result["run_id"]))
    ok("教师只读总览模拟", rpc(teacher, "set_memory_enabled", enabled=False))
    ask(teacher, conv["teacher"], "全组最近讨论")
    ask(secretary, conv["secretary"], "问卷完成概况")


if __name__ == "__main__":
    passed, error = False, None
    try:
        main()
        passed = True
    except Exception as exc:
        error = {"type": type(exc).__name__, "check": str(exc) if isinstance(exc, AssertionError) else "见失败检查项"}
    report = {"ok": passed, "checks": CHECKS, "total": len(CHECKS), "error": error, "paid_api_called": False}
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    target = ROOT / ("tasks/20261107-ai-assistant/records/independent-http-" + stamp + ".json")
    target.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps({"ok": passed, "total": len(CHECKS), "error": error,
                      "record": str(target.relative_to(ROOT))}, ensure_ascii=False))
    raise SystemExit(0 if passed else 1)
