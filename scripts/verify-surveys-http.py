"""Real HTTP acceptance for named secretary surveys; fictional local accounts only."""
import concurrent.futures
import csv
import importlib.util
import io
import json
import secrets
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks/20261106-surveys"
loader = importlib.util.spec_from_file_location("rg_core_http", ROOT / "scripts/verify-http.py")
core = importlib.util.module_from_spec(loader)
loader.loader.exec_module(core)
CHECKS, RECORDS = [], {}
core.RESULTS = CHECKS
check = core.check


def rpc(client, method, **data):
    return client.request("/api/method/research_group.surveys." + method, "POST", data)


def successful(label, response):
    status, payload, _ = response
    check(label, status == 200 and "message" in (payload or {}), status=status)
    return payload["message"]


def denied(label, response):
    status, _, _ = response
    check(label, status in (403, 417), status=status)


def listing(client, doctype, filters, fields=None):
    query = urllib.parse.urlencode({"filters": json.dumps(filters),
        "fields": json.dumps(fields or ["name"]), "limit_page_length": 100})
    status, payload, _ = client.request("/api/resource/" + urllib.parse.quote(doctype) + "?" + query)
    check("HTTP 列表权限正常处理 " + doctype, status == 200, status=status)
    return payload["data"]


def update(client, doctype, name, fields):
    return client.request("/api/resource/" + urllib.parse.quote(doctype) + "/" +
                          urllib.parse.quote(name), "PUT", fields)


def create_survey(secretary, title, deadline=None, questions=None):
    fields = {"title": title, "questions": questions if questions is not None else [
        {"title": "希望讨论的时间", "question_type": "单选", "required": 1,
         "options": "周一\n周二"},
        {"title": "可参与的事项", "question_type": "多选", "required": 1,
         "options": "组会\n讨论\n报告"},
        {"title": "补充信息", "question_type": "简答", "required": 1},
    ]}
    if deadline:
        fields["deadline"] = deadline
    return secretary.create("RG Survey", fields)


def main():
    passwords = json.loads((ROOT / "runtime/secrets/demo-credentials.json").read_text(encoding="utf-8"))
    clients = {key: core.Client().login(key, passwords) for key in core.ACCOUNTS}
    secretary, one, two, teacher = (clients[key] for key in ("secretary", "one", "two", "teacher"))
    label = "HTTP实名问卷-" + secrets.token_hex(4)
    for suffix, questions in (("空题", []), ("重复选项", [
            {"title": "非法发布用例", "question_type": "单选", "required": 1, "options": "A\nA"}])):
        invalid = create_survey(secretary, label + "-" + suffix, questions=questions)
        denied("HTTP 发布拒绝" + suffix, rpc(secretary, "publish_survey", name=invalid["name"]))
        status, payload, _ = secretary.get("RG Survey", invalid["name"])
        check("HTTP 发布失败保留草稿 " + suffix, status == 200 and payload["data"]["status"] == "草稿")
    survey = create_survey(secretary, label)
    name = survey["name"]
    RECORDS["survey"] = name
    check("HTTP 草稿状态", survey["status"] == "草稿")
    for role, member in (("学生", one), ("教师", teacher)):
        denied("HTTP " + role + "不可读取草稿", member.get("RG Survey", name))
        check("HTTP " + role + "列表不含草稿", not listing(member, "RG Survey", {"name": name}))
        denied("HTTP " + role + "不可建问卷", member.request("/api/resource/RG%20Survey", "POST", {
            "title": label, "questions": survey["questions"]}))
    successful("HTTP 秘书发布", rpc(secretary, "publish_survey", name=name))
    status, payload, _ = one.get("RG Survey", name)
    check("HTTP 已发布问卷与题目可读", status == 200 and len(payload["data"]["questions"]) == 3,
          status=status)
    survey = payload["data"]
    q1, q2, q3 = [row["name"] for row in survey["questions"]]
    answers = {q1: "周一", q2: ["组会", "讨论"], q3: "'=2+2"}
    denied("HTTP 发布后不可改题", update(secretary, "RG Survey", name, {
        "questions": [{**row, "title": "修改题目"} for row in survey["questions"]]}))
    for label_bad, values in (("必填缺失", {q1: "周一"}),
                              ("非法选项", {**answers, q1: "未定义的选项"}),
                              ("未知题目", {**answers, "not-a-question": "伪造"})):
        denied("HTTP " + label_bad, rpc(one, "submit_response", survey=name, answers=values))
    check("HTTP 失败提交无半份答卷", secretary.count("RG Survey Response", {"survey": name}) == 0)
    successful("HTTP 首次提交", rpc(one, "submit_response", survey=name, answers=answers))
    rows = listing(secretary, "RG Survey Response", {"survey": name},
                   ["name", "respondent", "respondent_name", "revision", "answers_json"])
    check("HTTP 实名身份由服务器确定", len(rows) == 1 and rows[0]["respondent"] == core.ACCOUNTS["one"]
          and bool(rows[0]["respondent_name"]))
    response_name = rows[0]["name"]
    RECORDS["response"] = response_name
    first_revision = rows[0]["revision"]
    latest = {q1: "周二", q2: ["报告"], q3: "=2+2"}
    successful("HTTP 截止前修改", rpc(one, "submit_response", survey=name, answers=latest))
    own = successful("HTTP 本人答卷入口", rpc(one, "my_response", survey=name))
    check("HTTP 本人入口仅返回当前身份", own["respondent"] == core.ACCOUNTS["one"]
          and own["answers"] == latest)
    check("HTTP 修改仍为同一份答卷", secretary.count("RG Survey Response", {"survey": name}) == 1)
    rows = listing(secretary, "RG Survey Response", {"survey": name}, ["name", "revision", "answers_json"])
    check("HTTP 最新答复与修订号", json.loads(rows[0]["answers_json"]) == latest
          and rows[0]["revision"] == first_revision + 1)
    for role, member in (("其他学生", two), ("普通教师", teacher), ("未登录者", core.Client())):
        denied("HTTP " + role + "不可读他人答卷", member.get("RG Survey Response", response_name))
        denied("HTTP " + role + "不可看汇总", rpc(member, "survey_results", survey=name))
        denied("HTTP " + role + "不可导出", rpc(member, "export_csv", survey=name))
    for role, member in (("学生", one), ("教师", teacher), ("秘书", secretary)):
        before_forgery = secretary.count("RG Survey Response", {"survey": name})
        forged_status, forged_payload, _ = member.request(
            "/api/resource/RG%20Survey%20Response", "POST", {"survey": name,
            "respondent": core.ACCOUNTS["two"], "respondent_name": "伪造身份",
            "answers_json": json.dumps(latest), "submitted_at": own["submitted_at"],
            "updated_at": own["updated_at"], "revision": 1})
        check("HTTP " + role + "不可通过普通CRUD伪造完整答卷",
              forged_status == 403 and (forged_payload or {}).get("exc_type") == "PermissionError",
              status=forged_status, exc_type=(forged_payload or {}).get("exc_type"))
        check("HTTP " + role + "伪造被拒后答卷数不变",
              secretary.count("RG Survey Response", {"survey": name}) == before_forgery)
    denied("HTTP 普通CRUD不可篡改现有答卷", update(one, "RG Survey Response", response_name,
        {"respondent": core.ACCOUNTS["two"], "answers_json": "{}"}))
    successful("HTTP 教师提交本人答卷", rpc(teacher, "submit_response", survey=name,
        answers={q1: "周一", q2: ["组会"], q3: "教师本人意见"}))
    teacher_rows = listing(teacher, "RG Survey Response", {"survey": name}, ["name", "respondent"])
    check("HTTP 普通教师列表仅本人", len(teacher_rows) == 1 and
          teacher_rows[0]["respondent"] == core.ACCOUNTS["teacher"])
    second_session = core.Client().login("two", passwords)
    variants = [{q1: "周一", q2: ["讨论"], q3: "并发A"},
                {q1: "周二", q2: ["报告"], q3: "并发B"}]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(rpc, client, "submit_response", survey=name, answers=value)
                   for client, value in zip((two, second_session), variants)]
        for index, future in enumerate(futures):
            successful("HTTP 同人并发提交 " + str(index + 1), future.result())
    check("HTTP 同人并发只计一份", secretary.count("RG Survey Response", {
        "survey": name, "respondent": core.ACCOUNTS["two"]}) == 1)
    check("HTTP 三个实名成员各一份", secretary.count("RG Survey Response", {"survey": name}) == 3)
    result = successful("HTTP 秘书读取实名汇总", rpc(secretary, "survey_results", survey=name))
    RECORDS["results"] = result
    check("HTTP 实名明细与填写人数", result["submitted_count"] == 3 and
          {response["respondent"] for response in result["responses"]} ==
          {core.ACCOUNTS[key] for key in ("one", "two", "teacher")} and
          result["active_submitted_count"] == 3 and
          result["pending_count"] == result["active_member_count"] - 3)
    second_answers = successful("HTTP 并发后本人答卷", rpc(two, "my_response", survey=name))["answers"]
    check("HTTP 并发保留一份完整内容", second_answers in variants)
    questions = {question["name"]: question for question in result["questions"]}
    single_counts = {option["label"]: option["count"] for option in questions[q1]["options"]}
    multiple_counts = {option["label"]: option["count"] for option in questions[q2]["options"]}
    check("HTTP 单选统计对应修改后内容", single_counts == {
        "周一": 1 + int(second_answers[q1] == "周一"), "周二": 1 + int(second_answers[q1] == "周二")})
    check("HTTP 多选统计对应修改后内容", multiple_counts == {
        "组会": 1, "讨论": int("讨论" in second_answers[q2]),
        "报告": 1 + int("报告" in second_answers[q2])})
    check("HTTP 简答汇总对应最新答复", any(answer["respondent"] == core.ACCOUNTS["one"]
          and answer["answer"] == "=2+2" for answer in questions[q3]["answers"]))
    status, _, body = rpc(secretary, "export_csv", survey=name)
    check("HTTP 秘书CSV可下载", status == 200, status=status)
    csv_rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    check("HTTP CSV包含身份和最新内容", core.ACCOUNTS["one"] in body.decode("utf-8-sig")
          and any("'=2+2" == cell for row in csv_rows for cell in row))
    check("HTTP CSV防公式注入", not any(cell.startswith(("=", "+", "-", "@", "\t", "\r"))
          for row in csv_rows for cell in row))
    RECORDS["csv_rows"] = len(csv_rows)
    # Account/member disable boundaries run against independent fictional users
    # in TestSurveys, preserving the existing demonstration members byte-for-byte.
    successful("HTTP 秘书提前关闭", rpc(secretary, "close_survey", name=name))
    denied("HTTP 已关闭拒绝修改", rpc(one, "submit_response", survey=name, answers=answers))
    denied("HTTP 已关闭拒绝首次提交", rpc(secretary, "submit_response", survey=name, answers=answers))
    after = listing(secretary, "RG Survey Response", {"name": response_name}, ["answers_json"])
    check("HTTP 关闭后保留旧答卷", json.loads(after[0]["answers_json"]) == latest)
    clock = successful("HTTP 获取实际服务器时间", rpc(secretary, "get_survey", name=name))
    deadline = datetime.fromisoformat(clock["server_time"]) + timedelta(seconds=45)
    expiring = create_survey(secretary, label + "-截止", deadline.strftime("%Y-%m-%d %H:%M:%S.%f"))
    RECORDS["expiring_survey"] = expiring["name"]
    successful("HTTP 发布有截止时间问卷", rpc(secretary, "publish_survey", name=expiring["name"]))
    eq1, eq2, eq3 = [row["name"] for row in expiring["questions"]]
    expiry_answers = {eq1: "周一", eq2: ["组会"], eq3: "截止前内容"}
    successful("HTTP 截止前可提交", rpc(one, "submit_response", survey=expiring["name"], answers=expiry_answers))
    observations = []
    RECORDS["expiry_server_observations"] = observations
    wait_limit = time.monotonic() + 90
    while True:
        observed = successful("HTTP 观察服务器截止状态", rpc(one, "get_survey", name=expiring["name"]))
        observations.append({"server_time": observed["server_time"],
                             "accepting_responses": observed["accepting_responses"]})
        if not observed["accepting_responses"]:
            break
        if time.monotonic() >= wait_limit:
            raise AssertionError("服务器在有界等待内未到达截止状态")
        time.sleep(1)
    check("HTTP 服务器已达到截止时间", observed["status"] == "已发布"
          and datetime.fromisoformat(observed["server_time"]) >= deadline)
    denied("HTTP 超时拒绝修改", rpc(one, "submit_response", survey=expiring["name"], answers=expiry_answers))
    denied("HTTP 超时拒绝首次提交", rpc(two, "submit_response", survey=expiring["name"], answers=expiry_answers))
    check("HTTP 超时保留既有答卷", secretary.count("RG Survey Response", {"survey": expiring["name"]}) == 1)


if __name__ == "__main__":
    failure = None
    try:
        main()
    except Exception as error:
        failure = type(error).__name__ + ": " + str(error)
    result = {"base_url": core.BASE, "fictional_only": True, "checks": CHECKS,
              "records": RECORDS, "error": failure}
    (TASK / "records/http-verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": sum(row["passed"] for row in CHECKS),
                      "total": len(CHECKS), "error": failure}, ensure_ascii=False))
    sys.exit(1 if failure else 0)
