"""Dedicated collaboration services own these tables, including native UI access.

The request guard also covers Administrator's native permission short circuit.
Administrative CLI maintenance remains available; none of these checks replace
the service's current-identity, row and transaction checks.
"""
from urllib.parse import unquote, urlsplit

import frappe

from research_group.policy import deny

ACCOUNT_DOCTYPES = ("RG Registration", "RG Account Profile", "RG Account Audit")
MAIL_DOCTYPES = ("RG Message", "RG Mail Event")
MEAL_DOCTYPES = ("RG Meal Round", "RG Meal Item", "RG Meal Order", "RG Meal Order Item")
MANUSCRIPT_DOCTYPES = (
    "RG Manuscript", "RG Manuscript Version", "RG Manuscript Review", "RG Manuscript Selection",
)
PORTAL_DOCTYPES = ("RG Research Direction", "RG Direction Topic", "RG Portal Settings", "RG Announcement")
INTERNAL_DOCTYPES = (*ACCOUNT_DOCTYPES, *MAIL_DOCTYPES, *MEAL_DOCTYPES,
                     *MANUSCRIPT_DOCTYPES, *PORTAL_DOCTYPES)

# This map is intentionally source-controlled, never derived from client input
# or a dynamically discovered tool catalog. Guest account writes have their own
# one-use cookie nonce and are guarded inside accounts.browser_write instead.
_SERVICE_WRITES = {
    "meals.create_round": ("title", "deadline", "meeting", "menu", "copy_from"),
    "meals.update_draft": ("name", "expected_revision", "title", "deadline", "meeting", "menu"),
    "meals.publish_round": ("name", "expected_revision"),
    "meals.set_round_status": ("name", "closed", "expected_revision"),
    "meals.set_deadline": ("name", "deadline", "expected_revision"),
    "meals.submit_order": ("round", "items", "remarks", "expected_revision", "budget_acknowledged", "request_id"),
    "meals.void_order": ("order", "reason", "expected_revision"),
    "portals.save_home": ("introduction", "navigation", "expected_revision"),
    "portals.restore_home": ("expected_revision",),
    "portals.save_announcement": ("title", "body", "audience", "name", "expected_revision", "status"),
    "portals.save_direction": ("title", "goal", "priority", "topics", "name", "expected_revision"),
    "notifications.preview_message": ("subject", "body", "recipients", "recipient_role", "all_members", "kind"),
    "notifications.confirm_message": ("name", "confirmation_digest"),
    "notifications.retry_event": ("name",),
    "manuscripts.create_manuscript": ("title", "reviewer", "topic"),
    "manuscripts.upload_version": ("manuscript", "expected_revision", "kind", "parent_version"),
    "manuscripts.add_review": ("version", "content", "anchor"),
    "manuscripts.reply_review": ("review", "content"),
    "manuscripts.resolve_review": ("review", "status"),
    "manuscripts.create_selection": ("version", "original_text", "anchor"),
    "manuscripts.revoke_selection": ("name",),
}


def no_permission(doc=None, ptype=None, user=None, **kwargs):
    return False


def no_query(user=None):
    return "1=0"


def guard_native(doctype):
    from research_group.ai import AI_DOCTYPES

    if doctype in (*INTERNAL_DOCTYPES, *AI_DOCTYPES):
        deny()


def version_permission(doc, ptype=None, user=None, **kwargs):
    from research_group.ai import AI_DOCTYPES

    return doc.get("ref_doctype") not in (*INTERNAL_DOCTYPES, *AI_DOCTYPES)


def version_query(user=None):
    from research_group.ai import AI_DOCTYPES

    names = ",".join(frappe.db.escape(name) for name in (*INTERNAL_DOCTYPES, *AI_DOCTYPES))
    return f"COALESCE(`tabVersion`.ref_doctype,'') NOT IN ({names})"


def is_internal_file(doc):
    return doc.get("attached_to_doctype") in INTERNAL_DOCTYPES


def _document_types(value):
    """Inspect native top-level document envelopes without limiting business text.

    The native endpoint parses the same JSON. Its request admission and each
    controller retain their existing limits; a private-table guard must not
    impose a smaller shared limit on ordinary surveys and other documents.
    Only envelope rows are inspected, never nested bodies or child tables.
    """
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        return [value.get("doctype")]
    if isinstance(value, list):
        return [row.get("doctype") for row in value if isinstance(row, dict)]
    return []


def before_request():
    request = getattr(frappe.local, "request", None)
    if request is None:
        return
    # Dedicated APIs authorize their own whitelist of fields. Generic native
    # operations may name a type in a path, a form field or a JSON document.
    path = unquote(request.path)
    form = frappe.form_dict
    method = form.get("cmd") or (path.split("/method/", 1)[1].split("/", 1)[0]
                                  if "/method/" in path else "")
    short_method = method.removeprefix("research_group.")
    if method.startswith("research_group.") and short_method in _SERVICE_WRITES:
        from research_group.ai.api import browser_write
        browser_write(_SERVICE_WRITES[short_method])
        header = request.headers.get("Origin") or request.headers.get("Referer")
        if not header:
            deny()
        origin, current = urlsplit(header), urlsplit(request.host_url)
        if (origin.scheme, origin.netloc) != (current.scheme, current.netloc):
            deny()
    if method.startswith("research_group."):
        return
    candidates = [form.get("doctype"), form.get("dt"), form.get("parenttype")]
    for marker in ("/api/resource/", "/api/v1/resource/", "/api/v2/document/", "/api/v2/doctype/"):
        if marker in path:
            candidates.append(path.split(marker, 1)[1].split("/", 1)[0])
    for key in ("doc", "docs", "data"):
        candidates.extend(_document_types(form.get(key)))
    for doctype in candidates:
        guard_native(doctype)
    if "Version" in candidates:
        name = form.get("name")
        for marker in ("/api/resource/Version/", "/api/v1/resource/Version/", "/api/v2/document/Version/"):
            if marker in path:
                name = path.split(marker, 1)[1].split("/", 1)[0]
        if name:
            guard_native(frappe.db.get_value("Version", name, "ref_doctype"))
    if "File" in candidates:
        name = form.get("name")
        for marker in ("/api/resource/File/", "/api/v1/resource/File/", "/api/v2/document/File/"):
            if marker in path:
                name = path.split(marker, 1)[1].split("/", 1)[0]
        if name and frappe.db.exists("File", name):
            from research_group.permissions import file_has_permission
            if not file_has_permission(frappe.get_doc("File", name), "read"):
                deny()
