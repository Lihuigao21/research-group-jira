"""Role workspaces and narrowly scoped, plain-text group home maintenance.

The selected portal is presentation state. Every request checks the current
database identity under the same authorization gate as identity changes.
"""
import json
import re

import frappe
from frappe.model.document import Document
from frappe.utils import cint, now_datetime

from research_group.ai.security import current_roles, global_lock
from research_group.policy import ROLES, SECRETARY, STUDENT, TEACHER, deny, save_internal, trusted

PORTALS = {
    "teacher": {"role": TEACHER, "title": "教师工作台", "route": "rg-teacher-portal",
                "agent_route": "rg-teacher-agent"},
    "secretary": {"role": SECRETARY, "title": "秘书工作台", "route": "rg-secretary-portal",
                  "agent_route": "rg-secretary-agent"},
    "student": {"role": STUDENT, "title": "学生工作台", "route": "rg-student-portal",
                "agent_route": "rg-student-agent"},
}
NAVIGATION = {
    "topics": {"label": "课题与进展", "route": "List/RG Topic", "roles": (TEACHER, STUDENT)},
    "appointments": {"label": "预约讨论", "route": "List/RG Appointment", "roles": (TEACHER, STUDENT)},
    "materials": {"label": "组会与材料", "route": "List/RG Meeting Report", "roles": ROLES},
    "surveys": {"label": "投票与问卷", "route": "List/RG Survey", "roles": ROLES},
    "meals": {"label": "工作餐", "route": "rg-meals", "roles": ROLES},
    "manuscripts": {"label": "论文修改", "route": "rg-manuscripts", "roles": (TEACHER, STUDENT)},
    "accounts": {"label": "我的账号", "route": "rg-accounts", "roles": ROLES},
    "assistant": {"label": "我的 AI 助手", "route": "rg-ai-assistant", "roles": ROLES},
}
SETTINGS_NAME = "group-portal"


def _guard(*allowed):
    global_lock()
    assigned = set(ROLES) if frappe.session.user == "Administrator" else current_roles()
    if not assigned.intersection(allowed or ROLES):
        deny()
    return assigned


def _write(allowed):
    from research_group.ai.api import browser_write
    browser_write(allowed)


def _plain(value, label, limit, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        frappe.throw(f"{label}须为有效文本，最多 {limit} 字。")
    value = value.strip()
    if re.search(r"<[^>]*>|(?:javascript|vbscript|data)\s*:", value, re.I):
        frappe.throw(f"{label}仅支持纯文本，不支持 HTML 或可执行链接。")
    return value


def _json(value, expected):
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        frappe.throw("请求格式无效。")
    if not isinstance(result, expected):
        frappe.throw("请求格式无效。")
    return result


def _navigation(value):
    value = _json(value, list)
    if len(value) != len(NAVIGATION):
        frappe.throw("请保留全部批准栏目，可调整名称、顺序和显示状态。")
    found, result = set(), []
    for row in value:
        if not isinstance(row, dict) or set(row) - {"id", "label", "visible"}:
            frappe.throw("栏目只允许修改名称、顺序和显示状态。")
        key = row.get("id")
        if key not in NAVIGATION or key in found:
            frappe.throw("只允许使用批准的站内栏目。")
        if row.get("visible") not in (True, False, 0, 1):
            frappe.throw("栏目显示状态无效。")
        found.add(key)
        result.append({"id": key, "label": _plain(row.get("label"), "栏目名称", 40, True),
                       "visible": bool(row.get("visible"))})
    return result


def default_navigation():
    return [{"id": key, "label": value["label"], "visible": True} for key, value in NAVIGATION.items()]


def _settings(lock=False):
    # Current read is required even for the singleton's existence. A waiting
    # transaction can have an older REPEATABLE READ snapshot with no settings.
    rows = frappe.db.sql("SELECT name FROM `tabRG Portal Settings` WHERE name=%s FOR UPDATE", (SETTINGS_NAME,))
    if not rows:
        return {"name": SETTINGS_NAME, "introduction": "", "navigation_json": json.dumps(default_navigation()),
                "revision": 0, "edited_by": None, "edited_at": None, "previous_json": ""}
    return frappe.get_doc("RG Portal Settings", SETTINGS_NAME, for_update=True).as_dict()


class PortalDocument(Document):
    def validate(self):
        if not trusted(self):
            deny()

    def on_trash(self):
        deny()

    def notify_update(self):
        # Draft announcements and research-direction identities have no native
        # subscribers. Dedicated pages refresh their authorized API results.
        return


class RGPortalSettings(PortalDocument):
    def autoname(self):
        self.name = SETTINGS_NAME

    def validate(self):
        super().validate()
        self.introduction = _plain(self.introduction or "", "首页介绍", 12000)
        self.navigation_json = json.dumps(_navigation(self.navigation_json), ensure_ascii=False)
        if self.name != SETTINGS_NAME:
            deny()


class RGAnnouncement(PortalDocument):
    def validate(self):
        super().validate()
        self.title = _plain(self.title, "公告标题", 180, True)
        self.body = _plain(self.body, "公告正文", 12000, True)
        if self.audience not in {"all", *PORTALS} or self.status not in {"draft", "published", "offline"}:
            frappe.throw("公告状态或可见范围无效。")


class RGResearchDirection(PortalDocument):
    def validate(self):
        super().validate()
        self.title = _plain(self.title, "方向名称", 180, True)
        self.goal = _plain(self.goal or "", "方向目标", 12000)
        if self.priority not in {"高", "中", "低"}:
            frappe.throw("优先级无效。")


class RGDirectionTopic(Document):
    def validate(self):
        deny()


@frappe.whitelist()
def bootstrap(portal=None):
    assigned = _guard()
    available = [{"id": key, "title": item["title"], "route": item["route"]}
                 for key, item in PORTALS.items() if item["role"] in assigned]
    if portal is not None and (portal not in PORTALS or PORTALS[portal]["role"] not in assigned):
        deny()
    current = portal or available[0]["id"]
    current_role = PORTALS[current]["role"]
    settings = _settings()
    navigation = []
    for row in _navigation(settings["navigation_json"]):
        target = NAVIGATION[row["id"]]
        if row["visible"] and current_role in target["roles"]:
            route = PORTALS[current]["agent_route"] if row["id"] == "assistant" else target["route"]
            navigation.append({**row, "route": route})
    audiences = ["all", current]
    placeholders = ",".join(["%s"] * len(audiences))
    announcements = frappe.db.sql("SELECT name,title,body,published_at,publisher FROM `tabRG Announcement` "
        f"WHERE status='published' AND audience IN ({placeholders}) ORDER BY published_at DESC,name LIMIT 20 FOR UPDATE",
        tuple(audiences), as_dict=True)
    return {"user": frappe.session.user, "portal": current, "portals": available,
            "introduction": settings["introduction"], "navigation": navigation, "announcements": announcements,
            "can_message": current_role in (TEACHER, SECRETARY),
            "can_maintain_home": current_role == SECRETARY,
            "can_manage_directions": current_role == TEACHER}


@frappe.whitelist()
def home_settings():
    _guard(SECRETARY)
    row = _settings()
    return {"introduction": row["introduction"], "navigation": _navigation(row["navigation_json"]),
            "revision": row["revision"], "edited_by": row["edited_by"], "edited_at": row["edited_at"],
            "can_restore": bool(row["previous_json"]),
            "announcements": frappe.db.sql("SELECT name,title,body,status,audience,revision,published_at "
                "FROM `tabRG Announcement` ORDER BY modified DESC,name LIMIT 100 FOR UPDATE", as_dict=True)}


def _revision(expected, actual):
    if isinstance(expected, bool) or str(expected) != str(cint(actual)):
        frappe.throw("记录已更新，请刷新后再提交。", frappe.TimestampMismatchError)


@frappe.whitelist(methods=["POST"])
def save_home(introduction, navigation, expected_revision):
    _write({"introduction", "navigation", "expected_revision"})
    _guard(SECRETARY)
    old = _settings(lock=True)
    _revision(expected_revision, old["revision"])
    intro = _plain(introduction, "首页介绍", 12000)
    nav = _navigation(navigation)
    doc = frappe.get_doc("RG Portal Settings", SETTINGS_NAME, for_update=True) if old["revision"] else frappe.get_doc({
        "doctype": "RG Portal Settings", "name": SETTINGS_NAME})
    doc.update({"introduction": intro, "navigation_json": json.dumps(nav, ensure_ascii=False),
                "revision": cint(old["revision"]) + 1, "edited_by": frappe.session.user,
                "edited_at": now_datetime(), "previous_json": json.dumps({
                    "introduction": old["introduction"], "navigation": _navigation(old["navigation_json"])},
                    ensure_ascii=False)})
    save_internal(doc, new=not old["revision"])
    return home_settings()


@frappe.whitelist(methods=["POST"])
def restore_home(expected_revision):
    _write({"expected_revision"})
    _guard(SECRETARY)
    old = _settings(lock=True)
    _revision(expected_revision, old["revision"])
    if not old["previous_json"]:
        frappe.throw("没有可恢复的上一版。")
    previous = _json(old["previous_json"], dict)
    return save_home(previous["introduction"], previous["navigation"], expected_revision)


@frappe.whitelist(methods=["POST"])
def save_announcement(title, body, audience="all", name=None, expected_revision=0, status="draft"):
    _write({"title", "body", "audience", "name", "expected_revision", "status"})
    _guard(SECRETARY)
    if name:
        doc = frappe.get_doc("RG Announcement", name, for_update=True)
        _revision(expected_revision, doc.revision)
    else:
        _revision(expected_revision, 0)
        doc = frappe.get_doc({"doctype": "RG Announcement", "revision": 0})
    if status not in {"draft", "published", "offline"}:
        frappe.throw("公告状态无效。")
    if doc.get("status") == "published" and status == "published" and any(
            doc.get(key) != value for key, value in (("title", title), ("body", body), ("audience", audience))):
        frappe.throw("已发布公告请先下线，再修改和重新发布。")
    old_status = doc.get("status")
    doc.update({"title": title, "body": body, "audience": audience, "status": status,
                "revision": cint(doc.revision) + 1, "publisher": frappe.session.user})
    if status == "published" and old_status != "published":
        doc.published_at = now_datetime()
    save_internal(doc, new=doc.is_new())
    return {key: doc.get(key) for key in ("name", "title", "body", "audience", "status", "revision", "published_at")}


@frappe.whitelist()
def teacher_students():
    _guard(TEACHER)
    rows = frappe.db.sql(
        "SELECT m.user, m.display_name, m.active, u.enabled FROM `tabRG Member` m "
        "JOIN `tabUser` u ON u.name=m.user WHERE EXISTS (SELECT 1 FROM `tabHas Role` r "
        "WHERE r.parent=m.user AND r.parenttype='User' AND r.role=%s) "
        "ORDER BY m.active DESC, m.display_name, m.user LIMIT 501", (STUDENT,), as_dict=True)
    return {"students": rows[:500], "more": len(rows) > 500}


@frappe.whitelist()
def student_detail(student):
    _guard(TEACHER)
    from research_group.reports import teacher_overview
    if not frappe.db.exists("RG Member", {"user": student}):
        deny()
    member = frappe.db.get_value("RG Member", {"user": student}, ["user", "display_name", "active"], as_dict=True)
    topics = teacher_overview({"student": student})[1]
    progress = frappe.get_all("RG Progress", filters={"student": student}, fields=[
        "name", "topic", "recorded_at", "summary", "blockers", "next_milestone"],
        order_by="recorded_at desc, creation desc, name desc", limit_page_length=21)
    discussions = frappe.get_all("RG Discussion Record", filters={"student": student}, fields=[
        "name", "topic", "discussed_at", "recorded_at", "teacher", "conclusion", "teacher_feedback", "actions"],
        order_by="discussed_at desc, creation desc, name desc", limit_page_length=21)
    return {"student": member, "topics": topics, "progress": progress[:20], "discussions": discussions[:20],
            "more_progress": len(progress) > 20, "more_discussions": len(discussions) > 20}


@frappe.whitelist()
def directions():
    _guard(TEACHER)
    from research_group.reports import teacher_overview
    topics = teacher_overview()[1]
    configured = frappe.get_all("RG Research Direction", fields=[
        "name", "title", "goal", "priority", "revision", "edited_at", "edited_by"], order_by="title", limit_page_length=0)
    groups = {row.name: {**row, "topics": []} for row in configured}
    linked = {}
    for row in frappe.get_all("RG Direction Topic", fields=["parent", "topic"], limit_page_length=0):
        if row.parent in groups:
            linked.setdefault(row.topic, []).append(row.parent)
    configured_titles = {row.title for row in configured}
    for topic in topics:
        # A saved direction owns its explicit association list, including an
        # empty list. Legacy topic text only aggregates unconfigured directions.
        keys = linked.get(topic["topic"], [])
        if not keys and topic["direction"] not in configured_titles:
            keys = ["existing:" + topic["direction"]]
        for key in keys:
            if key not in groups:
                groups[key] = {"name": None, "title": topic["direction"], "goal": "", "priority": "中",
                               "revision": 0, "topics": []}
            groups[key]["topics"].append(topic)
    return {"directions": list(groups.values()), "available_topics": [
        {"name": row["topic"], "title": row["title"], "student": row["student"]} for row in topics]}


@frappe.whitelist(methods=["POST"])
def save_direction(title, goal="", priority="中", topics=None, name=None, expected_revision=0):
    _write({"title", "goal", "priority", "topics", "name", "expected_revision"})
    _guard(TEACHER)
    selected = _json(topics or [], list)
    if (len(selected) > 500 or any(not isinstance(item, str) for item in selected)
            or len(set(selected)) != len(selected)):
        frappe.throw("关联课题须为不重复的有效课题，最多 500 项。")
    for topic in selected:
        if not frappe.db.exists("RG Topic", topic):
            deny()
    if name:
        doc = frappe.get_doc("RG Research Direction", name, for_update=True)
        _revision(expected_revision, doc.revision)
    else:
        _revision(expected_revision, 0)
        doc = frappe.get_doc({"doctype": "RG Research Direction", "revision": 0})
    doc.update({"title": title, "goal": goal, "priority": priority, "revision": cint(doc.revision) + 1,
                "edited_by": frappe.session.user, "edited_at": now_datetime(), "topics": []})
    for topic in selected:
        doc.append("topics", {"topic": topic})
    save_internal(doc, new=doc.is_new())
    return {"name": doc.name, "revision": doc.revision}
