"""Named, member-only surveys. Parent locks serialize every response and close."""
import csv
import json
from io import StringIO

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from research_group.policy import (
    DRAFT, PUBLISHED, ROLES, SECRETARY, deny, require_role, save_internal, secure_get, trusted,
)

CLOSED = "已关闭"
SINGLE, MULTIPLE, TEXT = "单选", "多选", "简答"
MAX_QUESTIONS, MAX_OPTIONS, MAX_TEXT = 100, 100, 5000


def _text(value, label, maximum, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        frappe.throw(f"{label}须填写有效文本，最多 {maximum} 字。")
    return value.strip()


def _options(question, number=None):
    label = f"第 {number} 题的选项" if number is not None else "选项"
    raw = _text(question.get("options"), label, 25000)
    return [line.strip() for line in raw.splitlines() if line.strip()]


def validate_questions(doc, publishing=False):
    questions = doc.get("questions") or []
    if len(questions) > MAX_QUESTIONS or (publishing and not questions):
        frappe.throw(f"发布前请填写 1 至 {MAX_QUESTIONS} 道题目。")
    seen = set()
    for number, question in enumerate(questions, 1):
        _text(question.get("title"), f"第 {number} 题题目", 500, required=publishing)
        if question.get("question_type") not in {SINGLE, MULTIPLE, TEXT}:
            frappe.throw(f"第 {number} 题：题型须为单选、多选或简答。")
        if question.get("name") in seen:
            frappe.throw(f"第 {number} 题：题目标识不能重复。")
        if question.get("name"):
            seen.add(question.name)
        options = _options(question, number)
        if len(options) > MAX_OPTIONS or any(len(option) > 200 for option in options):
            frappe.throw(f"第 {number} 题：最多 {MAX_OPTIONS} 个选项，每个选项最多 200 字。")
        if publishing and question.question_type in {SINGLE, MULTIPLE}:
            if len(options) < 2 or len(options) != len(set(options)):
                frappe.throw(f"第 {number} 题：选择题须至少有两个非空选项，且选项不能重复。")
        if publishing and question.question_type == TEXT and options:
            frappe.throw(f"第 {number} 题：简答题不应填写选项。")


def _question_content(doc):
    return [
        (row.name, row.idx, row.title, row.question_type, int(row.required or 0), row.options or "")
        for row in (doc.get("questions") or [])
    ]


class RGSurvey(Document):
    def before_validate(self):
        require_role(SECRETARY)
        if not self.is_new():
            # Native save has already locked this parent in check_if_latest.
            _require_current_roles((SECRETARY,))
        if self.is_new():
            if self.publisher and self.publisher != frappe.session.user:
                deny()
            self.publisher = frappe.session.user
            if self.status != DRAFT or self.published_at or self.closed_at:
                frappe.throw("请先保存问卷草稿，再使用发布操作。")
        self.kind = "投票" if len(self.get("questions") or []) == 1 else "问卷"
        # A submitted child name must never steal a row from another parent.
        for question in self.get("questions") or []:
            if question.name:
                parent = frappe.db.get_value(
                    "RG Survey Question", question.name, ["parent", "parenttype", "parentfield"], as_dict=True,
                )
                if parent and (parent.parent != self.name or parent.parenttype != "RG Survey"
                               or parent.parentfield != "questions"):
                    deny()

    def validate(self):
        require_role(SECRETARY)
        _text(self.title, "问卷标题", 200, required=True)
        _text(self.description, "问卷说明", MAX_TEXT)
        validate_questions(self, publishing=self.status != DRAFT)
        if self.is_new():
            return
        old = self.get_doc_before_save()
        if self.publisher != old.publisher:
            deny()
        changed_state = self.status != old.status
        if changed_state:
            if not trusted(self) or (old.status, self.status) not in {(DRAFT, PUBLISHED), (PUBLISHED, CLOSED)}:
                frappe.throw("请使用发布或关闭操作，不能直接更改问卷状态。")
        if old.status != DRAFT:
            if any(self.get(field) != old.get(field) for field in ("title", "description", "deadline")):
                frappe.throw("发布后标题、说明和截止时间锁定，请另建问卷。")
            if _question_content(self) != _question_content(old):
                frappe.throw("发布后题目锁定，请另建问卷。")
        for field in ("published_at", "closed_at"):
            if self.get(field) != old.get(field) and not (trusted(self) and changed_state):
                frappe.throw("发布和关闭时间由服务器记录。")


class RGSurveyQuestion(Document):
    def validate(self):
        # Table rows are persisted by their parent, without standalone CRUD.
        frappe.throw("题目只能随问卷保存。")


class RGSurveyResponse(Document):
    def check_if_latest(self):
        if not self.is_new():
            survey = frappe.db.get_value(self.doctype, self.name, "survey")
            if survey:
                frappe.get_doc("RG Survey", survey, for_update=True)
        return super().check_if_latest()

    def validate(self):
        if not trusted(self):
            frappe.throw("请通过问卷填写入口提交或修改本人答卷。")
        require_role(*ROLES)
        if self.respondent != frappe.session.user:
            deny()
        if not self.is_new():
            old = self.get_doc_before_save()
            if any(self.get(field) != old.get(field) for field in ("survey", "respondent", "submitted_at")):
                frappe.throw("答卷身份和首次提交时间不可修改。")


def _require_current_roles(allowed, participant=False):
    """Called after the survey lock; never consult cached roles or RR snapshots."""
    user = frappe.session.user
    if user == "Administrator" and not participant:
        return set(ROLES)
    users = frappe.db.sql("SELECT enabled FROM `tabUser` WHERE name=%s FOR UPDATE", (user,))
    if not users or not users[0][0]:
        deny()
    members = frappe.db.sql("SELECT active FROM `tabRG Member` WHERE user=%s FOR UPDATE", (user,))
    if not members or not members[0][0]:
        deny()
    assigned = frappe.db.sql(
        "SELECT role FROM `tabHas Role` WHERE parent=%s AND parenttype='User' "
        "AND parentfield='roles' ORDER BY name FOR UPDATE", (user,),
    )
    current = {row[0] for row in assigned} & set(ROLES)
    if not current.intersection(allowed):
        deny()
    return current


def _locked_survey(name, allowed=ROLES, participant=False):
    secure_get("RG Survey", name)
    doc = frappe.get_doc("RG Survey", name, for_update=True)
    current_roles = _require_current_roles(allowed, participant=participant)
    if doc.status == DRAFT and SECRETARY not in current_roles:
        deny()
    # Explicit current reads also cover children after a pre-existing RR snapshot.
    rows = frappe.db.sql(
        "SELECT * FROM `tabRG Survey Question` WHERE parent=%s AND parenttype='RG Survey' "
        "AND parentfield='questions' ORDER BY idx FOR UPDATE", (name,), as_dict=True,
    )
    doc.set("questions", rows)
    return doc


def _is_open(doc, current=None):
    return doc.status == PUBLISHED and (
        not doc.deadline or (current or now_datetime()) < get_datetime(doc.deadline)
    )


@frappe.whitelist(methods=["POST"])
def publish_survey(name):
    require_role(SECRETARY)
    doc = _locked_survey(name, (SECRETARY,))
    if doc.status != DRAFT:
        frappe.throw("只有草稿可以发布。")
    validate_questions(doc, publishing=True)
    current = now_datetime()
    if doc.deadline and get_datetime(doc.deadline) <= current:
        frappe.throw("截止时间必须晚于服务器当前时间，草稿已保留。")
    doc.status, doc.published_at = PUBLISHED, current
    save_internal(doc)
    return _survey_dict(doc)


@frappe.whitelist(methods=["POST"])
def close_survey(name):
    require_role(SECRETARY)
    doc = _locked_survey(name, (SECRETARY,))
    if doc.status != PUBLISHED:
        frappe.throw("只有已发布的问卷可以关闭。")
    doc.status, doc.closed_at = CLOSED, now_datetime()
    save_internal(doc)
    return _survey_dict(doc)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            frappe.throw("答卷中的题目标识不能重复。")
        result[key] = value
    return result


def canonical_answers(doc, answers):
    if isinstance(answers, str):
        if len(answers) > 1024 * 1024:
            frappe.throw("答卷过大。")
        try:
            answers = json.loads(answers, object_pairs_hook=_unique_pairs)
        except (ValueError, RecursionError):
            frappe.throw("答卷须为有效的 JSON 对象。")
    if not isinstance(answers, dict):
        frappe.throw("答卷须按题目标识提交。")
    questions = doc.get("questions") or []
    if set(answers) - {question.name for question in questions}:
        frappe.throw("答卷包含不属于本问卷的题目。")
    result = {}
    for number, question in enumerate(questions, 1):
        value = answers.get(question.name)
        choices = _options(question, number)
        if question.question_type == MULTIPLE:
            if value is None:
                value = []
            if (not isinstance(value, list) or len(value) > MAX_OPTIONS
                    or any(not isinstance(item, str) or item not in choices for item in value)
                    or len(value) != len(set(value))):
                frappe.throw(f"第 {number} 题：多选答案包含非法或重复选项。")
            value = [choice for choice in choices if choice in value]
        elif question.question_type == SINGLE:
            if value is None:
                value = ""
            if not isinstance(value, str) or (value and value not in choices):
                frappe.throw(f"第 {number} 题：单选答案包含非法选项。")
        else:
            value = _text(value, f"第 {number} 题的简答内容", MAX_TEXT)
        if question.required and not value:
            frappe.throw(f"第 {number} 题：请完成必填题后提交，答卷尚未保存。")
        result[question.name] = value
    return result


def _response_dict(doc):
    if not doc:
        return None
    return {
        "name": doc.name, "survey": doc.survey, "respondent": doc.respondent,
        "respondent_name": doc.respondent_name, "answers": json.loads(doc.answers_json),
        "submitted_at": doc.submitted_at, "updated_at": doc.updated_at, "revision": doc.revision,
    }


def _own_response(survey):
    rows = frappe.db.sql(
        "SELECT name, survey, respondent, respondent_name, answers_json, submitted_at, updated_at, revision "
        "FROM `tabRG Survey Response` WHERE survey=%s AND respondent=%s FOR UPDATE",
        (survey, frappe.session.user), as_dict=True,
    )
    return _response_dict(rows[0]) if rows else None


def _survey_dict(doc):
    current = now_datetime()
    return {
        "name": doc.name, "title": doc.title, "description": doc.description,
        "kind": doc.kind, "status": doc.status, "deadline": doc.deadline,
        "publisher": doc.publisher, "published_at": doc.published_at, "closed_at": doc.closed_at,
        "server_time": current, "accepting_responses": bool(_is_open(doc, current)),
        "questions": [{"name": row.name, "title": row.title, "question_type": row.question_type,
                       "required": int(row.required or 0), "options": _options(row)} for row in doc.questions],
    }


@frappe.whitelist()
def get_survey(name):
    require_role(*ROLES)
    result = _survey_dict(_locked_survey(name))
    result["my_response"] = _own_response(name)
    return result


@frappe.whitelist()
def my_response(survey):
    require_role(*ROLES)
    _locked_survey(survey)
    return _own_response(survey)


@frappe.whitelist(methods=["POST"])
def submit_response(survey, answers):
    require_role(*ROLES)
    doc = _locked_survey(survey, participant=True)
    current = now_datetime()  # the deadline is checked after acquiring the parent lock
    if not _is_open(doc, current):
        frappe.throw("问卷尚未发布、已截止或已关闭，不能新增或修改答卷。")
    validate_questions(doc, publishing=True)
    content = canonical_answers(doc, answers)
    member = frappe.db.sql(
        "SELECT display_name FROM `tabRG Member` WHERE user=%s AND active=1 FOR UPDATE",
        (frappe.session.user,),
    )[0][0]
    rows = frappe.db.sql(
        "SELECT name FROM `tabRG Survey Response` WHERE survey=%s AND respondent=%s LIMIT 1 FOR UPDATE",
        (survey, frappe.session.user),
    )
    if rows:
        response = frappe.get_doc("RG Survey Response", rows[0][0], for_update=True)
        response.revision = int(response.revision or 0) + 1
    else:
        response = frappe.get_doc({
            "doctype": "RG Survey Response", "survey": survey, "respondent": frappe.session.user,
            "submitted_at": current, "revision": 1,
        })
    response.respondent_name, response.updated_at = member, current
    response.answers_json = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    save_internal(response, new=not rows)
    return _response_dict(response)


def _result_data(doc):
    responses = frappe.db.sql(
        "SELECT name, survey, respondent, respondent_name, answers_json, submitted_at, updated_at, revision "
        "FROM `tabRG Survey Response` WHERE survey=%s ORDER BY updated_at, name FOR UPDATE",
        (doc.name,), as_dict=True,
    )
    details = [_response_dict(response) for response in responses]
    members = frappe.db.sql(
        "SELECT DISTINCT m.user, m.display_name FROM `tabRG Member` m JOIN `tabUser` u ON u.name=m.user "
        "JOIN `tabHas Role` h ON h.parent=m.user AND h.parenttype='User' "
        "WHERE m.active=1 AND u.enabled=1 AND h.role IN %(roles)s ORDER BY m.display_name, m.user",
        {"roles": ROLES}, as_dict=True,
    )
    submitted = {response["respondent"] for response in details}
    pending = [dict(member) for member in members if member.user not in submitted]
    summaries = []
    for question in doc.questions:
        item = {"name": question.name, "title": question.title, "question_type": question.question_type,
                "answered_count": sum(bool(response["answers"].get(question.name)) for response in details)}
        if question.question_type == TEXT:
            item["answers"] = [
                {"respondent": response["respondent"], "respondent_name": response["respondent_name"],
                 "answer": response["answers"][question.name], "updated_at": response["updated_at"]}
                for response in details if response["answers"].get(question.name)
            ]
        else:
            item["options"] = [{
                "label": option,
                "count": sum(option in response["answers"].get(question.name, [])
                             if question.question_type == MULTIPLE
                             else option == response["answers"].get(question.name) for response in details),
            } for option in _options(question)]
        summaries.append(item)
    return {
        "survey": _survey_dict(doc), "submitted_count": len(details), "active_member_count": len(members),
        "active_submitted_count": len(members) - len(pending), "pending_count": len(pending),
        "pending_members": pending, "questions": summaries, "responses": details,
    }


@frappe.whitelist()
def survey_results(survey):
    require_role(SECRETARY)
    return _result_data(_locked_survey(survey, (SECRETARY,)))


def csv_cell(value):
    value = str(value if value is not None else "")
    candidate = value.lstrip(" \t\r\n\v\f\ufeff\u00a0")
    if candidate.startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        value = "'" + value
    return value


@frappe.whitelist()
def export_csv(survey):
    require_role(SECRETARY)
    doc = _locked_survey(survey, (SECRETARY,))
    result = _result_data(doc)
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow([csv_cell(value) for value in [
        "账号", "姓名", "首次提交时间", "最近修改时间", "修订次数",
        *[f"{index}. {question.title}" for index, question in enumerate(doc.questions, 1)],
    ]])
    for response in result["responses"]:
        values = []
        for question in doc.questions:
            value = response["answers"].get(question.name, "")
            values.append("\n".join(value) if isinstance(value, list) else value)
        writer.writerow([csv_cell(value) for value in [
            response["respondent"], response["respondent_name"], response["submitted_at"],
            response["updated_at"], response["revision"], *values,
        ]])
    frappe.response.update({
        "filename": f"{doc.name}.csv", "filecontent": ("\ufeff" + output.getvalue()).encode("utf-8"),
        "type": "download", "content_type": "text/csv; charset=utf-8", "display_content_as": "attachment",
    })
