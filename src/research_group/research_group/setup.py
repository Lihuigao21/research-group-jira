from copy import deepcopy

import frappe

from research_group.policy import ROLES, roles


def has_app_access():
    return bool(roles())


def before_install():
    for role in ROLES:
        if not frappe.db.exists("Role", role):
            frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)


def after_install():
    before_install()
    settings = frappe.get_single("System Settings")
    settings.update({
        "language": "zh", "time_zone": "Asia/Shanghai", "country": "China",
        "date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss", "max_file_size": 50,
        "disable_document_sharing": 1, "allow_guests_to_upload_files": 0,
        "only_allow_system_managers_to_upload_public_files": 1, "allow_error_traceback": 0,
    })
    settings.save(ignore_permissions=True)
    website = frappe.get_single("Website Settings")
    website.disable_signup = 1
    website.save(ignore_permissions=True)
    frappe.db.set_default("lang", "zh")
    frappe.db.set_default("suspend_email_queue", 1)
    # install-app alone must already carry the database uniqueness barriers and
    # the AI gate row; a later migrate repeats this idempotently.
    after_migrate()


def after_migrate():
    before_install()
    frappe.db.add_index("RG Appointment", ["teacher", "status", "start_at", "end_at"], "rg_teacher_overlap")
    frappe.db.add_unique("RG Meeting Report", ["meeting", "student"], "rg_unique_reporter")
    frappe.db.add_unique("RG Material Version", ["report", "version"], "rg_unique_material_version")
    frappe.db.add_index("RG Survey", ["status", "deadline"], "rg_survey_availability")
    frappe.db.add_unique("RG Survey Response", ["survey", "respondent"], "rg_unique_survey_respondent")
    from research_group.ai.security import install_state
    install_state()
    for doctype in ("RG AI Conversation", "RG AI Memory", "RG AI Plan", "RG AI Operation", "RG AI Run"):
        frappe.db.add_index(doctype, ["private_user", "status", "modified"], "rg_ai_owner_state")
    frappe.db.add_unique("RG AI Message", ["conversation", "sequence"], "rg_ai_message_sequence")
    frappe.db.add_index("RG AI Message", ["request_id", "role"], "rg_ai_request_messages")
    migrate_registration_reservations()
    # The dedicated services also hold parent locks. Database uniqueness is
    # retained as a final barrier if a request is retried on another connection.
    for doctype, fields, index in (
        ("RG Registration", ["reserved_email"], "rg_registration_email"),
        ("RG Registration", ["reserved_student_id"], "rg_registration_student"),
        ("RG Account Profile", ["user"], "rg_profile_user"),
        ("RG Account Profile", ["student_id"], "rg_profile_student"),
        ("RG Account Profile", ["contact_email"], "rg_profile_contact"),
        ("RG Mail Event", ["event_key"], "rg_mail_event_key"),
        ("RG Meal Order", ["round", "member"], "rg_meal_member"),
        ("RG Manuscript Version", ["manuscript", "version"], "rg_manuscript_version"),
    ):
        frappe.db.add_unique(doctype, fields, index)
    for doctype, fields, index in (
        ("RG Mail Event", ["status", "creation"], "rg_mail_drain"),
        ("RG Mail Event", ["message", "recipient_user"], "rg_mail_recipient"),
        ("RG Mail Event", ["recipient_user", "creation"], "rg_mail_person_history"),
        ("RG Mail Event", ["related_doctype", "related_name", "creation"], "rg_mail_source_history"),
        ("RG Message", ["actor", "status"], "rg_message_actor"),
        ("RG Meal Round", ["status", "deadline"], "rg_meal_availability"),
        ("RG Manuscript", ["author", "reviewer"], "rg_manuscript_participant"),
        ("RG Manuscript Review", ["manuscript_version", "recorded_at"], "rg_manuscript_reviews"),
        ("RG Manuscript Selection", ["private_user", "status", "expires_at"], "rg_selection_expiry"),
    ):
        frappe.db.add_index(doctype, fields, index)
    harden_accounts()


def migrate_registration_reservations():
    """Keep rejected and expired evidence without reserving their identities.

    This also upgrades an intermediate candidate that used uniqueness directly
    on the original audit fields. Other existing applications retain their
    reservations; no existing User or registration identity is reassigned.
    """
    indexes = {}
    for row in frappe.db.sql("SHOW INDEX FROM `tabRG Registration`", as_dict=True):
        if not row.Non_unique and row.Key_name != "PRIMARY":
            indexes.setdefault(row.Key_name, []).append((int(row.Seq_in_index), row.Column_name))
    for name, fields in indexes.items():
        columns = [field for _, field in sorted(fields)]
        if columns in (["email"], ["student_id"]):
            # Database-origin names only; quote the identifier even on a site
            # that previously chose its own index name.
            quoted = name.replace("`", "``")
            frappe.db.sql_ddl(f"ALTER TABLE `tabRG Registration` DROP INDEX `{quoted}`")
    frappe.db.sql("UPDATE `tabRG Registration` SET reserved_email=email,reserved_student_id=student_id "
                  "WHERE status NOT IN ('expired','rejected') AND (reserved_email IS NULL OR reserved_email='') "
                  "AND (reserved_student_id IS NULL OR reserved_student_id='')")


def harden_accounts():
    """Enforce the approved authentication policy without enabling mail or 2FA."""
    settings = frappe.get_single("System Settings")
    settings.update({
        "enable_password_policy": 1, "minimum_password_score": "4",
        "session_expiry": "08:00", "allow_consecutive_login_attempts": 5,
        "allow_login_after_fail": 900, "allow_login_using_mobile_number": 0,
        "allow_login_using_user_name": 0, "login_with_email_link": 0,
    })
    settings.save(ignore_permissions=True)
    website = frappe.get_single("Website Settings")
    website.disable_signup = 1
    website.save(ignore_permissions=True)


def boot_session(bootinfo):
    assigned = roles()
    bootinfo.research_group_roles = sorted(assigned)
    bootinfo.research_group_demo = bool(frappe.conf.get("rg_demo"))
    if not assigned or not (bootinfo.get("sysdefaults") or {}).get("setup_complete"):
        return
    from research_group.portals import PORTALS

    available = [dict(item) for item in PORTALS.values() if item["role"] in assigned]
    bootinfo.research_group_portals = available
    bootinfo.research_group_home_route = available[0]["route"]
    bootinfo.home_page = available[0]["route"]

    # The standard shortcut-only Workspaces duplicate the real role Pages.
    # Change this boot payload, not cached shared navigation or saved user data.
    legacy = {"课题组协作", *(item["title"] for item in PORTALS.values())}
    workspace_info = deepcopy(bootinfo.get("workspaces") or {})
    removed = {item["name"] for item in workspace_info.get("pages", [])
               if item.get("name") in legacy and item.get("module") == "Research Group"
               and item.get("standard")}
    # Keep workspace metadata so links from user-created shells still parse.
    # Exact frontend aliases lead old bookmarks to the real role Pages.
    sidebars = deepcopy(bootinfo.get("module_sidebars") or {})
    sidebar = sidebars.get("Research Group")
    if sidebar:
        routes = {item["route"] for item in available}
        items = sidebar.get("items", [])
        portals = [{**item, "child": 0} for item in items
                   if item.get("link_type") == "Page" and item.get("link_to") in routes]
        portals.sort(key=lambda item: [value["route"] for value in available].index(item["link_to"]))
        sidebar["items"] = portals + [item for item in items
            if not (item.get("link_type") == "Workspace" and item.get("link_to") in removed)
            and not (item.get("link_type") == "Page" and item.get("link_to") in routes)]
        sidebar["workspaces"] = [name for name in sidebar.get("workspaces", []) if name not in removed]
        bootinfo.module_sidebars = sidebars


def set_guest_language():
    # Frappe otherwise prefers an English browser header over the site's Chinese.
    if frappe.session.user == "Guest" and not frappe.form_dict.get("_lang"):
        frappe.local.lang = "zh"

