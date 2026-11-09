"""Opt-in fake data only. Passwords come from an ignored local file."""
import json
from pathlib import Path

import frappe
from frappe.utils import add_days, nowdate

from research_group.policy import SECRETARY, STUDENT, TEACHER

ACCOUNTS = {
    "teacher.demo@example.invalid": ("演示教师（虚构）", [TEACHER]),
    "student.one.demo@example.invalid": ("演示学生甲（虚构）", [STUDENT]),
    "student.two.demo@example.invalid": ("演示学生乙（虚构）", [STUDENT]),
    "secretary.demo@example.invalid": ("演示秘书兼学生（虚构）", [SECRETARY, STUDENT]),
}


def seed(credentials_path):
    if frappe.session.user != "Administrator":
        frappe.throw("仅管理员可初始化虚构演示数据。", frappe.PermissionError)
    if not frappe.conf.get("rg_demo"):
        frappe.throw("只有显式设置 rg_demo=1 的演示站点可导入。")
    credentials = json.loads(Path(credentials_path).read_text(encoding="utf-8"))
    for email, (display_name, assigned_roles) in ACCOUNTS.items():
        if not credentials.get(email):
            frappe.throw("本地凭据文件缺少演示账号口令。")
        if not frappe.db.exists("User", email):
            frappe.get_doc({
                "doctype": "User", "email": email, "first_name": display_name, "enabled": 1,
                "user_type": "System User", "language": "zh", "send_welcome_email": 0,
                "new_password": credentials[email], "roles": [{"role": role} for role in assigned_roles],
            }).insert(ignore_permissions=True)
        if not frappe.db.exists("RG Member", email):
            frappe.get_doc({"doctype": "RG Member", "user": email, "display_name": display_name,
                            "active": 1, "is_demo": 1}).insert(ignore_permissions=True)
    topic_names = {}
    for email, title in [
        ("student.one.demo@example.invalid", "虚构演示：开放量子体系方法比较"),
        ("student.two.demo@example.invalid", "虚构演示：分子动力学误差分析"),
        ("secretary.demo@example.invalid", "虚构演示：数值收敛检查"),
    ]:
        existing = frappe.db.get_value("RG Topic", {"student": email, "title": title}, "name")
        if not existing:
            existing = frappe.get_doc({"doctype": "RG Topic", "title": title, "student": email,
                "direction": "仅用于演示的虚构研究方向", "priority": "中", "status": "进行中",
                "next_milestone": "完成虚构对照样例", "target_date": add_days(nowdate(), 14),
            }).insert(ignore_permissions=True).name
        topic_names[email] = existing
    for email, topic in topic_names.items():
        if not frappe.db.exists("RG Progress", {"topic": topic}):
            frappe.set_user(email)
            frappe.get_doc({"doctype": "RG Progress", "topic": topic, "progress_date": nowdate(),
                            "summary": "虚构演示：已完成初步对照。", "blockers": "虚构演示：等待参数确认。",
                            "next_milestone": "虚构演示：补充收敛性分析。"}).insert()
    frappe.set_user("Administrator")
    if not frappe.db.exists("RG Group Meeting", {"title": "虚构演示组会（所有姓名与内容均为虚构）"}):
        from research_group.services import assign_reporter, publish_meeting
        frappe.set_user("secretary.demo@example.invalid")
        date = str(add_days(nowdate(), 3))
        meeting = frappe.get_doc({"doctype": "RG Group Meeting", "title": "虚构演示组会（所有姓名与内容均为虚构）",
                                 "start_at": date + " 14:00:00", "end_at": date + " 16:00:00",
                                 "location": "虚构会议室", "notes": "演示数据，不代表真实成员与工作安排。"}).insert()
        assign_reporter(meeting.name, "student.one.demo@example.invalid")
        assign_reporter(meeting.name, "student.two.demo@example.invalid")
        publish_meeting(meeting.name)
    frappe.set_user("Administrator")
    frappe.db.commit()
    return {"demo": True, "accounts": list(ACCOUNTS), "passwords": "仅见被 Git 忽略的本地凭据文件"}

