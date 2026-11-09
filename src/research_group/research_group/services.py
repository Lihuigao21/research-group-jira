"""Whitelisted actions enforce authorization and atomic state transitions."""
import hashlib
from pathlib import Path

import frappe
from frappe.utils import get_datetime, now_datetime

from research_group.files import max_bytes, read_stored_file_bytes, save_managed_file, validate_material
from research_group.policy import (
    APPROVED, ARCHIVED, CANCELLED, DRAFT, PENDING, PUBLISHED, REJECTED,
    ROLES, SECRETARY, STUDENT, TEACHER, deny, require_business_role, require_member, require_role,
    roles, save_internal, secure_get,
)


def _lock(doctype, name):
    # Constants-only table names; identifiers are not caller controlled.
    if doctype not in {"User", "RG Appointment", "RG Meeting Report", "RG Group Meeting"}:
        raise ValueError(doctype)
    row = frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE", (name,))
    if not row:
        deny()


def _locked_appointment(name):
    require_business_role(*ROLES)
    initial = secure_get("RG Appointment", name)
    # Lock the teacher first on *every* transition. Current/locking reads after this
    # avoid a stale REPEATABLE READ snapshot and serialize independent connections.
    _lock("User", initial.teacher)
    _lock("RG Appointment", name)
    require_business_role(*ROLES)
    return frappe.get_doc("RG Appointment", name, for_update=True)


@frappe.whitelist(methods=["POST"])
def decide_appointment(name, decision, note=None):
    require_business_role(TEACHER)
    doc = _locked_appointment(name)
    if doc.teacher != frappe.session.user:
        deny()
    if doc.status != PENDING:
        frappe.throw("该申请已经处理。")
    if decision not in {APPROVED, REJECTED}:
        frappe.throw("无效的审批结果。")
    if decision == APPROVED:
        overlap = frappe.db.sql(
            "SELECT name FROM `tabRG Appointment` WHERE teacher=%s AND status=%s "
            "AND name<>%s AND start_at<%s AND end_at>%s FOR UPDATE",
            (doc.teacher, APPROVED, doc.name, doc.end_at, doc.start_at),
        )
        if overlap:
            frappe.throw("与该教师已批准的讨论时间重叠；本申请仍保留待审批。")
    doc.status, doc.decision_note, doc.decided_at = decision, note or "", now_datetime()
    save_internal(doc)
    return doc.as_dict()


@frappe.whitelist(methods=["POST"])
def cancel_appointment(name, note=None):
    doc = _locked_appointment(name)
    r = require_business_role(*ROLES)
    if not ((doc.student == frappe.session.user and STUDENT in r)
            or (doc.teacher == frappe.session.user and TEACHER in r)):
        deny()
    if doc.status not in {PENDING, APPROVED}:
        frappe.throw("只有尚未完成的讨论可以取消。")
    doc.status, doc.decision_note, doc.decided_at = CANCELLED, note or "", now_datetime()
    save_internal(doc)
    return doc.as_dict()


@frappe.whitelist(methods=["POST"])
def archive_discussion(name, conclusion, actions, teacher_feedback=None):
    doc = _locked_appointment(name)
    r = require_business_role(*ROLES)
    is_teacher = doc.teacher == frappe.session.user and TEACHER in r
    if not (is_teacher or (doc.student == frappe.session.user and STUDENT in r)):
        deny()
    if doc.status != APPROVED:
        frappe.throw("只有已批准的讨论才能归档；取消和拒绝的讨论不可归档。")
    if not conclusion or not conclusion.strip() or not actions or not actions.strip():
        frappe.throw("请填写讨论结论与后续行动。")
    if teacher_feedback and not is_teacher:
        deny()
    record = frappe.get_doc({
        "doctype": "RG Discussion Record", "appointment": doc.name, "topic": doc.topic,
        "student": doc.student, "teacher": doc.teacher, "discussed_at": doc.start_at,
        "conclusion": conclusion, "actions": actions, "teacher_feedback": teacher_feedback or "",
        "recorded_at": now_datetime(), "recorded_by": frappe.session.user,
    })
    save_internal(record, new=True)
    doc.status, doc.archived_at = ARCHIVED, now_datetime()
    save_internal(doc)
    return record.as_dict()


@frappe.whitelist(methods=["POST"])
def publish_meeting(name):
    require_business_role(TEACHER, SECRETARY)
    meeting = secure_get("RG Group Meeting", name, "write")
    _lock("RG Group Meeting", name)
    meeting = frappe.get_doc("RG Group Meeting", name, for_update=True)
    if meeting.publisher != frappe.session.user:
        deny()
    if meeting.status != DRAFT:
        frappe.throw("该组会已经发布。")
    if not frappe.db.sql("SELECT name FROM `tabRG Meeting Report` WHERE meeting=%s LIMIT 1 FOR UPDATE", (name,)):
        frappe.throw("发布前请至少指定一名报告学生。")
    meeting.status = PUBLISHED
    save_internal(meeting)
    return meeting.as_dict()


@frappe.whitelist(methods=["POST"])
def update_teacher_feedback(name, feedback):
    require_role(TEACHER)
    doc = secure_get("RG Discussion Record", name)
    if doc.teacher != frappe.session.user:
        deny()
    if not feedback or not feedback.strip():
        frappe.throw("请填写教师意见。")
    doc.teacher_feedback = feedback.strip()
    save_internal(doc)
    return doc.as_dict()


@frappe.whitelist(methods=["POST"])
def assign_reporter(meeting, student):
    require_role(TEACHER, SECRETARY)
    doc = secure_get("RG Group Meeting", meeting, "write")
    _lock("RG Group Meeting", meeting)
    doc = frappe.get_doc("RG Group Meeting", meeting, for_update=True)
    if doc.publisher != frappe.session.user or doc.status != DRAFT:
        deny()
    require_member(student, STUDENT)
    if frappe.db.sql(
        "SELECT name FROM `tabRG Meeting Report` WHERE meeting=%s AND student=%s LIMIT 1 FOR UPDATE",
        (meeting, student),
    ):
        frappe.throw("该学生已经在报告名单中。")
    return frappe.get_doc({
        "doctype": "RG Meeting Report", "meeting": meeting, "student": student,
    }).insert().as_dict()


def _upload():
    if not getattr(frappe.local, "request", None) or "file" not in frappe.request.files:
        frappe.throw("请选择要上传的文件。")
    stream = frappe.request.files["file"]
    content = stream.stream.read(max_bytes() + 1)
    validate_material(stream.filename, content)
    return stream.filename, content


def _cleanup_created_material_files(created_files):
    """Savepoint rollback does not execute native after_rollback callbacks."""
    for created in created_files:
        file = created["file"]
        file.validate_file_path()
        path = Path(created["path"]).resolve()
        if path != Path(file.get_full_path()).resolve():
            raise RuntimeError("Managed file path changed during rollback")
        # A current read sees committed or transaction-local dedup references;
        # never remove an existing/shared blob just because this insert failed.
        references = frappe.db.sql(
            "SELECT name FROM `tabFile` WHERE file_url=%s OR content_hash=%s LIMIT 1 FOR UPDATE",
            (created["file_url"], created["content_hash"]),
        )
        if not references:
            path.unlink(missing_ok=True)
        # The failed insert's native callback can run on a later outer rollback.
        # It must not touch a blob subsequently created at the same path.
        for flag in ("new_file", "original_content", "original_path"):
            file.flags.pop(flag, None)


def submit_material(report, topic, filename, content):
    """Internal byte entry for tests and the multipart endpoint; never whitelisted."""
    require_role(STUDENT)
    doc = secure_get("RG Meeting Report", report)
    if doc.student != frappe.session.user:
        deny()
    validate_material(filename, content)  # no submitted row/file before validation
    if not topic or not topic.strip() or len(topic) > 200:
        frappe.throw("请填写不超过 200 字的报告主题。")
    _lock("RG Group Meeting", doc.meeting)
    _lock("RG Meeting Report", doc.name)
    doc = frappe.get_doc("RG Meeting Report", doc.name, for_update=True)
    meeting = frappe.get_doc("RG Group Meeting", doc.meeting, for_update=True)
    if meeting.status != PUBLISHED:
        frappe.throw("组会尚未发布。")
    # The row lock serializes reuploads and guarantees strictly increasing versions.
    number = int(doc.latest_version or 0) + 1
    submitted_at = now_datetime()
    version = frappe.get_doc({
        "doctype": "RG Material Version", "report": doc.name, "version": number,
        "report_topic": topic.strip(), "original_filename": filename,
        "submitted_by": frappe.session.user, "submitted_at": submitted_at,
        "late": int(submitted_at > get_datetime(meeting.deadline)),
        "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content),
    })
    # A savepoint also protects a caller that catches a failed internal operation.
    savepoint = "rg_material_" + frappe.generate_hash(length=8)
    frappe.db.savepoint(savepoint)
    created_files = []
    try:
        save_internal(version, new=True)
        file = save_managed_file(  # validate_material ran on these bytes at the top of this function
            filename, content, "RG Material Version", version.name, created_files=created_files,
            already_validated=True,
        )
        stored = read_stored_file_bytes(file, max_read_bytes=len(content))
        if len(stored) != version.size_bytes or hashlib.sha256(stored).hexdigest() != version.sha256:
            frappe.throw("保存后的材料内容校验失败，请联系管理员。")
        # This field is the only late-bound part of a new immutable version.
        frappe.db.set_value("RG Material Version", version.name, "material_file", file.name, update_modified=False)
        doc.topic, doc.latest_version = topic.strip(), number
        doc.last_submitted_at, doc.late = submitted_at, version.late
        save_internal(doc)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        _cleanup_created_material_files(created_files)
        raise
    return {"name": version.name, "version": number, "late": version.late, "file_url": file.unique_url}


@frappe.whitelist(methods=["POST"])
def upload_report_material(report, topic):
    # Authenticate/authorize before parsing the file and before leaking report data.
    require_role(STUDENT)
    doc = secure_get("RG Meeting Report", report)
    if doc.student != frappe.session.user:
        deny()
    filename, content = _upload()
    return submit_material(report, topic, filename, content)


@frappe.whitelist(methods=["POST"])
def upload_discussion_material(doctype, name):
    require_role(*ROLES)
    if doctype not in {"RG Appointment", "RG Discussion Record"}:
        deny()
    doc = secure_get(doctype, name)
    r = require_role(*ROLES)
    if not ((doc.student == frappe.session.user and STUDENT in r)
            or (doc.teacher == frappe.session.user and TEACHER in r)):
        deny()
    filename, content = _upload()  # validates these bytes
    file = save_managed_file(filename, content, doctype, name, already_validated=True)
    return {"name": file.name, "file_url": file.unique_url}


@frappe.whitelist()
def material_history(report):
    secure_get("RG Meeting Report", report)
    rows = frappe.get_list("RG Material Version", filters={"report": report}, fields=[
        "name", "version", "report_topic", "original_filename", "submitted_at", "late", "material_file",
    ], order_by="version desc", limit_page_length=0)
    for row in rows:
        file = frappe.get_doc("File", row.material_file)
        row.file_url = file.unique_url
    return rows


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def member_search(doctype, txt, searchfield, start, page_len, filters):
    require_role(*[TEACHER, STUDENT, SECRETARY])
    role = (filters or {}).get("role")
    if role not in {TEACHER, STUDENT, SECRETARY}:
        deny()
    return frappe.db.sql(
        "SELECT m.user, m.display_name FROM `tabRG Member` m "
        "JOIN `tabUser` u ON u.name=m.user JOIN `tabHas Role` h ON h.parent=m.user "
        "WHERE m.active=1 AND u.enabled=1 AND h.parenttype='User' AND h.role=%s "
        "AND (m.display_name LIKE %s OR m.user LIKE %s) ORDER BY m.display_name LIMIT %s OFFSET %s",
        (role, f"%{txt}%", f"%{txt}%", min(int(page_len), 50), max(int(start), 0)),
    )

