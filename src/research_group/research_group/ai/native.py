"""原生Version与附件旁路保护。"""
import frappe

from research_group.ai import AI_DOCTYPES


def version_permission(doc, ptype=None, user=None, **kwargs):
    return doc.get("ref_doctype") not in AI_DOCTYPES


def version_query(user=None):
    names = ",".join(frappe.db.escape(name) for name in AI_DOCTYPES)
    return f"COALESCE(`tabVersion`.ref_doctype,'') NOT IN ({names})"


def is_ai_file(doc):
    return doc.get("attached_to_doctype") in AI_DOCTYPES
