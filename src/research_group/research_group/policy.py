"""Shared policy. The tokens are Python object identities, never client flags."""
import frappe

TEACHER = "课题组教师"
STUDENT = "课题组学生"
SECRETARY = "课题组秘书"
ROLES = (TEACHER, STUDENT, SECRETARY)
PRIVATE = ("RG Topic", "RG Progress", "RG Appointment", "RG Discussion Record")
MANAGED = (*PRIVATE, "RG Group Meeting", "RG Meeting Report", "RG Material Version", "RG Member",
           "RG Manuscript Version")
PENDING, APPROVED, REJECTED, CANCELLED, ARCHIVED = "待审批", "已批准", "已拒绝", "已取消", "已归档"
DRAFT, PUBLISHED = "草稿", "已发布"
SERVICE_TOKEN = object()
FILE_TOKEN = object()


def roles(user=None):
    user = user or frappe.session.user
    if user == "Administrator":
        return set(ROLES)
    if user == "Guest" or not frappe.db.get_value("User", user, "enabled"):
        return set()
    if not frappe.db.exists("RG Member", {"user": user, "active": 1}):
        return set()
    return set(frappe.get_roles(user)) & set(ROLES)


def require_role(*allowed):
    assigned = roles()
    if not assigned.intersection(allowed):
        deny()
    return assigned


def require_business_role(*allowed):
    """Notification-producing records take the identity gate before parent rows."""
    from research_group.ai.security import native_identity_lock
    from research_group.surveys import _require_current_roles

    native_identity_lock()
    return _require_current_roles(allowed)


def deny():
    frappe.throw("没有权限访问或操作此记录。", frappe.PermissionError)


def secure_get(doctype, name, ptype="read"):
    from research_group.permissions import has_permission

    try:
        doc = frappe.get_doc(doctype, name)
    except frappe.DoesNotExistError:
        deny()
    if not has_permission(doc, ptype):
        deny()
    return doc


def trusted(doc):
    return doc.flags.get("rg_service") is SERVICE_TOKEN


def save_internal(doc, new=False):
    doc.flags.rg_service = SERVICE_TOKEN
    return doc.insert(ignore_permissions=True) if new else doc.save(ignore_permissions=True)


def require_member(user, role):
    if role not in roles(user):
        frappe.throw("请选择已启用且具有对应角色的课题组成员。")



def require_business_member(user, role):
    """Current target identity check while the event producer holds the gate."""
    from research_group.ai.security import native_identity_lock

    native_identity_lock()
    if user == "Administrator" and role in ROLES:
        return
    found = frappe.db.sql(
        "SELECT h.role FROM `tabUser` u JOIN `tabRG Member` m ON m.user=u.name "
        "JOIN `tabHas Role` h ON h.parent=u.name AND h.parenttype='User' AND h.parentfield='roles' "
        "WHERE u.name=%s AND u.enabled=1 AND m.active=1 AND h.role=%s LOCK IN SHARE MODE",
        (user, role),
    )
    if not found:
        frappe.throw("请选择已启用且具有对应角色的课题组成员。")
