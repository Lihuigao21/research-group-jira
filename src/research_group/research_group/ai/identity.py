"""在Frappe原生父行锁之前加入AI授权闸门；不替换账号业务。"""
from frappe.core.doctype.user.user import User
from frappe.core.doctype.role.role import Role

from research_group.ai.security import native_identity_lock


class ResearchGroupUser(User):
    def check_if_latest(self):
        native_identity_lock(self)
        return super().check_if_latest()


class ResearchGroupRole(Role):
    def check_if_latest(self):
        native_identity_lock(self)
        return super().check_if_latest()
