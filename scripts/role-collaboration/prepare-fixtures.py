"""Private-pipe preparation of candidate-only fictional verified contacts."""
import json
import os
import re
import frappe

def prepare(request):
    SITE = request["site"]
    os.chdir("/home/frappe/frappe-bench/sites")
    frappe.init(SITE, sites_path="/home/frappe/frappe-bench/sites")
    frappe.connect()
    try:
        named = request.get("expected_db_name")
        if named is not None:
            assert re.fullmatch(r"_[0-9a-f]{16}", named) and frappe.conf.db_name == named
        else:
            assert str(frappe.conf.db_name).startswith("rg_candidate_")
        assert frappe.conf.host_name == request["base"]
        assert frappe.conf.rg_mail_mode == "capture" and frappe.conf.mute_emails and frappe.conf.allow_tests
        frappe.set_user("Administrator")
        frappe.in_test = True
        from research_group import accounts, notifications
        from research_group.ai.security import global_lock
        global_lock()
        prepared = []
        for key, user in {"teacher": "teacher.demo@example.invalid", "one": "student.one.demo@example.invalid",
                          "two": "student.two.demo@example.invalid", "secretary": "secretary.demo@example.invalid"}.items():
            member = frappe.db.get_value("RG Member", {"user": user}, ["is_demo", "active"], as_dict=True)
            assert member and member.is_demo and member.active and frappe.db.get_value("User", user, "enabled")
            existing = frappe.db.get_value("RG Account Profile", {"user": user}, "name")
            if existing:
                profile = frappe.get_doc("RG Account Profile", existing)
                assert profile.student_id == "HTTP-DEMO-" + key
                assert profile.contact_verified and profile.email_version == 1
            else:
                profile = accounts._insert("RG Account Profile", user=user, student_id="HTTP-DEMO-" + key,
                    contact_email="rg-fictional-" + key + "@m.fudan.edu.cn", contact_verified=1, email_version=1)
            # The source-bound contract is a new immutable event identity;
            # retain earlier profile-based fixture events without rewriting.
            notifications.queue_template("candidate-fixture-status:v2:" + user, "account_status", recipient_user=user,
                values={"display_name": "Fictional candidate " + key, "status": "Fictional HTTP acceptance only",
                        "login_id": user}, related_doctype="User", related_name=user)
            prepared.append({"role": key, "profile": profile.name})
        frappe.db.commit()
        return {"ok": True, "mail_mode": "capture", "user_or_business_rows_changed": False, "profiles": prepared}
    finally:
        frappe.destroy()
