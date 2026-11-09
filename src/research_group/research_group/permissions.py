"""The same row policy protects Desk, REST, reports, File lists and downloads."""
import frappe

from research_group.policy import (
    APPROVED, DRAFT, MANAGED, PRIVATE, PUBLISHED, SECRETARY, STUDENT, TEACHER, roles,
)

READ = {"read", "select", "report", "print", "export"}


def _meeting_visible(name, user):
    r = roles(user)
    if not r:
        return False
    meeting = frappe.db.get_value("RG Group Meeting", name, ["status", "publisher"], as_dict=True)
    return bool(meeting and (meeting.status == PUBLISHED or r & {TEACHER, SECRETARY}))


def has_permission(doc, ptype="read", user=None, **kwargs):
    ptype = ptype or "read"  # Desk asks for the complete permission map with ptype=None
    user = user or frappe.session.user
    from research_group.collaboration_security import INTERNAL_DOCTYPES
    if doc.doctype in INTERNAL_DOCTYPES:
        return False
    if user == "Administrator":
        return True
    r = roles(user)
    if not r:
        return False
    dt = doc.doctype
    if ptype in {"delete", "share", "email", "submit", "cancel", "amend", "import"}:
        return False
    if dt == "RG Member":
        return ptype in READ
    if dt == "RG Survey":
        if ptype in READ:
            return SECRETARY in r or doc.get("status") in {PUBLISHED, "已关闭"}
        if SECRETARY not in r:
            return False
        if ptype == "create":
            return True
        # Read stored state so a forged draft value cannot reopen a locked form.
        state = frappe.db.get_value("RG Survey", doc.get("name"), "status") if doc.get("name") else None
        return ptype == "write" and (state is None or state == DRAFT)
    if dt == "RG Survey Question":
        return ptype in READ and _survey_visible(doc.get("parent"), user)
    if dt == "RG Survey Response":
        if ptype not in READ:
            return False
        if SECRETARY in r:
            return True
        return (ptype in {"read", "select"} and doc.get("respondent") == user
                and _survey_visible(doc.get("survey"), user))
    if dt in PRIVATE:
        own = STUDENT in r and doc.get("student") == user
        readable = TEACHER in r or own
        if ptype in READ:
            return readable
        if dt == "RG Topic":
            return TEACHER in r
        if dt == "RG Progress":
            # Desk needs write for a new form's editable fields; persisted
            # progress remains append-only in both permissions and controller.
            new = not doc.get("name") or not bool(frappe.db.exists("RG Progress", doc.get("name")))
            return STUDENT in r and (ptype == "create" or (ptype == "write" and new))
        if dt == "RG Appointment":
            return (STUDENT in r and ptype == "create") or (
                ptype == "write" and (own or (TEACHER in r and doc.get("teacher") == user))
            )
        # Records are produced by the archive service, never generic CRUD.
        return False
    if dt == "RG Group Meeting":
        if ptype in READ:
            return doc.get("status") == PUBLISHED or bool(r & {TEACHER, SECRETARY})
        return bool(r & {TEACHER, SECRETARY}) and (
            ptype == "create" or doc.get("publisher") == user
        )
    if dt == "RG Meeting Report":
        if ptype in READ:
            return _meeting_visible(doc.get("meeting"), user)
        meeting = frappe.db.get_value("RG Group Meeting", doc.get("meeting"), ["publisher", "status"], as_dict=True)
        return bool(
            meeting and meeting.publisher == user and meeting.status == DRAFT
            and r & {TEACHER, SECRETARY} and ptype in {"create", "write"}
        )
    if dt == "RG Material Version":
        meeting = frappe.db.get_value("RG Meeting Report", doc.get("report"), "meeting")
        return ptype in READ and _meeting_visible(meeting, user)
    return False


def _survey_visible(name, user=None):
    r = roles(user)
    if not r:
        return False
    status = frappe.db.get_value("RG Survey", name, "status")
    return bool(status and (SECRETARY in r or status in {PUBLISHED, "已关闭"}))


def _survey_condition(alias, user=None):
    r = roles(user)
    if not r:
        return "1=0"
    if SECRETARY in r:
        return "1=1"
    return f"{alias}.`status` IN ({frappe.db.escape(PUBLISHED)}, {frappe.db.escape('已关闭')})"


def survey_query_conditions(user=None):
    return _survey_condition("`tabRG Survey`", user)


def survey_question_query_conditions(user=None):
    return ("`tabRG Survey Question`.parenttype='RG Survey' AND "
            "`tabRG Survey Question`.parentfield='questions' AND "
            "EXISTS (SELECT 1 FROM `tabRG Survey` s WHERE s.name=`tabRG Survey Question`.parent AND "
            + _survey_condition("s", user) + ")")


def survey_response_query_conditions(user=None):
    user = user or frappe.session.user
    r = roles(user)
    if not r:
        return "1=0"
    if SECRETARY in r:
        return ""
    return (f"`tabRG Survey Response`.respondent={frappe.db.escape(user)} AND "
            "EXISTS (SELECT 1 FROM `tabRG Survey` s WHERE s.name=`tabRG Survey Response`.survey AND "
            + _survey_condition("s", user) + ")")


def _private_condition(doctype, user=None):
    user = user or frappe.session.user
    r = roles(user)
    if user == "Administrator" or TEACHER in r:
        return ""
    if STUDENT in r:
        return f"`tab{doctype}`.`student` = {frappe.db.escape(user)}"
    return "1=0"


def member_query_conditions(user=None):
    return "" if roles(user) else "1=0"


def topic_query_conditions(user=None):
    return _private_condition("RG Topic", user)


def progress_query_conditions(user=None):
    return _private_condition("RG Progress", user)


def appointment_query_conditions(user=None):
    return _private_condition("RG Appointment", user)


def discussion_query_conditions(user=None):
    return _private_condition("RG Discussion Record", user)


def _meeting_condition(alias, user=None):
    r = roles(user)
    if not r:
        return "1=0"
    if r & {TEACHER, SECRETARY}:
        return "1=1"
    return f"{alias}.`status` = {frappe.db.escape(PUBLISHED)}"


def meeting_query_conditions(user=None):
    return _meeting_condition("`tabRG Group Meeting`", user)


def report_query_conditions(user=None):
    return ("EXISTS (SELECT 1 FROM `tabRG Group Meeting` m WHERE m.name = "
            "`tabRG Meeting Report`.meeting AND " + _meeting_condition("m", user) + ")")


def material_query_conditions(user=None):
    return ("EXISTS (SELECT 1 FROM `tabRG Meeting Report` r JOIN `tabRG Group Meeting` m "
            "ON m.name=r.meeting WHERE r.name=`tabRG Material Version`.report AND "
            + _meeting_condition("m", user) + ")")


def file_has_permission(doc, ptype="read", user=None, **kwargs):
    from research_group.files import is_managed_file_alias, is_survey_file
    from research_group.ai.native import is_ai_file
    from research_group.collaboration_security import is_internal_file

    ptype = ptype or "read"
    user = user or frappe.session.user
    if is_ai_file(doc):
        return False
    if is_internal_file(doc):
        if doc.get("attached_to_doctype") != "RG Manuscript Version" or ptype not in READ or not doc.is_private:
            return False
        from research_group.manuscripts import can_read
        try:
            parent = frappe.get_doc("RG Manuscript Version", doc.attached_to_name)
        except frappe.DoesNotExistError:
            return False
        return can_read(parent, user=user)
    if is_survey_file(doc) and user != "Administrator":
        return False
    if is_managed_file_alias(doc):
        return False
    if doc.get("attached_to_doctype") not in MANAGED:
        return True  # leave unrelated Frappe files to the native policy
    if ptype not in READ:
        return False
    try:
        parent = frappe.get_doc(doc.attached_to_doctype, doc.attached_to_name)
    except frappe.DoesNotExistError:
        return False
    return bool(doc.is_private and has_permission(parent, "read", user=user))


def file_query_conditions(user=None):
    """AND this with native File conditions, which otherwise only inspect DocPerm."""
    user = user or frappe.session.user
    f = "`tabFile`"
    from research_group.files import SURVEY_FILE_DOCTYPES, managed_file_alias_names

    from research_group.ai import AI_DOCTYPES
    from research_group.collaboration_security import INTERNAL_DOCTYPES
    excluded = ", ".join(frappe.db.escape(d) for d in
                         (*MANAGED, *SURVEY_FILE_DOCTYPES, *AI_DOCTYPES, *INTERNAL_DOCTYPES))
    aliases = managed_file_alias_names()
    unrelated = f"COALESCE({f}.attached_to_doctype, '') NOT IN ({excluded})"
    if aliases:
        unrelated += f" AND {f}.name NOT IN (" + ", ".join(frappe.db.escape(name) for name in aliases) + ")"
    terms = ["(" + unrelated + ")"]
    for dt in MANAGED:
        if dt in PRIVATE:
            condition = _private_condition(dt, user).replace(f"`tab{dt}`", "p") or "1=1"
        elif dt == "RG Manuscript Version":
            from research_group.manuscripts import permission_sql
            condition = permission_sql(dt, user=user).replace(f"`tab{dt}`", "p")
        elif dt == "RG Member":
            condition = "1=1" if roles(user) else "1=0"
        elif dt == "RG Group Meeting":
            condition = _meeting_condition("p", user)
        elif dt == "RG Meeting Report":
            condition = ("EXISTS (SELECT 1 FROM `tabRG Group Meeting` m WHERE m.name=p.meeting AND "
                         + _meeting_condition("m", user) + ")")
        else:
            condition = ("EXISTS (SELECT 1 FROM `tabRG Meeting Report` r JOIN `tabRG Group Meeting` m "
                         "ON m.name=r.meeting WHERE r.name=p.report AND "
                         + _meeting_condition("m", user) + ")")
        terms.append(f"({f}.is_private=1 AND {f}.attached_to_doctype={frappe.db.escape(dt)} "
                     f"AND EXISTS (SELECT 1 FROM `tab{dt}` p WHERE p.name={f}.attached_to_name AND {condition}))")
    return "(" + " OR ".join(terms) + ")"

