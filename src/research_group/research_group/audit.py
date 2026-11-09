import hashlib

import frappe

from research_group.files import read_stored_file_bytes


def material_integrity():
    if frappe.session.user != "Administrator":
        frappe.throw("仅管理员可执行完整性核查。", frappe.PermissionError)
    checked, errors = 0, []
    for row in frappe.get_all("RG Material Version", fields=["name", "material_file", "sha256", "size_bytes"]):
        checked += 1
        if not row.material_file or not frappe.db.exists("File", row.material_file):
            errors.append({"version": row.name, "error": "missing_file_row"})
            continue
        file = frappe.get_doc("File", row.material_file)
        try:
            data = read_stored_file_bytes(file)
            correct = (file.is_private and file.attached_to_doctype == "RG Material Version"
                       and file.attached_to_name == row.name and len(data) == row.size_bytes
                       and hashlib.sha256(data).hexdigest() == row.sha256)
        except (OSError, frappe.DoesNotExistError):
            correct = False
        if not correct:
            errors.append({"version": row.name, "error": "hash_size_parent_or_privacy_mismatch"})
    return {"checked_versions": checked, "errors": errors, "ok": not errors}

