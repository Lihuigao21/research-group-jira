import frappe

from research_group.policy import SECRETARY, STUDENT, TEACHER, require_role


def col(fieldname, label, fieldtype="Data", options=None, width=180):
    result = dict(fieldname=fieldname, label=label, fieldtype=fieldtype, width=width)
    if options:
        result["options"] = options
    return result


def teacher_overview(filters=None):
    from research_group.portals import _guard
    _guard(TEACHER)
    if isinstance(filters, str):
        filters = frappe.parse_json(filters)
    if filters is not None and (not isinstance(filters, dict) or set(filters) - {"student"}):
        frappe.throw("教师总览只支持按学生筛选。")
    student = (filters or {}).get("student")
    if student and (not isinstance(student, str) or not frappe.db.exists("RG Member", {"user": student})):
        frappe.throw("请选择课题组成员。")
    topics = frappe.get_list("RG Topic", fields=[
        "name", "title", "student", "direction", "priority", "status", "next_milestone", "target_date",
    ], filters={"student": student} if student else {}, order_by="modified desc", limit_page_length=0)
    # Teachers are authorized for every private topic. Rank inside each topic
    # in SQL so a busy topic cannot crowd another topic out of a global limit.
    progress = frappe.db.sql(
        "SELECT name, topic, summary, blockers, next_milestone, recorded_at FROM ("
        "SELECT name, topic, summary, blockers, next_milestone, recorded_at, "
        "ROW_NUMBER() OVER (PARTITION BY topic ORDER BY recorded_at DESC, creation DESC, name DESC) AS rg_rank "
        "FROM `tabRG Progress`) latest WHERE rg_rank=1", as_dict=True,
    )
    discussions = frappe.db.sql(
        "SELECT name, topic, conclusion, teacher_feedback, actions, discussed_at FROM ("
        "SELECT name, topic, conclusion, teacher_feedback, actions, discussed_at, "
        "ROW_NUMBER() OVER (PARTITION BY topic ORDER BY discussed_at DESC, creation DESC, name DESC) AS rg_rank "
        "FROM `tabRG Discussion Record`) latest WHERE rg_rank=1", as_dict=True,
    )
    latest_progress = {row.topic: row for row in progress}
    latest_discussion = {row.topic: row for row in discussions}
    data = []
    for topic in topics:
        p, d = latest_progress.get(topic.name, {}), latest_discussion.get(topic.name, {})
        data.append(dict(
            topic=topic.name, title=topic.title, student=topic.student, direction=topic.direction,
            priority=topic.priority, status=topic.status, summary=p.get("summary"),
            last_progress_at=p.get("recorded_at"), blockers=p.get("blockers"),
            next_milestone=p.get("next_milestone") or topic.next_milestone,
            target_date=topic.target_date, discussion=d.get("name"), last_discussed_at=d.get("discussed_at"),
            conclusion=d.get("conclusion"), teacher_feedback=d.get("teacher_feedback"), actions=d.get("actions"),
        ))
    return [
        col("topic", "课题记录", "Link", "RG Topic"), col("title", "当前课题", width=240),
        col("student", "学生", "Link", "User"), col("direction", "研究方向", width=240),
        col("priority", "优先级", width=90), col("status", "课题状态", width=100),
        col("summary", "最近进展", width=280), col("last_progress_at", "最近进展时间", "Datetime"),
        col("blockers", "当前阻碍", width=240), col("next_milestone", "下一里程碑", width=260),
        col("target_date", "里程碑日期", "Date"), col("discussion", "最近讨论", "Link", "RG Discussion Record"),
        col("last_discussed_at", "最近讨论时间", "Datetime"), col("conclusion", "讨论结论", width=240),
        col("teacher_feedback", "教师意见", width=240), col("actions", "后续行动", width=240),
    ], data


def student_timeline(filters=None):
    require_role(TEACHER, STUDENT)
    data = []
    for row in frappe.get_list("RG Progress", fields=[
        "name", "topic", "student", "recorded_at", "summary", "blockers", "next_milestone",
    ], limit_page_length=0):
        data.append(dict(time=row.recorded_at, event="追加进展", topic=row.topic, student=row.student,
                         detail=row.summary, blockers=row.blockers, actions=row.next_milestone,
                         reference_type="RG Progress", reference_name=row.name))
    for row in frappe.get_list("RG Appointment", fields=[
        "name", "topic", "student", "creation", "agenda", "status", "decision_note",
    ], limit_page_length=0):
        data.append(dict(time=row.creation, event="讨论申请 · " + row.status, topic=row.topic,
                         student=row.student, detail=row.agenda, actions=row.decision_note,
                         reference_type="RG Appointment", reference_name=row.name))
    for row in frappe.get_list("RG Discussion Record", fields=[
        "name", "topic", "student", "recorded_at", "conclusion", "teacher_feedback", "actions",
    ], limit_page_length=0):
        data.append(dict(time=row.recorded_at, event="讨论归档", topic=row.topic, student=row.student,
                         detail=row.conclusion, feedback=row.teacher_feedback, actions=row.actions,
                         reference_type="RG Discussion Record", reference_name=row.name))
    data.sort(key=lambda r: str(r["time"]), reverse=True)
    return [
        col("time", "时间", "Datetime"), col("event", "事件"), col("student", "学生", "Link", "User"),
        col("topic", "课题", "Link", "RG Topic"), col("detail", "进展或结论", width=320),
        col("blockers", "阻碍", width=220), col("feedback", "教师意见", width=240),
        col("actions", "下一步与后续行动", width=280),
        col("reference_type", "记录类型"), col("reference_name", "原始记录", "Dynamic Link", "reference_type"),
    ], data


def secretary_schedule(filters=None):
    require_role(TEACHER, SECRETARY, STUDENT)
    meetings = frappe.get_list("RG Group Meeting", fields=[
        "name", "title", "start_at", "end_at", "location", "status", "deadline",
    ], order_by="start_at desc", limit_page_length=0)
    reports = frappe.get_list("RG Meeting Report", fields=[
        "name", "meeting", "student", "topic", "latest_version", "last_submitted_at", "late",
    ], filters={"meeting": ["in", [meeting.name for meeting in meetings]]},
        limit_page_length=0) if meetings else []
    by_meeting = {}
    for report in reports:
        by_meeting.setdefault(report.meeting, []).append(report)
    data = []
    for meeting in meetings:
        for report in by_meeting.get(meeting.name, [{}]):
            data.append(dict(meeting=meeting.name, title=meeting.title, start_at=meeting.start_at,
                             end_at=meeting.end_at, location=meeting.location, status=meeting.status,
                             deadline=meeting.deadline, report=report.get("name"), student=report.get("student"),
                             topic=report.get("topic") or "待提交", versions=report.get("latest_version") or 0,
                             submitted_at=report.get("last_submitted_at"), late=report.get("late") or 0))
    return [
        col("meeting", "组会", "Link", "RG Group Meeting"), col("title", "组会名称", width=240),
        col("start_at", "开始时间", "Datetime"), col("end_at", "结束时间", "Datetime"),
        col("location", "地点"), col("status", "发布状态", width=100), col("deadline", "材料截止", "Datetime"),
        col("student", "报告学生", "Link", "User"), col("topic", "报告主题", width=280),
        col("versions", "版本数", "Int", width=80), col("submitted_at", "最近提交", "Datetime"),
        col("late", "迟交", "Check", width=70), col("report", "查看材料", "Link", "RG Meeting Report"),
    ], data

