"""Public account forms. GET never looks up or consumes a verification key."""
import frappe

no_cache = True


def get_context(context):
    context.no_cache = True
    context.no_header = True
    context.no_breadcrumbs = True
    context.title = "课题组账号"
    context.rg_signed_in = frappe.session.user != "Guest"
