"""固定来源schema；依赖由实际注入数据传播，模型不能提供或清洗来源。"""
import hashlib
import time

import frappe
from frappe.utils import get_datetime, now_datetime

from research_group.ai.security import current_roles
from research_group.ai.storage import dumps, loads
from research_group.policy import PUBLISHED, SECRETARY, STUDENT, TEACHER, deny

FIELDS = {
    "RG Member": ("user", "display_name", "active"),
    "RG Topic": ("title", "student", "direction", "priority", "status", "next_milestone", "target_date"),
    "RG Progress": ("topic", "student", "progress_date", "summary", "blockers", "next_milestone", "recorded_at"),
    "RG Appointment": ("topic", "student", "teacher", "start_at", "end_at", "agenda", "status"),
    "RG Discussion Record": ("topic", "student", "teacher", "discussed_at", "conclusion", "teacher_feedback",
                             "actions", "recorded_at", "recorded_by"),
    "RG Group Meeting": ("title", "start_at", "end_at", "deadline", "location", "status", "publisher"),
    "RG Meeting Report": ("meeting", "student", "topic", "latest_version", "last_submitted_at", "late"),
    "RG Material Version": ("report", "version", "report_topic", "original_filename", "submitted_at", "late",
                            "material_file", "sha256", "size_bytes"),
    "RG Survey": ("title", "description", "kind", "status", "deadline", "published_at", "closed_at", "questions"),
    "RG Survey Response": ("survey", "respondent", "answers_json", "submitted_at", "updated_at", "revision"),
    "RG AI Memory": ("private_user", "content", "kind", "status", "expires_at", "context_version", "sources"),
    "RG AI Plan": ("private_user", "topic", "content", "status", "expires_at", "context_version", "sources"),
    "RG Manuscript": ("title", "author", "reviewer", "topic", "status", "current_version", "revision"),
    "RG Manuscript Version": ("manuscript", "version", "parent_version", "kind", "original_filename",
                              "submitted_by", "submitted_at", "size_bytes", "sha256", "material_file", "upload_key"),
    "RG Manuscript Selection": ("private_user", "manuscript", "manuscript_version", "original_text", "sha256",
                                "version_sha256", "anchor", "extractor_version", "expires_at", "status"),
}
PRIVATE = {"RG Topic", "RG Progress", "RG Appointment", "RG Discussion Record"}
MAX_PRIVATE_DEPTH = 256


def validation_audit(user):
    return {"memo": {}, "heights": {}, "nodes": 0, "started": time.monotonic(), "roles": current_roles(user)}


def read_current(doctype, name, user=None, _stack=(), _audit=None):
    user = user or frappe.session.user
    if doctype not in FIELDS or not isinstance(name, str) or not 1 <= len(name) <= 140:
        deny()
    if _audit is None:
        _audit = validation_audit(user)
        _audit["nodes"] = 1
    assigned = _audit["roles"]
    try:
        doc = frappe.get_doc(doctype, name, for_update=True)
    except frappe.DoesNotExistError:
        deny()
    allowed = False
    if doctype in {"RG Manuscript", "RG Manuscript Version", "RG Manuscript Selection"}:
        from research_group.manuscripts import get_authorized_manuscript, get_authorized_version, read_selection
        reader = {"RG Manuscript": get_authorized_manuscript, "RG Manuscript Version": get_authorized_version,
                  "RG Manuscript Selection": read_selection}[doctype]
        doc = reader(name, user)
        allowed = True
    elif doctype in PRIVATE:
        allowed = TEACHER in assigned or (STUDENT in assigned and doc.student == user)
    elif doctype == "RG Member":
        allowed = True
    elif doctype == "RG Group Meeting":
        allowed = doc.status == PUBLISHED or bool(assigned & {TEACHER, SECRETARY})
    elif doctype == "RG Meeting Report":
        read_current("RG Group Meeting", doc.meeting, user)
        allowed = True
    elif doctype == "RG Material Version":
        read_current("RG Meeting Report", doc.report, user)
        allowed = True
    elif doctype == "RG Survey":
        allowed = SECRETARY in assigned or doc.status in {PUBLISHED, "已关闭"}
    elif doctype == "RG Survey Response":
        read_current("RG Survey", doc.survey, user)
        allowed = SECRETARY in assigned or doc.respondent == user
    elif doctype in {"RG AI Memory", "RG AI Plan"}:
        key = (doctype, name)
        if key in _stack:
            deny()
        if len(_stack) >= MAX_PRIVATE_DEPTH:
            frappe.throw("私人来源链达到有界深度，请缩小来源后重新生成。")
        allowed = doc.private_user == user and doc.site == frappe.local.site and doc.status not in {"deleted", "candidate"}
        if allowed and doc.expires_at and get_datetime(doc.expires_at) <= now_datetime():
            allowed = False
        if allowed:
            refs = loads(doc.sources)
            validate(refs, user, strict=True, _stack=(*_stack, key), _audit=_audit)
            # Memoized ancestors still contribute their full graph height.
            # Checking only the current DFS stack depends on traversal order.
            height = 1 + max((_audit["heights"].get((item["doctype"], item["name"]), 0) for item in refs), default=0)
            if height > MAX_PRIVATE_DEPTH:
                frappe.throw("私人来源链达到有界深度，请缩小来源后重新生成。")
            _audit["heights"][key] = height
    if not allowed:
        deny()
    return doc


def data(doc):
    return {"doctype": doc.doctype, "name": doc.name, "modified": str(doc.modified),
            **{field: doc.get(field) for field in FIELDS[doc.doctype]}}


def version(doc):
    return hashlib.sha256(dumps(data(doc)).encode("utf-8")).hexdigest()


def ref(doc):
    return {"doctype": doc.doctype, "name": doc.name, "version": version(doc)}


def union(*collections):
    result = {}
    for collection in collections:
        for item in collection or []:
            if not isinstance(item, dict) or set(item) != {"doctype", "name", "version"}:
                deny()
            key = (item["doctype"], item["name"])
            if key in result and result[key]["version"] != item["version"]:
                frappe.throw("来源在本轮中发生变化，请重新读取。", frappe.PermissionError)
            result[key] = item
    if len(result) > 512:
        frappe.throw("本轮来源过多，请缩小范围或分页查询。")
    return [result[key] for key in sorted(result)]


def validate(sources, user=None, strict=False, allow_private=True, _stack=(), _audit=None):
    if not isinstance(sources, list) or len(sources) > 512:
        if strict:
            deny()
        return False
    try:
        audit = _audit if _audit is not None else validation_audit(user)
        for item in union(sources):
            if (item["doctype"] not in FIELDS or not isinstance(item["version"], str)
                    or len(item["version"]) != 64
                    or (not allow_private and item["doctype"].startswith("RG AI "))):
                deny()
            key = (item["doctype"], item["name"])
            if key in _stack:
                deny()
            if key not in audit["memo"]:
                audit["nodes"] += 1
                if audit["nodes"] > 512 or time.monotonic() - audit["started"] > 10:
                    frappe.throw("来源验证达到本轮有界范围，请缩小上下文。")
                doc = read_current(item["doctype"], item["name"], user, _stack=_stack, _audit=audit)
                audit["memo"][key] = version(doc)
            if audit["memo"][key] != item["version"]:
                frappe.throw("来源已更新或过期，请重读后重新生成。", frappe.PermissionError)
        return True
    except (frappe.PermissionError, frappe.DoesNotExistError, frappe.ValidationError):
        if strict:
            raise
        # 在专用列表中只返回屏蔽标记，不回显原文、旧来源或异常详情。
        return False


def reject_cycle(target_doctype, target_name, sources, user=None):
    """编辑目标不能经其他私人条目再次依赖自身；失败保留原条目。"""
    target = (target_doctype, target_name)
    pending = [item for item in sources if (item["doctype"], item["name"]) != target]
    seen = set()
    started = time.monotonic()
    while pending:
        item = pending.pop()
        key = (item["doctype"], item["name"])
        if key == target:
            frappe.throw("该修改会形成记忆/计划来源循环，请在不引用其派生条目的会话中重新生成。")
        if key in seen or not item["doctype"].startswith("RG AI "):
            continue
        seen.add(key)
        if len(seen) > 512 or time.monotonic() - started > 10:
            frappe.throw("私人来源图过大，请缩小范围。")
        # 这里只遍历受控私有元数据；内容/权限/版本由调用方的validate一次复验。
        # 不逐个重新展开整个DAG，避免共享祖先导致平方/指数查询。
        doc = frappe.get_doc(item["doctype"], item["name"], for_update=True)
        if doc.private_user != (user or frappe.session.user) or doc.site != frappe.local.site:
            deny()
        pending.extend(loads(doc.sources))


def capture(doc):
    refs = [ref(doc)]
    if doc.doctype in {"RG AI Memory", "RG AI Plan"}:
        refs = union(refs, loads(doc.sources))
    if doc.doctype in {"RG Progress", "RG Discussion Record", "RG Appointment"}:
        refs = union(refs, capture(read_current("RG Topic", doc.topic)))
    elif doc.doctype == "RG Meeting Report":
        refs = union(refs, capture(read_current("RG Group Meeting", doc.meeting)))
    elif doc.doctype == "RG Material Version":
        refs = union(refs, capture(read_current("RG Meeting Report", doc.report)))
    elif doc.doctype == "RG Survey Response":
        refs = union(refs, capture(read_current("RG Survey", doc.survey)))
    elif doc.doctype == "RG Manuscript Version":
        refs = union(refs, capture(read_current("RG Manuscript", doc.manuscript)))
    elif doc.doctype == "RG Manuscript Selection":
        refs = union(refs, capture(read_current("RG Manuscript Version", doc.manuscript_version)))
    return refs


def business_dependencies(sources):
    # 私人条目的内容已经受本人隐私版本约束；复制仍保留全部正式来源。
    return [item for item in sources if not item["doctype"].startswith("RG AI ")]


def links(sources):
    result = []
    for item in sources:
        if item["doctype"].startswith("RG AI "):
            continue
        route = frappe.scrub(item["doctype"]).replace("_", "-")
        from urllib.parse import quote
        name = item["name"]
        if item["doctype"].startswith("RG Manuscript"):
            doc = read_current(item["doctype"], name)
            name = doc.name if doc.doctype == "RG Manuscript" else doc.manuscript
            route = "rg-manuscripts"
        result.append({"doctype": item["doctype"], "name": item["name"], "version": item["version"],
                       "url": "/desk/" + route + "/" + quote(name, safe="")})
    return result
