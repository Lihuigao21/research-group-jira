"""专用写入令牌、正文配额和无正文的删除清单。"""
import hashlib
import json
import os
from pathlib import Path

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from research_group.ai import AI_DOCTYPES, TOKEN
from research_group.policy import deny

JOURNAL_BYTES = 65536


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def loads(value, default=None):
    if not value:
        return default if default is not None else []
    try:
        def unique_pairs(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    deny()
                result[key] = item
            return result
        return json.loads(value, object_pairs_hook=unique_pairs)
    except (TypeError, ValueError):
        deny()


class PrivateDocument(Document):
    def autoname(self):
        if not self.name:
            self.name = frappe.generate_hash(length=32)

    def validate(self):
        if self.flags.get("rg_ai") is not TOKEN:
            deny()
        if self.site != frappe.local.site:
            deny()
        if not self.is_new() and self.private_user != self.get_doc_before_save().private_user:
            deny()

    def on_trash(self):
        # 专用删除走清空正文/删除清单，不让 Deleted Document 保存副本。
        if self.flags.get("rg_ai") is not TOKEN:
            deny()

    def notify_update(self):
        # Private UI polls owner-scoped endpoints. Native doctype rooms would
        # broadcast list_update with private IDs/user metadata to other readers.
        return


def save(doc, new=False):
    doc.flags.rg_ai = TOKEN
    doc.flags.ignore_version = True
    return doc.insert(ignore_permissions=True, ignore_links=True, set_name=doc.get("name")) if new else doc.save(ignore_permissions=True)


def insert(doctype, user, **values):
    return save(frappe.get_doc({"doctype": doctype, "site": frappe.local.site,
                               "private_user": user, **values}), new=True)


def no_permission(doc=None, ptype=None, user=None, **kwargs):
    # 即便本人也使用专用接口，以便逐条验证派生来源；禁止通用CRUD/导出。
    return False


def no_query(user=None):
    return "1=0"


def require_capacity(user, addition=0):
    """调用方持有站点闸门；当前读实际UTF-8正文，包含历史和工具回执。"""
    from research_group.ai.config import limits

    columns = {
        "RG AI Conversation": ("title",), "RG AI Message": ("content", "sources"),
        "RG AI Memory": ("content", "sources"), "RG AI Plan": ("content", "sources"),
        "RG AI Operation": ("parameters", "preview", "sources"),
    }
    own = total = 0
    for doctype, fields in columns.items():
        expression = "+".join(f"OCTET_LENGTH(COALESCE(`{field}`,''))" for field in fields)
        # 锁定当前行，避免REPEATABLE-READ旧快照；不读取/输出正文。
        for row in frappe.db.sql(
            f"SELECT private_user, {expression} AS bytes FROM `tab{doctype}` FOR UPDATE", as_dict=True,
        ):
            total += row.bytes or 0
            if row.private_user == user:
                own += row.bytes or 0
    cap = limits()
    if addition > 0 and (own + addition > cap["user_bytes"] or total + addition > cap["site_bytes"]):
        frappe.throw("AI文本存储额度已满，请先清理本人历史、记忆或计划。")
    return {"user_bytes": own, "site_bytes": total, "user_limit": cap["user_bytes"]}


def journal_targets(user, targets):
    """恢复前须重放最新清单；清单不含被删除正文或其内容摘要。"""
    if not targets:
        return
    entry = {"site": frappe.local.site, "user": user, "deleted_at": str(now_datetime()), "targets": []}
    overhead = len((dumps(entry) + "\n").encode("utf-8"))
    path = Path(frappe.get_site_path("private", "rg-ai-deletions.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        def flush_batch():
            payload = (dumps(entry) + "\n").encode("utf-8")
            if len(payload) > JOURNAL_BYTES:
                frappe.throw("删除清单条目过大。")
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

        size = overhead
        for doctype, name in targets:
            item = {"doctype": doctype, "name": name}
            item_size = len(dumps(item).encode("utf-8"))
            if overhead + item_size > JOURNAL_BYTES:
                frappe.throw("删除清单条目过大。")
            if entry["targets"] and size + item_size + 1 > JOURNAL_BYTES:
                flush_batch()
                entry["targets"] = []
                size = overhead
            size += item_size + bool(entry["targets"])
            entry["targets"].append(item)
        flush_batch()
    for doctype, name in targets:
        identifier = hashlib.sha256((doctype + ":" + name).encode()).hexdigest()[:32]
        if not frappe.db.exists("RG AI Tombstone", identifier):
            insert("RG AI Tombstone", user, name=identifier, target_doctype=doctype,
                   target_name=name, deleted_at=now_datetime())


def clear_target(doctype, name, user):
    if doctype not in {"RG AI Conversation", "RG AI Memory", "RG AI Plan"}:
        deny()
    rows = frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE name=%s AND private_user=%s FOR UPDATE",
                         (name, user))
    if not rows:
        return
    if doctype == "RG AI Conversation":
        frappe.db.delete("RG AI Message", {"conversation": name, "private_user": user})
        # Shared by online deletion and restore replay: historical statuses must
        # never retain an alternate copy of the deleted conversation body.
        frappe.db.sql("UPDATE `tabRG AI Operation` SET parameters='',preview='',sources='[]' "
                      "WHERE conversation=%s AND private_user=%s", (name, user))
        # Preserve only the run's existing budget/status ledger, not the
        # request's explicit manuscript selection metadata after deletion.
        frappe.db.sql("UPDATE `tabRG AI Run` SET selection_ids='[]' "
                      "WHERE conversation=%s AND private_user=%s", (name, user))
        frappe.db.set_value(doctype, name, {"title": "已删除", "status": "deleted"}, update_modified=False)
    else:
        frappe.db.set_value(doctype, name, {"content": "", "sources": "[]", "status": "deleted"},
                            update_modified=False)
    frappe.db.sql(f"UPDATE `tab{doctype}` SET context_version=context_version+1 WHERE name=%s", (name,))


def replay_deletions(path=None):
    """仅运维CLI：维护模式下用独立保留的最新清单重放，再开放站点。"""
    if frappe.session.user != "Administrator":
        deny()
    source = Path(path or frappe.get_site_path("private", "rg-ai-deletions.jsonl"))
    if not source.is_file():
        frappe.throw("未找到删除清单，恢复站点应继续保持维护模式。")
    from research_group.ai.security import global_lock, invalidate

    global_lock()
    count = 0
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            if len(line.encode("utf-8")) > JOURNAL_BYTES:
                frappe.throw("删除清单条目过大。")
            entry = loads(line, {})
            if entry.get("site") != frappe.local.site:
                # 独立恢复站点允许运维明确指定旧site名，不按用户名猜来源。
                original = frappe.conf.get("rg_ai_restore_source_site")
                if not original or entry.get("site") != original:
                    frappe.throw("删除清单站点绑定不匹配。")
            user = entry["user"]
            for item in entry["targets"]:
                clear_target(item["doctype"], item["name"], user)
                count += 1
            invalidate(user)
    return {"applied_targets": count, "contains_content": False}


def guard_native(doctype):
    if doctype in AI_DOCTYPES:
        deny()
