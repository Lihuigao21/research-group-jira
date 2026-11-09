app_name = "research_group"
app_title = "课题组协作"
app_publisher = "Research Group"
app_description = "课题进展、预约讨论和组会材料"
app_email = "noreply@example.invalid"
app_license = "mit"
app_home = "/desk/research-group/课题组协作"
add_to_apps_screen = [{
    "name": app_name,
    "title": app_title,
    "route": app_home,
    "has_permission": "research_group.setup.has_app_access",
    "sequence_id": 1,
}]
required_apps = ["frappe"]
app_include_css = ["/assets/research_group/css/desk.css", "/assets/research_group/css/ai_assistant.css",
                   "/assets/research_group/css/portals.css"]
app_include_js = "/assets/research_group/js/desk.js"
web_include_js = ["/assets/research_group/js/accounts.js"]
before_install = "research_group.setup.before_install"
after_install = "research_group.setup.after_install"
after_migrate = "research_group.setup.after_migrate"
boot_session = "research_group.setup.boot_session"
before_request = ["research_group.setup.set_guest_language", "research_group.accounts.validate_group_session",
                  "research_group.collaboration_security.before_request"]
auth_hooks = ["research_group.accounts.validate_group_api_identity"]

_doctypes = [
    "RG Member", "RG Topic", "RG Progress", "RG Appointment", "RG Discussion Record",
    "RG Group Meeting", "RG Meeting Report", "RG Material Version",
]
_survey_doctypes = ["RG Survey", "RG Survey Question", "RG Survey Response"]
has_permission = {d: "research_group.permissions.has_permission" for d in [*_doctypes, *_survey_doctypes]}
has_permission["File"] = "research_group.permissions.file_has_permission"
permission_query_conditions = {
    "RG Member": "research_group.permissions.member_query_conditions",
    "RG Topic": "research_group.permissions.topic_query_conditions",
    "RG Progress": "research_group.permissions.progress_query_conditions",
    "RG Appointment": "research_group.permissions.appointment_query_conditions",
    "RG Discussion Record": "research_group.permissions.discussion_query_conditions",
    "RG Group Meeting": "research_group.permissions.meeting_query_conditions",
    "RG Meeting Report": "research_group.permissions.report_query_conditions",
    "RG Material Version": "research_group.permissions.material_query_conditions",
    "RG Survey": "research_group.permissions.survey_query_conditions",
    "RG Survey Question": "research_group.permissions.survey_question_query_conditions",
    "RG Survey Response": "research_group.permissions.survey_response_query_conditions",
    "File": "research_group.permissions.file_query_conditions",
}
override_doctype_class = {
    "File": "research_group.files.ResearchGroupFile",
    "User": "research_group.accounts.ResearchGroupAccountUser",
    "Role": "research_group.ai.identity.ResearchGroupRole",
}
override_whitelisted_methods = {
    "frappe.desk.form.load.getdoc": "research_group.files.load_document",
    "frappe.desk.form.load.get_docinfo": "research_group.files.load_document_info",
    "frappe.desk.form.load.get_filtered_attachments": "research_group.files.filtered_attachments",
    "frappe.core.api.file.zip_files": "research_group.files.zip_files",
    "upload_file": "research_group.files.upload_file",
    "frappe.handler.upload_file": "research_group.files.upload_file",
    "frappe.core.doctype.user.user.reset_password": "research_group.accounts.request_password_reset",
    "frappe.core.doctype.user.user.update_password": "research_group.accounts.update_password",
    "frappe.www.login.send_login_link": "research_group.accounts.block_native_link_login",
    "frappe.www.login.login_via_key": "research_group.accounts.block_native_link_login",
    "frappe.www.login.login_via_token": "research_group.accounts.block_native_link_login",
}
doctype_js = {d: "public/js/forms.js" for d in _doctypes}
doctype_js.update({d: "public/js/surveys.js" for d in _survey_doctypes if d != "RG Survey Question"})

# AI内容只由专用接口逐条验来源，原生读写/导出均不开放。
from research_group.ai import AI_DOCTYPES
has_permission.update({d: "research_group.ai.storage.no_permission" for d in AI_DOCTYPES})
permission_query_conditions.update({d: "research_group.ai.storage.no_query" for d in AI_DOCTYPES})
has_permission["Version"] = "research_group.ai.native.version_permission"
permission_query_conditions["Version"] = "research_group.ai.native.version_query"
from research_group.collaboration_security import INTERNAL_DOCTYPES
has_permission.update({d: "research_group.collaboration_security.no_permission" for d in INTERNAL_DOCTYPES})
permission_query_conditions.update({d: "research_group.collaboration_security.no_query" for d in INTERNAL_DOCTYPES})
has_permission["Version"] = "research_group.collaboration_security.version_permission"
permission_query_conditions["Version"] = "research_group.collaboration_security.version_query"
doc_events = {
    d: {"before_insert": "research_group.ai.security.native_identity_lock",
        "before_save": "research_group.ai.security.native_identity_lock",
        "on_update": "research_group.ai.security.identity_changed",
        "on_trash": "research_group.ai.security.identity_changed"}
    for d in ("User", "Role", "RG Member")
}
for _event_doctype in ("RG Appointment", "RG Group Meeting", "RG Meal Round"):
    doc_events.setdefault(_event_doctype, {})["on_update"] = "research_group.notifications.business_event"
for _identity_doctype in ("User", "RG Member"):
    for _identity_event in ("on_update", "on_trash"):
        doc_events[_identity_doctype][_identity_event] = [
            "research_group.ai.security.identity_changed", "research_group.accounts.identity_status_changed",
        ]

scheduler_events = {
    "cron": {
        "*/5 * * * *": ["research_group.notifications.drain_events"],
        "*/10 * * * *": ["research_group.notifications.enqueue_deadline_reminders"],
    },
}

