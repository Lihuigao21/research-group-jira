"""Document invariants also run for native REST/Desk CRUD."""
import frappe
from frappe.model.document import Document
from frappe.utils import add_days, get_datetime, now_datetime

from research_group.policy import (
    APPROVED, ARCHIVED, CANCELLED, DRAFT, PENDING, PUBLISHED, REJECTED,
    SECRETARY, STUDENT, TEACHER, deny, require_business_member, require_business_role, require_member, require_role, roles, trusted,
)


def immutable(doc):
    if not doc.is_new():
        frappe.throw("历史记录只能追加，不能覆盖或删除。")


def times(doc):
    if get_datetime(doc.end_at) <= get_datetime(doc.start_at):
        frappe.throw("结束时间必须晚于开始时间。")


def topic_student(doc):
    topic = frappe.db.get_value("RG Topic", doc.topic, ["student", "status"], as_dict=True)
    if not topic:
        frappe.throw("课题不存在。")
    if frappe.session.user != "Administrator" and STUDENT in roles() and TEACHER not in roles():
        if topic.student != frappe.session.user:
            deny()
    return topic.student


class BusinessEventDocument(Document):
    def before_insert(self):
        from research_group.ai.security import native_identity_lock
        native_identity_lock(self)

    def check_if_latest(self):
        from research_group.ai.security import native_identity_lock
        native_identity_lock(self)
        return super().check_if_latest()


class RGMember(Document):
    def check_if_latest(self):
        from research_group.ai.security import native_identity_lock
        native_identity_lock(self)
        return super().check_if_latest()

    def validate(self):
        if not trusted(self) and frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
            deny()


class RGTopic(Document):
    def validate(self):
        require_role(TEACHER)
        require_member(self.student, STUDENT)
        if not self.is_new() and self.has_value_changed("student"):
            frappe.throw("已有课题不能更换所属学生，请另建课题。")


class RGProgress(Document):
    def before_validate(self):
        self.student = topic_student(self)
        if self.is_new():
            self.recorded_at = now_datetime()

    def validate(self):
        immutable(self)
        require_role(STUDENT)
        if self.student != frappe.session.user and frappe.session.user != "Administrator":
            deny()
        if not self.summary or not self.summary.strip():
            frappe.throw("请填写本次进展。")


class RGAppointment(BusinessEventDocument):
    def before_validate(self):
        if self.is_new():
            self.student = topic_student(self)

    def validate(self):
        times(self)
        if self.is_new():
            require_business_role(STUDENT)
            if self.student != frappe.session.user and frappe.session.user != "Administrator":
                deny()
            require_business_member(self.teacher, TEACHER)
            if self.status != PENDING or self.decision_note or self.decided_at or self.archived_at:
                frappe.throw("新申请只能处于待审批状态。")
            return
        if not trusted(self):
            frappe.throw("请使用批准、拒绝、取消或归档操作。")
        old = self.get_doc_before_save()
        for field in ("student", "teacher", "topic", "start_at", "end_at", "agenda"):
            if self.get(field) != old.get(field):
                frappe.throw("申请内容不可覆盖；请取消后重新申请。")
        transitions = {
            PENDING: {APPROVED, REJECTED, CANCELLED},
            APPROVED: {CANCELLED, ARCHIVED},
        }
        if self.status not in transitions.get(old.status, set()):
            frappe.throw("该讨论状态不允许此操作。")


class RGDiscussionRecord(Document):
    def validate(self):
        if not trusted(self):
            frappe.throw("讨论记录须由预约的归档操作创建。")
        if not self.is_new():
            old = self.get_doc_before_save()
            for field in self.meta.fields:
                if field.fieldname != "teacher_feedback" and self.has_value_changed(field.fieldname):
                    frappe.throw("归档内容不可覆盖；教师只能补充教师意见。")
            if self.teacher != frappe.session.user or TEACHER not in roles():
                deny()
            return
        if frappe.db.get_value("RG Appointment", self.appointment, "status") != APPROVED:
            frappe.throw("只有已批准的讨论才能归档。")


class RGGroupMeeting(BusinessEventDocument):
    def before_validate(self):
        if self.is_new():
            self.publisher = frappe.session.user
            if not self.deadline and self.start_at:
                self.deadline = get_datetime(add_days(get_datetime(self.start_at).date(), -1)).replace(
                    hour=23, minute=59, second=0
                )

    def validate(self):
        require_business_role(TEACHER, SECRETARY)
        times(self)
        if not self.is_new() and self.publisher != self.get_doc_before_save().publisher:
            frappe.throw("发布者不可更换。")
        if self.publisher != frappe.session.user and frappe.session.user != "Administrator":
            deny()
        if self.is_new() and self.status != DRAFT:
            frappe.throw("请先保存草稿并指定报告人，再发布组会。")
        if not self.is_new() and self.has_value_changed("status") and not trusted(self):
            frappe.throw("请使用发布操作。")
        if not self.is_new() and self.get_doc_before_save().status == PUBLISHED:
            if self.status != PUBLISHED:
                frappe.throw("已发布组会不能退回草稿。")
        if not self.deadline:
            frappe.throw("材料截止时间不能为空。")
        if get_datetime(self.deadline) > get_datetime(self.start_at):
            frappe.throw("材料截止时间不能晚于组会开始时间。")


class RGMeetingReport(Document):
    def check_if_latest(self):
        if not self.is_new():
            # Native save locks this report in check_if_latest. Take the
            # immutable parent first, matching publish/assignment lock order.
            meeting = frappe.db.get_value(self.doctype, self.name, "meeting")
            if meeting:
                frappe.get_doc("RG Group Meeting", meeting, for_update=True)
        return super().check_if_latest()

    def validate(self):
        if trusted(self):
            return
        require_role(TEACHER, SECRETARY)
        if not self.is_new() and self.has_value_changed("meeting"):
            frappe.throw("报告所属组会不可更换。")
        # Native REST/Desk insert shares the publish/assignment lock. A locking
        # read sees the current status even after a REPEATABLE READ snapshot.
        meeting = frappe.get_doc("RG Group Meeting", self.meeting, for_update=True)
        if meeting.publisher != frappe.session.user and frappe.session.user != "Administrator":
            deny()
        if meeting.status != DRAFT:
            frappe.throw("发布后不能通过普通编辑更改报告人。")
        require_member(self.student, STUDENT)
        if frappe.db.sql(
            "SELECT name FROM `tabRG Meeting Report` WHERE meeting=%s AND student=%s AND name!=%s LIMIT 1 FOR UPDATE",
            (self.meeting, self.student, self.name or ""),
        ):
            frappe.throw("该学生已经在报告名单中。")
        if self.is_new() and (self.topic or self.latest_version or self.last_submitted_at or self.late):
            frappe.throw("主题和提交信息须由报告人提交。")
        if not self.is_new():
            for f in ("topic", "latest_version", "last_submitted_at", "late"):
                if self.has_value_changed(f):
                    frappe.throw("报告记录不可覆盖，请通过提交材料操作追加。")


class RGMaterialVersion(Document):
    def validate(self):
        if not trusted(self):
            frappe.throw("材料版本只能通过提交操作创建。")
        immutable(self)

