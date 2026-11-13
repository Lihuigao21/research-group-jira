"""Real HTTP portal acceptance, run centrally after the candidate DB tests.

Only fictional demo passwords from runtime/secrets/demo-credentials.json are
accepted by role_http.credentials(). No administrator, DB or container access.
Temporary home changes are restored; fictional direction/announcement evidence stays.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import quote

import role_http as h
import acceptance_config as cfg

TASK = cfg.current().task_dir
PORTALS = {"teacher": {"teacher"}, "one": {"student"}, "two": {"student"},
           "secretary": {"secretary", "student"}}
PORTAL_ROLES = {"teacher": "课题组教师", "secretary": "课题组秘书", "student": "课题组学生"}
NAVIGATION = {"topics": "List/RG Topic", "appointments": "List/RG Appointment",
              "materials": "List/RG Meeting Report", "surveys": "List/RG Survey",
              "meals": "rg-meals", "manuscripts": "rg-manuscripts", "accounts": "rg-accounts"}


def route(item, portal):
    return "rg-" + portal + "-agent" if item == "assistant" else NAVIGATION[item]


def rpc(client, method, **data):
    return h.rpc(client, method, **data)


def good(label, client, method, **data):
    return h.success(label, rpc(client, method, **data))


def no(label, client, method, **data):
    return h.denied(label, rpc(client, method, **data), statuses=(403, 417))


def snapshot(home):
    return {"introduction": home["introduction"], "navigation": home["navigation"]}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def verify_identity(clients):
    for key, client in clients.items():
        state = good("bootstrap " + key, client, "portals.bootstrap")
        h.check("actual portal set " + key, {row["id"] for row in state["portals"]} == PORTALS[key])
        h.check("server identity " + key, state["user"] == h.ACCOUNTS[key])
        for portal in PORTALS[key]:
            switched = good("authorized portal " + key + "/" + portal, client,
                            "portals.bootstrap", portal=portal)
            h.check("selected presentation " + key + "/" + portal, switched["portal"] == portal)
            h.check("navigation scoped " + key + "/" + portal,
                    all(row["route"] == route(row["id"], portal) for row in switched["navigation"]))
            ai = good("current role " + key + "/" + portal, client, "ai.api.bootstrap", portal=portal)
            h.check("single Agent role " + key + "/" + portal,
                    set(ai["roles"]) == {PORTAL_ROLES[portal]})
        for portal in {"teacher", "secretary", "student"} - PORTALS[key]:
            no("foreign portal denied " + key + "/" + portal, client, "portals.bootstrap", portal=portal)
            no("foreign Agent denied " + key + "/" + portal, client, "ai.api.bootstrap", portal=portal)
    secretary = clients["secretary"]
    before = set(good("dual role before", secretary, "ai.api.bootstrap", portal="student")["roles"])
    for portal in ("student", "secretary", "student", "secretary"):
        state = good("dual role switch " + portal, secretary, "portals.bootstrap", portal=portal)
        h.check("dual role retains both entries " + portal,
                {row["id"] for row in state["portals"]} == {"student", "secretary"})
        h.check("dual role only selected Agent entry " + portal,
                {row["route"] for row in state["navigation"] if row["id"] == "assistant"}
                == {route("assistant", portal)})
        h.check("dual role can manage ordinary students " + portal,
                good("account authority " + portal, secretary, "accounts.bootstrap")["can_manage"] is True)
        no("dual role never becomes teacher " + portal, secretary, "portals.directions")
    after = set(good("dual role after", secretary, "ai.api.bootstrap", portal="student")["roles"])
    h.check("role switch does not change Agent binding", before == after == {PORTAL_ROLES["student"]})
    for key in ("one", "two"):
        no("student cannot maintain home " + key, clients[key], "portals.home_settings")
        no("student cannot read teacher student list " + key, clients[key], "portals.teacher_students")
    no("teacher cannot maintain home", clients["teacher"], "portals.home_settings")


def verify_teacher(clients, nonce, evidence):
    teacher, one, secretary = (clients[key] for key in ("teacher", "one", "secretary"))
    students = good("teacher student list", teacher, "portals.teacher_students")["students"]
    h.check("both fictional students have entries",
            {h.ACCOUNTS["one"], h.ACCOUNTS["two"]}.issubset({row["user"] for row in students}))
    detail = good("teacher specific student", teacher, "portals.student_detail", student=h.ACCOUNTS["one"])
    h.check("student detail identity fixed", detail["student"]["user"] == h.ACCOUNTS["one"])
    h.check("student filter excludes other students",
            bool(detail["topics"]) and all(row["student"] == h.ACCOUNTS["one"] for row in detail["topics"]))
    h.check("latest progress includes source time",
            any(row.get("summary") and row.get("last_progress_at") for row in detail["topics"]))
    for key in ("one", "secretary"):
        no("student detail denied to " + key, clients[key], "portals.student_detail", student=h.ACCOUNTS["two"])
    report = h.success("HTTP teacher report filter", teacher.request("/api/method/frappe.desk.query_report.run",
        "POST", {"report_name": "RG Teacher Overview", "filters": {"student": h.ACCOUNTS["one"]},
                 "ignore_prepared_report": 1}))
    h.check("registered report enforces student filter",
            bool(report["result"]) and all(row["student"] == h.ACCOUNTS["one"] for row in report["result"]))
    directions = good("read directions", teacher, "portals.directions")
    h.check("direction topic options reflect teacher topics", bool(directions["available_topics"]))
    topic = detail["topics"][0]["topic"]
    standalone = good("create unlinked direction", teacher, "portals.save_direction",
        title="HTTP虚构独立方向-" + nonce, goal="仅用于隔离验收的组级目标", priority="高", topics=[])
    linked = good("create linked direction", teacher, "portals.save_direction",
        title="HTTP虚构关联方向-" + nonce, goal="关联本次演示课题", priority="中", topics=[topic])
    evidence["direction_ids"] = [standalone["name"], linked["name"]]
    configured = {row["name"]: row for row in good("read saved directions", teacher, "portals.directions")["directions"]}
    h.check("standalone direction remains without topics", configured[standalone["name"]]["topics"] == []
            and configured[standalone["name"]]["goal"] == "仅用于隔离验收的组级目标")
    h.check("linked direction contains exact topic", {row["topic"] for row in configured[linked["name"]]["topics"]} == {topic})
    updated = good("update direction current revision", teacher, "portals.save_direction", name=linked["name"],
        title="HTTP虚构关联方向-" + nonce, goal="更新后的组级目标", priority="高", topics=[topic],
        expected_revision=linked["revision"])
    h.check("direction revision advances", updated["revision"] == linked["revision"] + 1)
    no("direction stale overwrite rejected", teacher, "portals.save_direction", name=linked["name"],
       title="旧版本覆盖", topics=[], expected_revision=linked["revision"])
    h.denied("direction correct CSRF external Origin rejected", teacher.request(
        "/api/method/research_group.portals.save_direction", "POST",
        {"name": linked["name"], "title": "跨站方向覆盖", "goal": "跨站正文", "priority": "低", "topics": [],
         "expected_revision": updated["revision"]}, headers={"Origin": "https://outside.invalid"}))
    preserved = next(row for row in good("direction after negative requests", teacher,
                     "portals.directions")["directions"] if row["name"] == linked["name"])
    h.check("direction negative requests preserve current revision and goal",
            preserved["revision"] == updated["revision"] and preserved["goal"] == "更新后的组级目标")
    # Matching legacy text must not resurrect an explicitly removed link.
    # Rename the fictional configuration afterwards to retain the demo's
    # ordinary unconfigured aggregation.
    title = detail["topics"][0]["direction"]
    explicit = good("configure matching legacy direction", teacher, "portals.save_direction",
        title=title, goal="HTTP虚构方向解除回归-" + nonce, priority="中", topics=[topic])
    evidence["direction_ids"].append(explicit["name"])
    explicit_revision = explicit["revision"]
    try:
        unlinked = good("remove last explicit direction association", teacher, "portals.save_direction",
            name=explicit["name"], title=title, goal="HTTP虚构方向解除回归-" + nonce,
            priority="中", topics=[], expected_revision=explicit_revision)
        explicit_revision = unlinked["revision"]
        state = good("read explicitly emptied matching direction", teacher, "portals.directions")
        row = next(item for item in state["directions"] if item["name"] == explicit["name"])
        h.check("legacy title cannot restore removed topic", row["topics"] == [])
        h.check("unlinked topic remains available for deliberate association",
                topic in {item["name"] for item in state["available_topics"]})
    finally:
        good("restore ordinary legacy aggregation after unlink regression", teacher, "portals.save_direction",
             name=explicit["name"], title="HTTP虚构解除关联-" + nonce, goal="回归记录保留",
             priority="中", topics=[], expected_revision=explicit_revision)
    for key, client in (("student", one), ("secretary", secretary)):
        no("direction write denied " + key, client, "portals.save_direction", title="越权方向", topics=[])


def verify_home(clients, nonce, evidence, cleanup):
    secretary, teacher, student = (clients[key] for key in ("secretary", "teacher", "one"))
    original = good("read home settings", secretary, "portals.home_settings")
    cleanup["original_home"] = deepcopy(snapshot(original))
    evidence["original_home_sha256"] = digest(snapshot(original))
    nav = deepcopy(original["navigation"])
    nav.reverse(); nav[0]["label"] = "验收栏目"; nav[1]["visible"] = False
    intro = "HTTP隔离首页介绍-" + nonce + "\n纯文本与固定站内导航。"
    changed = good("save plain-text home and fixed navigation", secretary, "portals.save_home",
                   introduction=intro, navigation=nav, expected_revision=original["revision"])
    cleanup["home_touched"] = True
    h.check("home exact text and navigation order", changed["introduction"] == intro and changed["navigation"] == nav)
    h.check("fixed navigation IDs retained", {row["id"] for row in nav} == set(NAVIGATION) | {"assistant"})
    h.check("home optimistic revision advanced", changed["revision"] == original["revision"] + 1)
    public = good("render configured navigation", student, "portals.bootstrap")
    h.check("home introduction shown", public["introduction"] == intro)
    h.check("navigation targets fixed in source", all(row["route"] == route(row["id"], "student")
                                                       for row in public["navigation"]))
    h.check("hidden navigation stays hidden", not any(row["id"] == nav[1]["id"] for row in public["navigation"]))
    no("stale home write rejected", secretary, "portals.save_home", introduction="旧覆盖",
       navigation=nav, expected_revision=original["revision"])
    for value in ("<script>alert(1)</script>", "javascript:alert(1)", "data:text/html,unsafe"):
        no("home executable text rejected " + value.split(":", 1)[0], secretary, "portals.save_home",
           introduction=value, navigation=nav, expected_revision=changed["revision"])
    for label, patch in (("external route", {"route": "https://outside.invalid"}), ("unknown ID", {"id": "sql"}),
                         ("HTML label", {"label": "<img src=x>"})):
        bad = deepcopy(nav); bad[0].update(patch)
        no("navigation rejects " + label, secretary, "portals.save_home",
           introduction=intro, navigation=bad, expected_revision=changed["revision"])
    for role, client in (("teacher", teacher), ("student", student)):
        no("home write denied " + role, client, "portals.save_home", introduction="越权写入",
           navigation=nav, expected_revision=changed["revision"])
    no("forged server actor rejected", secretary, "portals.save_home", introduction=intro, navigation=nav,
       expected_revision=changed["revision"], edited_by=h.ACCOUNTS["teacher"])
    no("forged ignore_permissions rejected", secretary, "portals.save_home", introduction=intro,
       navigation=nav, expected_revision=changed["revision"], ignore_permissions=1)
    h.denied("correct CSRF external Origin rejected", secretary.request(
        "/api/method/research_group.portals.save_home", "POST",
        {"introduction": "外部来源覆盖", "navigation": nav, "expected_revision": changed["revision"]},
        headers={"Origin": "https://outside.invalid"}))
    current = good("verify rejected home writes did not mutate", secretary, "portals.home_settings")
    h.check("negative requests preserve exact home and revision", snapshot(current) == snapshot(changed)
            and current["revision"] == changed["revision"])
    restored = good("restore previous home", secretary, "portals.restore_home", expected_revision=changed["revision"])
    h.check("restore exact introduction and navigation", snapshot(restored) == snapshot(original))
    h.check("restore advances revision", restored["revision"] == changed["revision"] + 1)
    cleanup["home_touched"] = False
    evidence["restored_home_sha256"] = digest(snapshot(restored))


def verify_announcements(clients, nonce, evidence, cleanup):
    secretary, teacher, student = (clients[key] for key in ("secretary", "teacher", "one"))
    draft = good("announcement draft", secretary, "portals.save_announcement", title="HTTP虚构草稿-" + nonce,
                 body="草稿正文", status="draft", audience="all")
    cleanup["announcement_ids"].append(draft["name"])
    teacher_only = good("teacher-only published announcement", secretary, "portals.save_announcement",
        title="HTTP虚构教师公告-" + nonce, body="仅教师可见正文", status="published", audience="teacher")
    cleanup["announcement_ids"].append(teacher_only["name"])
    public = good("all-member published announcement", secretary, "portals.save_announcement",
        title="HTTP虚构全组公告-" + nonce, body="全组纯文本正文", status="published", audience="all")
    cleanup["announcement_ids"].append(public["name"])
    evidence["announcement_ids"] = list(cleanup["announcement_ids"])
    for key, client in (("student", student), ("teacher", teacher)):
        names = {row["name"] for row in good("announcement scope " + key, client, "portals.bootstrap")["announcements"]}
        h.check("draft hidden " + key, draft["name"] not in names)
        h.check("published all visible " + key, public["name"] in names)
        h.check("teacher audience enforced " + key, (teacher_only["name"] in names) == (key == "teacher"))
    no("published body cannot be overwritten", secretary, "portals.save_announcement", name=public["name"],
       title=public["title"], body="直接覆盖已发布正文", audience="all", status="published", expected_revision=public["revision"])
    no("stale announcement rejected", secretary, "portals.save_announcement", name=public["name"],
       title=public["title"], body=public["body"], audience="all", status="offline", expected_revision=0)
    no("student cannot publish announcement", student, "portals.save_announcement", title="越权公告", body="正文")
    no("announcement forged publisher rejected", secretary, "portals.save_announcement", name=public["name"],
       title=public["title"], body=public["body"], audience="all", status="offline",
       expected_revision=public["revision"], publisher=h.ACCOUNTS["teacher"])
    h.denied("announcement correct CSRF external Origin rejected", secretary.request(
        "/api/method/research_group.portals.save_announcement", "POST",
        {"name": public["name"], "title": public["title"], "body": public["body"], "audience": "all",
         "status": "offline", "expected_revision": public["revision"]}, headers={"Origin": "https://outside.invalid"}))
    preserved = next(row for row in good("announcement after negative requests", secretary,
                     "portals.home_settings")["announcements"] if row["name"] == public["name"])
    h.check("announcement negative requests preserve publication and revision",
            preserved["status"] == "published" and preserved["revision"] == public["revision"]
            and preserved["body"] == public["body"])
    offline = good("offline published announcement", secretary, "portals.save_announcement", name=public["name"],
        title=public["title"], body=public["body"], audience="all", status="offline", expected_revision=public["revision"])
    h.check("offline revision advances", offline["revision"] == public["revision"] + 1)
    h.check("offline announcement hidden", public["name"] not in {row["name"] for row in good(
        "read after offline", student, "portals.bootstrap")["announcements"]})


def verify_native(clients, evidence):
    native_records = [("RG Portal Settings", "group-portal"),
                      ("RG Research Direction", evidence["direction_ids"][0]),
                      ("RG Announcement", evidence["announcement_ids"][0])]
    for role in ("teacher", "secretary", "one"):
        client = clients[role]
        for doctype, name in native_records:
            path = "/api/resource/" + quote(doctype, safe="") + "/" + quote(name, safe="")
            h.denied("native JSON read denied " + role + "/" + doctype, client.request(path))
            h.denied("native JSON write denied " + role + "/" + doctype,
                     client.request(path, "PUT", {"flags": {"ignore_permissions": True}, "introduction": "原生越权"}))
        doc = {"doctype": "RG Portal Settings", "name": "group-portal", "introduction": "伪造服务令牌",
               "flags": {"ignore_permissions": True, "rg_service_token": True}, "ignore_permissions": True}
        h.denied("frappe.client JSON save rejects forged flags " + role,
                 client.request("/api/method/frappe.client.save", "POST", {"doc": doc}))
        h.denied("native JSON insert rejected " + role, client.request(
            "/api/resource/RG%20Announcement", "POST", {"title": "伪造公告", "body": "正文", "audience": "all",
                                                     "status": "published", "flags": {"ignore_permissions": True}}))


def cleanup_state(secretary, cleanup):
    current = good("cleanup read current home", secretary, "portals.home_settings")
    if snapshot(current) != cleanup["original_home"]:
        current = good("cleanup restore original home", secretary, "portals.save_home",
                       **cleanup["original_home"], expected_revision=current["revision"])
    h.check("final home content preserved", snapshot(current) == cleanup["original_home"])
    indexed = {row["name"]: row for row in current["announcements"]}
    for name in cleanup["announcement_ids"]:
        row = indexed.get(name)
        h.check("cleanup can find own announcement", row is not None)
        if row["status"] != "offline":
            good("cleanup offline fictional announcement", secretary, "portals.save_announcement", name=name,
                 title=row["title"], body=row["body"], audience=row["audience"], status="offline",
                 expected_revision=row["revision"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=cfg.current().base)
    parser.add_argument("--output", type=Path, default=cfg.current().receipt("portals-http-" + cfg.current().attempt + ".json"))
    args = parser.parse_args()
    if args.output.resolve() != cfg.current().receipt("portals-http-" + cfg.current().attempt + ".json"):
        parser.error("Output must match the configured task and attempt")
    if args.output.exists():
        parser.error("An existing acceptance receipt must not be overwritten")
    h.BASE = args.base.rstrip("/")
    h.RESULTS = []
    evidence, cleanup = {}, {"announcement_ids": [], "home_touched": False}
    report = {"suite": "portals-http", "base": h.BASE,
              "started_at": datetime.now(timezone.utc).isoformat(), "passed": False,
              "checks": h.RESULTS, "evidence": evidence, "secrets_recorded": False, "fictional_only": True}
    clients = {}
    error = None
    try:
        passwords = h.credentials()
        clients = {key: h.Client().login(key, passwords) for key in PORTALS}
        guest = h.Client()
        h.denied("guest has no portal", rpc(guest, "portals.bootstrap"))
        nonce = secrets.token_hex(5)
        verify_identity(clients)
        verify_teacher(clients, nonce, evidence)
        verify_home(clients, nonce, evidence, cleanup)
        verify_announcements(clients, nonce, evidence, cleanup)
        verify_native(clients, evidence)
    except Exception as caught:
        # Never persist response bodies, cookies, CSRF tokens or credentials.
        error = type(caught).__name__
    finally:
        if "original_home" in cleanup and clients.get("secretary"):
            try:
                cleanup_state(clients["secretary"], cleanup)
            except Exception as caught:
                report["cleanup_error"] = type(caught).__name__
                error = error or "CleanupFailed"
        report["passed"] = error is None
        report["ok"] = report["passed"]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if error:
            report["error_type"] = error
            report["last_check"] = h.RESULTS[-1]["check"] if h.RESULTS else None
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as destination:
            destination.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "checks": len(h.RESULTS), "record": str(args.output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
