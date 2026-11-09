"""Private manuscript versions, append-only review history and explicit AI excerpts.

Only dedicated APIs expose these internal tables. All writes lock the shared
identity gate before a manuscript and its children; a workstation switch does
not affect this authorization. Original files are never overwritten.
"""
from datetime import timedelta
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import subprocess
import sys
import time

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime
from pypdf import PdfReader, apply_configuration
from pypdf.generic import DecodedStreamObject, EncodedStreamObject

from research_group.ai.security import current_roles, global_lock
from research_group.files import (
    DEFAULT_MAX_BYTES, MAX_MANUSCRIPT_PARAGRAPHS, MAX_MANUSCRIPT_TEXT, _validate_docx,
    max_bytes, read_stored_file_bytes, save_managed_file, validate_manuscript,
)
from research_group.policy import STUDENT, TEACHER, deny

MANUSCRIPT_DOCTYPES = (
    "RG Manuscript", "RG Manuscript Version", "RG Manuscript Review", "RG Manuscript Selection",
)
TOKEN = object()
EXTRACTOR_VERSION = "rg-excerpt-2"
MAX_SELECTION_CHARS = 6000
SELECTION_MINUTES = 30
MAX_ACTIVE_SELECTIONS = 20
MAX_REVIEW_STATUS_EVENTS = 20
MAX_REVIEW_STATUS_BYTES = 8192
PDF_STREAM_BYTES = 8 * 1024 * 1024
PDF_DECODED_BYTES = 32 * 1024 * 1024
PDF_PARSE_SECONDS = 4
PDF_PROCESS_TIMEOUT = 6
PDF_MEMORY_BYTES = 512 * 1024 * 1024
MANUSCRIPT_FIELDS = (
    "name", "title", "author", "reviewer", "topic", "status", "current_version", "revision",
)
VERSION_FIELDS = (
    "name", "manuscript", "version", "kind", "parent_version", "original_filename",
    "submitted_by", "submitted_at", "size_bytes", "sha256", "material_file",
)
REVIEW_FIELDS = (
    "name", "manuscript", "manuscript_version", "kind", "parent_review", "author", "content",
    "anchor", "status", "original_text", "reason", "source_selection", "source_sha256", "recorded_at",
)
SELECTION_FIELDS = (
    "name", "manuscript", "manuscript_version", "private_user", "original_text", "sha256",
    "version_sha256", "anchor", "extractor_version", "expires_at", "status",
)


def _write(allowed):
    from research_group.ai.api import browser_write
    browser_write(allowed)


def _identity(user=None):
    user = user or frappe.session.user
    if user != frappe.session.user:
        deny()
    global_lock()
    return current_roles(user)


def _text(value, maximum, label, required=True):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > maximum or "\x00" in value:
        frappe.throw(f"{label}须为不超过{maximum}字的文本。")
    return value.strip()


def _integer(value):
    if isinstance(value, bool):
        deny()
    try:
        number = int(value)
    except (ValueError, TypeError):
        deny()
    if str(number) != str(value) or number < 0:
        deny()
    return number


def _page(offset, limit, maximum=50):
    offset, limit = _integer(offset), _integer(limit)
    if offset > 10000 or not 1 <= limit <= maximum:
        deny()
    return offset, limit


def _row(doctype, name):
    if doctype not in MANUSCRIPT_DOCTYPES or not isinstance(name, str) or not 1 <= len(name) <= 140:
        deny()
    try:
        return frappe.get_doc(doctype, name, for_update=True)
    except frappe.DoesNotExistError:
        deny()


def _participant(doc, user, assigned):
    return ((doc.author == user and STUDENT in assigned)
            or (doc.reviewer == user and TEACHER in assigned))


def manuscript_for(doc):
    if doc.doctype == "RG Manuscript":
        return doc
    if doc.doctype not in MANUSCRIPT_DOCTYPES:
        deny()
    return frappe.get_doc("RG Manuscript", doc.manuscript)


def _read_roles(user):
    # File permission hooks must not reuse cached roles or an ordinary-read
    # snapshot. Match native identity mutation's gate -> User -> Member order.
    if user in {"Administrator", "Guest"}:
        return set()
    global_lock()
    return current_roles(user)


def can_read(doc, user=None):
    """File/list hook helper; internal document JSON remains denied separately."""
    user = user or frappe.session.user
    try:
        assigned = _read_roles(user)
        if (doc.doctype not in MANUSCRIPT_DOCTYPES
                or not _participant(manuscript_for(doc), user, assigned)):
            return False
        if doc.doctype == "RG Manuscript Selection":
            return (doc.private_user == user and doc.status == "active"
                    and get_datetime(doc.expires_at) > now_datetime())
        return True
    except (frappe.DoesNotExistError, frappe.PermissionError, ValueError, TypeError):
        return False


def permission_sql(doctype, user=None, assigned=None):
    """Escaped predicate usable by File's parent EXISTS branch."""
    if doctype not in MANUSCRIPT_DOCTYPES:
        return "1=0"
    user = user or frappe.session.user
    if assigned is None:
        try:
            assigned = _read_roles(user)
        except frappe.PermissionError:
            return "1=0"
    escaped = frappe.db.escape(user)
    terms = []
    if STUDENT in assigned:
        terms.append(f"m.author={escaped}")
    if TEACHER in assigned:
        terms.append(f"m.reviewer={escaped}")
    if not terms:
        return "1=0"
    alias = f"`tab{doctype}`"
    if doctype == "RG Manuscript":
        return "(" + " OR ".join(terms).replace("m.", alias + ".") + ")"
    condition = ("EXISTS (SELECT 1 FROM `tabRG Manuscript` m WHERE m.name=" + alias
                 + ".manuscript AND (" + " OR ".join(terms) + "))")
    if doctype == "RG Manuscript Selection":
        condition += (f" AND {alias}.private_user={escaped} AND {alias}.status='active' "
                      f"AND {alias}.expires_at>CURRENT_TIMESTAMP")
    return condition


def get_authorized_manuscript(name, user=None):
    user = user or frappe.session.user
    assigned = _identity(user)
    manuscript = _row("RG Manuscript", name)
    # Current identity is obtained after the gate, so a prior snapshot cannot
    # preserve a removed role while this request waits for another operation.
    if not _participant(manuscript, user, assigned):
        deny()
    return manuscript


def get_authorized_version(name, user=None):
    # Read only the immutable parent pointer before taking the parent lock.
    if not isinstance(name, str) or not 1 <= len(name) <= 140:
        deny()
    initial = frappe.db.get_value("RG Manuscript Version", name, "manuscript")
    if not initial:
        deny()
    manuscript = get_authorized_manuscript(initial, user)
    version = _row("RG Manuscript Version", name)
    if version.manuscript != manuscript.name:
        deny()
    return version


def _save(doc, new=False):
    doc.flags.rg_manuscript = TOKEN
    doc.flags.ignore_version = True
    if new:
        return doc.insert(ignore_permissions=True, set_name=doc.get("name"))
    return doc.save(ignore_permissions=True)


def _insert(doctype, **values):
    if doctype == "RG Manuscript Review":
        values["status_history"] = json.dumps({"complete": True, "events": []}, separators=(",", ":"))
    return _save(frappe.get_doc({"doctype": doctype, **values}), new=True)


def _public(doc, fields):
    return {field: str(doc.get(field)) if field.endswith("_at") and doc.get(field) else doc.get(field)
            for field in fields}


def _version_public(doc):
    result = _public(doc, VERSION_FIELDS)
    result.pop("material_file", None)
    if doc.material_file:
        file = frappe.get_doc("File", doc.material_file)
        if (file.attached_to_doctype != "RG Manuscript Version" or file.attached_to_name != doc.name
                or not file.is_private):
            deny()
        result["file_url"] = file.unique_url
    else:
        result["file_url"] = None
    return result


@frappe.whitelist(methods=["POST"])
def create_manuscript(title, reviewer, topic=None):
    _write({"title", "reviewer", "topic"})
    assigned = _identity()
    if STUDENT not in assigned:
        deny()
    title = _text(title, 200, "论文标题")
    if not isinstance(reviewer, str) or TEACHER not in current_roles(reviewer):
        deny()
    # current_roles intentionally accepts a target user; it still uses current
    # reads and cannot give the caller any of that target's authority.
    if topic:
        record = frappe.get_doc("RG Topic", topic, for_update=True)
        if record.student != frappe.session.user:
            deny()
    doc = _insert("RG Manuscript", title=title, author=frappe.session.user,
                  reviewer=reviewer, topic=topic, status="draft", current_version=0, revision=0)
    return _public(doc, MANUSCRIPT_FIELDS)


@frappe.whitelist()
def list_manuscripts(offset=0, limit=20):
    assigned = _identity()
    offset, limit = _page(offset, limit)
    rows = frappe.db.sql("SELECT " + ",".join("`" + field + "`" for field in MANUSCRIPT_FIELDS)
                         + " FROM `tabRG Manuscript` WHERE " + permission_sql("RG Manuscript", assigned=assigned)
                         + " ORDER BY modified DESC,name LIMIT %s OFFSET %s FOR UPDATE",
                         (limit + 1, offset), as_dict=True)
    return {"items": [_public(row, MANUSCRIPT_FIELDS) for row in rows[:limit]],
            "has_more": len(rows) > limit, "offset": offset}


@frappe.whitelist()
def get_manuscript(name):
    doc = get_authorized_manuscript(name)
    versions = frappe.db.sql("SELECT name FROM `tabRG Manuscript Version` WHERE manuscript=%s "
                             "ORDER BY version DESC LIMIT 101 FOR UPDATE", (name,), as_dict=True)
    reviews = frappe.db.sql("SELECT name FROM `tabRG Manuscript Review` WHERE manuscript=%s "
                            "ORDER BY recorded_at,creation,name LIMIT 201 FOR UPDATE", (name,), as_dict=True)
    review_memo = {}
    return {"manuscript": _public(doc, MANUSCRIPT_FIELDS),
            "versions": [_version_public(_row("RG Manuscript Version", row.name)) for row in versions[:100]],
            "reviews": [value for row in reviews[:200]
                        if (value := _review_public(_row("RG Manuscript Review", row.name), review_memo)) is not None],
            "more_versions": len(versions) > 100, "more_reviews": len(reviews) > 200}


@frappe.whitelist()
def list_versions(manuscript, offset=0, limit=50):
    doc = get_authorized_manuscript(manuscript)
    offset, limit = _page(offset, limit)
    rows = frappe.db.sql("SELECT name FROM `tabRG Manuscript Version` WHERE manuscript=%s "
                         "ORDER BY version DESC,name LIMIT %s OFFSET %s FOR UPDATE",
                         (doc.name, limit + 1, offset), as_dict=True)
    return {"items": [_version_public(_row("RG Manuscript Version", row.name)) for row in rows[:limit]],
            "has_more": len(rows) > limit, "offset": offset}


@frappe.whitelist()
def list_reviews(manuscript, offset=0, limit=50):
    doc = get_authorized_manuscript(manuscript)
    offset, limit = _page(offset, limit)
    rows = frappe.db.sql("SELECT name FROM `tabRG Manuscript Review` WHERE manuscript=%s "
                         "ORDER BY recorded_at,creation,name LIMIT %s OFFSET %s FOR UPDATE",
                         (doc.name, limit + 1, offset), as_dict=True)
    memo = {}
    return {"items": [value for row in rows[:limit]
                      if (value := _review_public(_row("RG Manuscript Review", row.name), memo)) is not None],
            "has_more": len(rows) > limit, "offset": offset}


def submit_version(manuscript, expected_revision, filename, content, kind="student_draft", parent_version=None):
    """Byte entry, not a model tool or whitelisted method."""
    doc = get_authorized_manuscript(manuscript)
    user, assigned = frappe.session.user, current_roles()
    if kind not in {"student_draft", "teacher_annotation"}:
        deny()
    if ((kind == "student_draft" and (doc.author != user or STUDENT not in assigned))
            or (kind == "teacher_annotation" and (doc.reviewer != user or TEACHER not in assigned))):
        deny()
    revision = _integer(expected_revision)
    validate_manuscript(filename, content)
    digest = hashlib.sha256(content).hexdigest()
    key = hashlib.sha256(json.dumps([doc.name, user, revision, filename, digest, kind, parent_version],
                                   ensure_ascii=False).encode("utf-8")).hexdigest()
    prior = frappe.db.sql("SELECT name FROM `tabRG Manuscript Version` WHERE upload_key=%s FOR UPDATE",
                           (key,), as_dict=True)
    if prior:
        return _version_public(_row("RG Manuscript Version", prior[0].name))
    if revision != int(doc.revision or 0):
        frappe.throw("论文已更新，请刷新版本后重新提交；原有版本未被覆盖。")
    if parent_version:
        parent = _row("RG Manuscript Version", parent_version)
        if parent.manuscript != doc.name:
            deny()
    else:
        parents = frappe.db.sql("SELECT name FROM `tabRG Manuscript Version` WHERE manuscript=%s "
                                "ORDER BY version DESC LIMIT 1 FOR UPDATE", (doc.name,))
        parent_version = parents[0][0] if parents else None
    submitted_at = now_datetime()
    number = int(doc.current_version or 0) + 1
    savepoint = "rg_manuscript_" + frappe.generate_hash(length=8)
    frappe.db.savepoint(savepoint)
    created_files = []
    try:
        version = _insert("RG Manuscript Version", manuscript=doc.name, version=number,
                          kind=kind, parent_version=parent_version, original_filename=filename,
                          submitted_by=user, submitted_at=submitted_at, size_bytes=len(content),
                          sha256=digest, upload_key=key)
        file = save_managed_file(filename, content, "RG Manuscript Version", version.name,
                                 created_files=created_files)
        stored = read_stored_file_bytes(file, max_read_bytes=len(content))
        if len(stored) != len(content) or hashlib.sha256(stored).hexdigest() != digest:
            frappe.throw("论文落盘原字节校验失败。")
        frappe.db.set_value("RG Manuscript Version", version.name, "material_file", file.name,
                            update_modified=False)
        version.material_file = file.name
        doc.current_version, doc.revision = number, revision + 1
        doc.status = "under_review" if kind == "student_draft" else "revision_requested"
        _save(doc)
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        from research_group.services import _cleanup_created_material_files
        _cleanup_created_material_files(created_files)
        raise
    return _version_public(version)


@frappe.whitelist(methods=["POST"])
def upload_version(manuscript, expected_revision, kind="student_draft", parent_version=None):
    _write({"manuscript", "expected_revision", "kind", "parent_version"})
    get_authorized_manuscript(manuscript)  # authorization precedes multipart reading
    if not getattr(frappe.local, "request", None) or "file" not in frappe.request.files:
        frappe.throw("请选择PDF或DOCX文件。")
    stream = frappe.request.files["file"]
    return submit_version(manuscript, expected_revision, stream.filename, stream.stream.read(max_bytes() + 1),
                          kind=kind, parent_version=parent_version)


def _saved_suggestion_valid(doc, memo):
    """Natural selection expiry ends new AI reads; explicit revocation hides history.

    Confirmed reviews are durable history of an authorized immutable version.
    Recheck source bindings and bytes on every read, memoizing each file only
    within this request so many reviews cannot cause repeated 50 MiB reads.
    """
    try:
        source = _row("RG Manuscript Selection", doc.source_selection)
        version = _row("RG Manuscript Version", doc.manuscript_version)
        manuscript = _row("RG Manuscript", doc.manuscript)
        key = ("roles", source.private_user)
        if key not in memo:
            memo[key] = _read_roles(source.private_user)
        if (source.status == "revoked" or source.status not in {"active", "expired"}
                or source.private_user != doc.author or not _participant(manuscript, source.private_user, memo[key])
                or source.manuscript != doc.manuscript or source.manuscript_version != version.name
                or version.manuscript != doc.manuscript or source.version_sha256 != version.sha256
                or source.extractor_version != EXTRACTOR_VERSION
                or doc.source_sha256 != source.sha256 or doc.original_text != source.original_text
                or hashlib.sha256(source.original_text.encode("utf-8")).hexdigest() != source.sha256):
            return False
        key = ("version", version.name)
        if key not in memo:
            file = frappe.get_doc("File", version.material_file)
            if (not file.is_private or file.attached_to_doctype != "RG Manuscript Version"
                    or file.attached_to_name != version.name):
                memo[key] = False
            else:
                data = read_stored_file_bytes(file, max_read_bytes=max_bytes())
                memo[key] = len(data) == version.size_bytes and hashlib.sha256(data).hexdigest() == version.sha256
        return memo[key]
    except (frappe.DoesNotExistError, frappe.PermissionError, frappe.ValidationError, OSError, ValueError, TypeError):
        return False


def _review_status_history(doc):
    """Only bounded state metadata lives here; old rows have no invented past."""
    raw = doc.get("status_history")
    if not raw:
        return {"complete": False, "events": []}
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_REVIEW_STATUS_BYTES:
        deny()
    try:
        history = json.loads(raw)
        if (not isinstance(history, dict) or set(history) != {"complete", "events"}
                or type(history["complete"]) is not bool or not isinstance(history["events"], list)
                or len(history["events"]) > MAX_REVIEW_STATUS_EVENTS):
            deny()
        for event in history["events"]:
            if (not isinstance(event, dict) or set(event) != {"actor", "at", "before", "after", "action"}
                    or not isinstance(event["actor"], str) or not 1 <= len(event["actor"]) <= 140
                    or not isinstance(event["at"], str) or not 1 <= len(event["at"]) <= 40
                    or event["before"] not in {"open", "addressed"}
                    or event["after"] not in {"addressed", "closed"}
                    or event["before"] == event["after"] or event["action"] not in {"reply", "resolve"}):
                deny()
            get_datetime(event["at"])
        return history
    except (ValueError, TypeError, KeyError):
        deny()


def _change_review_status(doc, status, action):
    if doc.status == status:
        return
    history = _review_status_history(doc)
    if len(history["events"]) >= MAX_REVIEW_STATUS_EVENTS:
        frappe.throw("本条意见的状态记录已达到容量上限，请联系管理员核对；已有记录保持不变。")
    history["events"].append({"actor": frappe.session.user, "at": str(now_datetime()),
                              "before": doc.status, "after": status, "action": action})
    encoded = json.dumps(history, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REVIEW_STATUS_BYTES:
        frappe.throw("状态记录超过存储容量；已有记录保持不变。")
    doc.status, doc.status_history = status, encoded
    _save(doc)


def _review_public(doc, memo=None):
    memo = memo if memo is not None else {}
    blocked = doc.kind == "ai_suggestion" and not _saved_suggestion_valid(doc, memo)
    if doc.kind == "student_reply" and doc.parent_review:
        parent = _row("RG Manuscript Review", doc.parent_review)
        if parent.kind == "ai_suggestion" and not _saved_suggestion_valid(parent, memo):
            blocked = True
    if blocked:
        # Keep a body-free slot so history/pagination cannot accidentally skip
        # or duplicate later rows when an earlier source has been revoked.
        return {"name": doc.name, "manuscript": doc.manuscript,
                "manuscript_version": doc.manuscript_version, "kind": doc.kind,
                "status": doc.status, "recorded_at": str(doc.recorded_at), "blocked": True,
                "notice": "来源已撤销或无法验证，相关正文已屏蔽。"}
    result = _public(doc, REVIEW_FIELDS)
    history = _review_status_history(doc)
    result.update(status_history=history["events"], status_history_complete=history["complete"])
    if not history["complete"]:
        result["status_history_notice"] = "旧记录未保留早期状态变化，仅展示升级后实际记录的事件。"
    return result


@frappe.whitelist(methods=["POST"])
def add_review(version, content, anchor=""):
    _write({"version", "content", "anchor"})
    record = get_authorized_version(version)
    manuscript = get_authorized_manuscript(record.manuscript)
    if manuscript.reviewer != frappe.session.user or TEACHER not in current_roles():
        deny()
    review = _insert("RG Manuscript Review", manuscript=manuscript.name, manuscript_version=record.name,
                     kind="teacher_comment", author=frappe.session.user,
                     content=_text(content, 10000, "教师意见"), anchor=_text(anchor, 300, "位置", False),
                     status="open", recorded_at=now_datetime())
    manuscript.status = "revision_requested"
    _save(manuscript)
    return _review_public(review)


def _get_review(name):
    initial = frappe.db.get_value("RG Manuscript Review", name, "manuscript")
    if not initial:
        deny()
    get_authorized_manuscript(initial)
    record = _row("RG Manuscript Review", name)
    if _review_public(record).get("blocked"):
        deny()
    return record


@frappe.whitelist(methods=["POST"])
def reply_review(review, content):
    _write({"review", "content"})
    parent = _get_review(review)
    manuscript = get_authorized_manuscript(parent.manuscript)
    if manuscript.author != frappe.session.user or STUDENT not in current_roles():
        deny()
    if parent.kind not in {"teacher_comment", "ai_suggestion"}:
        frappe.throw("请回复一条教师意见或已保存的AI建议。")
    content = _text(content, 10000, "学生回复")
    savepoint = "rg_review_reply_" + frappe.generate_hash(length=8)
    frappe.db.savepoint(savepoint)
    try:
        reply = _insert("RG Manuscript Review", manuscript=parent.manuscript,
                        manuscript_version=parent.manuscript_version, parent_review=parent.name,
                        kind="student_reply", author=frappe.session.user, content=content,
                        status="addressed", recorded_at=now_datetime())
        if parent.status != "closed":
            _change_review_status(parent, "addressed", "reply")
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return _review_public(reply)


@frappe.whitelist(methods=["POST"])
def resolve_review(review, status="addressed"):
    _write({"review", "status"})
    record = _get_review(review)
    manuscript = get_authorized_manuscript(record.manuscript)
    assigned = current_roles()
    if record.kind == "student_reply" or status not in {"addressed", "closed"}:
        deny()
    allowed = ((status == "addressed" and manuscript.author == frappe.session.user and STUDENT in assigned)
               or (status == "closed" and manuscript.reviewer == frappe.session.user and TEACHER in assigned))
    if not allowed or (record.status == "closed" and status != "closed"):
        deny()
    _change_review_status(record, status, "resolve")
    return _review_public(record)


def _normalize(value):
    return re.sub(r"\s+", " ", value).strip()


class _PDFBudget:
    def __init__(self):
        self.deadline = time.monotonic() + PDF_PARSE_SECONDS
        self.decoded_bytes = 0
        # Retain identities so an object ID cannot be reused to evade charging.
        self.streams = {}
        self.depth = 0

    def check(self):
        if time.monotonic() > self.deadline:
            raise ValueError("PDF extraction time budget")

    def remaining(self, stream):
        self.check()
        if id(stream) in self.streams:
            return PDF_STREAM_BYTES
        remaining = PDF_DECODED_BYTES - self.decoded_bytes
        if remaining <= 0:
            raise ValueError("PDF cumulative decoded-byte budget")
        return min(PDF_STREAM_BYTES, remaining)

    def charge(self, stream, data):
        self.check()
        if len(data) > PDF_STREAM_BYTES:
            raise ValueError("PDF stream decoded-byte budget")
        if id(stream) not in self.streams:
            if len(data) > PDF_DECODED_BYTES - self.decoded_bytes:
                raise ValueError("PDF cumulative decoded-byte budget")
            self.streams[id(stream)] = stream
            self.decoded_bytes += len(data)


def _pdf_configuration(limit=PDF_STREAM_BYTES):
    # These context-local limits are installed before PdfReader reads even the
    # cross-reference/object streams, not after get_data() allocates its output.
    return {
        "maximum_declared_stream_length": PDF_STREAM_BYTES,
        "array_based_stream_maximum_output_length": PDF_STREAM_BYTES,
        "zlib_maximum_output_length": limit, "lzw_maximum_output_length": limit,
        "run_length_maximum_output_length": limit, "jbig2_maximum_output_length": limit,
        "image_maximum_buffer_size": limit, "xmp_maximum_input_length": limit,
        "zlib_maximum_recovery_input_length": min(limit, 1024 * 1024),
        "flate_maximum_columns": 65536, "flate_maximum_row_length": min(limit, 1024 * 1024),
        "page_tree_maximum_entries": 5000, "page_tree_maximum_depth": 50,
        "xform_maximum_invocations_per_extraction": 1000,
        "disable_legacy_handling": True, "jbig2dec_binary": None,
    }


@contextmanager
def _pdf_stream_budget(budget):
    """Charge all streams, including fonts/forms/xrefs, in the isolated worker.

    Method replacement is deliberately confined to a single-purpose subprocess;
    web threads never patch shared pypdf classes while serving another request.
    """
    originals = {cls: cls.get_data for cls in (EncodedStreamObject, DecodedStreamObject)}

    def wrap(original):
        def bounded(stream):
            budget.check()
            if budget.depth:
                # EncodedStreamObject delegates to its decoded cache; charge
                # the outer stream once rather than charging that cache twice.
                return original(stream)
            limit = budget.remaining(stream)
            budget.depth += 1
            try:
                with apply_configuration(**_pdf_configuration(limit)):
                    data = original(stream)
            finally:
                budget.depth -= 1
            budget.charge(stream, data)
            return data
        return bounded

    try:
        for cls, original in originals.items():
            cls.get_data = wrap(original)
        yield
    finally:
        for cls, original in originals.items():
            cls.get_data = original


def _extract_pdf_paragraphs(content, budget=None, validate_only=False):
    """Pure parser entry. Production calls this only in its bounded worker."""
    budget = budget or _PDFBudget()
    with apply_configuration(**_pdf_configuration()), _pdf_stream_budget(budget):
        budget.check()
        pdf = PdfReader(BytesIO(content), strict=True)
        page_count = len(pdf.pages)
        if pdf.is_encrypted or not page_count:
            raise ValueError("PDF encrypted or empty")
        budget.check()
        if validate_only:
            return [], False
        paragraphs, size, truncated = [], 0, page_count > 200
        for page_index in range(min(page_count, 200)):
            budget.check()
            page = pdf.pages[page_index]
            # ContentStream construction also checks concatenated array output.
            stream = page.get_contents()
            if stream is not None and len(stream.get_data()) > PDF_STREAM_BYTES:
                raise ValueError("PDF page decoded-byte budget")
            text = page.extract_text() or ""
            budget.check()
            for paragraph in re.split(r"\n\s*\n", text):
                paragraph = paragraph.strip()
                if not paragraph:
                    continue
                if (size + len(paragraph) > MAX_MANUSCRIPT_TEXT
                        or len(paragraphs) >= MAX_MANUSCRIPT_PARAGRAPHS):
                    return paragraphs, True
                paragraphs.append(paragraph)
                size += len(paragraph)
        budget.check()
        return paragraphs, truncated


def _pdf_parse_worker(validate_only=False):
    """Fixed stdin/stdout protocol, without database, network or file writes."""
    result = {"ok": False}
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (PDF_MEMORY_BYTES, PDF_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (PDF_PARSE_SECONDS + 1, PDF_PARSE_SECONDS + 1))
        content = sys.stdin.buffer.read(DEFAULT_MAX_BYTES + 1)
        if not content or len(content) > DEFAULT_MAX_BYTES:
            raise ValueError("PDF input-byte budget")
        paragraphs, truncated = _extract_pdf_paragraphs(content, validate_only=validate_only)
        result = {"ok": True, "paragraphs": paragraphs, "truncated": truncated}
    except Exception:
        # Parser messages can contain manuscript bytes. The caller receives no
        # traceback, PDF fragments or arbitrary stderr from this process.
        pass
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def _parse_pdf_isolated(content, validate_only=False):
    # Both entry points are fixed source, never caller-provided command text.
    entry = ("from research_group.manuscripts import _pdf_parse_worker; _pdf_parse_worker(validate_only=True)"
             if validate_only else
             "from research_group.manuscripts import _pdf_parse_worker; _pdf_parse_worker()")
    command = [sys.executable, "-c", entry]
    try:
        process = subprocess.run(command, input=content, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=PDF_PROCESS_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("PDF worker unavailable or time budget") from None
    if process.returncode or len(process.stdout) > MAX_MANUSCRIPT_TEXT * 6 + 65536:
        raise ValueError("PDF worker resource budget")
    result = json.loads(process.stdout)
    paragraphs = result.get("paragraphs")
    if (result.get("ok") is not True or not isinstance(paragraphs, list)
            or len(paragraphs) > MAX_MANUSCRIPT_PARAGRAPHS
            or any(not isinstance(value, str) for value in paragraphs)
            or sum(map(len, paragraphs)) > MAX_MANUSCRIPT_TEXT
            or type(result.get("truncated")) is not bool):
        raise ValueError("PDF worker rejected input")
    return paragraphs, result["truncated"]


def _version_paragraphs(record):
    file = frappe.get_doc("File", record.material_file)
    if (file.attached_to_doctype != "RG Manuscript Version" or file.attached_to_name != record.name
            or not file.is_private):
        deny()
    content = read_stored_file_bytes(file, max_read_bytes=max_bytes())
    if len(content) != record.size_bytes or hashlib.sha256(content).hexdigest() != record.sha256:
        frappe.throw("论文原文件完整性已变化，选段与AI处理已阻止。", frappe.PermissionError)
    if PurePosixPath(record.original_filename).suffix.lower() == ".docx":
        return _validate_docx(content), False
    try:
        return _parse_pdf_isolated(content)
    except (frappe.ValidationError, frappe.PermissionError):
        raise
    except Exception:
        frappe.throw("PDF无法安全提取文字，请使用DOCX版本或另传可提取文字的PDF。")


@frappe.whitelist()
def get_version_text(version, offset=0, limit=50):
    record = get_authorized_version(version)
    offset, limit = _page(offset, limit)
    paragraphs, truncated = _version_paragraphs(record)
    return {"version": record.name, "paragraphs": [
        {"index": index, "text": paragraphs[index]} for index in range(offset, min(offset + limit, len(paragraphs)))
    ], "offset": offset, "has_more": offset + limit < len(paragraphs), "truncated": truncated,
        "extractor_version": EXTRACTOR_VERSION}


@frappe.whitelist(methods=["POST"])
def create_selection(version, original_text, anchor="pasted"):
    _write({"version", "original_text", "anchor"})
    record = get_authorized_version(version)
    original = _text(original_text, MAX_SELECTION_CHARS, "选定原文")
    anchor = _text(anchor, 300, "选区位置")
    paragraphs, _ = _version_paragraphs(record)
    if _normalize(original) not in _normalize("\n\n".join(paragraphs)):
        frappe.throw("选定原文与该不可变论文版本不匹配，请从显示的文字重新选取。")
    # Bounded active excerpts prevent creating an unbounded second document store.
    active = frappe.db.sql("SELECT name FROM `tabRG Manuscript Selection` WHERE private_user=%s "
                           "AND status='active' AND expires_at>%s FOR UPDATE",
                           (frappe.session.user, now_datetime()))
    if len(active) >= MAX_ACTIVE_SELECTIONS:
        frappe.throw("最多保留20个有效选段，请在论文页“本人授权选区”撤销旧选段；选段也会在30分钟后自然到期。")
    selection = _insert("RG Manuscript Selection", manuscript=record.manuscript, manuscript_version=record.name,
                        private_user=frappe.session.user, original_text=original,
                        sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(), version_sha256=record.sha256,
                        anchor=anchor, extractor_version=EXTRACTOR_VERSION,
                        expires_at=now_datetime() + timedelta(minutes=SELECTION_MINUTES), status="active")
    return _public(selection, SELECTION_FIELDS)


def read_selection(name, user=None):
    """The only model-facing manuscript read: an explicit, owner-scoped excerpt."""
    user = user or frappe.session.user
    if user != frappe.session.user:
        deny()
    initial = frappe.db.get_value("RG Manuscript Selection", name, ["manuscript", "manuscript_version"], as_dict=True)
    if not initial:
        deny()
    get_authorized_manuscript(initial.manuscript, user)
    version = get_authorized_version(initial.manuscript_version, user)
    selection = _row("RG Manuscript Selection", name)
    if (selection.private_user != user or selection.status != "active"
            or get_datetime(selection.expires_at) <= now_datetime()
            or selection.manuscript_version != version.name or selection.manuscript != version.manuscript
            or selection.version_sha256 != version.sha256
            or selection.extractor_version != EXTRACTOR_VERSION
            or hashlib.sha256(selection.original_text.encode("utf-8")).hexdigest() != selection.sha256):
        deny()
    # Recheck persisted bytes too, so deletion/corruption cannot leave an excerpt
    # apparently authorized merely because its old database row still exists.
    file = frappe.get_doc("File", version.material_file)
    if (file.attached_to_doctype != "RG Manuscript Version" or file.attached_to_name != version.name
            or not file.is_private):
        deny()
    stored = read_stored_file_bytes(file, max_read_bytes=max_bytes())
    if len(stored) != version.size_bytes or hashlib.sha256(stored).hexdigest() != version.sha256:
        deny()
    return selection


@frappe.whitelist()
def get_selection(name):
    return _public(read_selection(name), SELECTION_FIELDS)


@frappe.whitelist()
def list_selections(manuscript=None, offset=0, limit=20):
    """Owner-only management view, including expired sources of saved advice.

    This is not a model read. It never grants a fresh AI authorization and never
    returns a paragraph from another participant or an unreadable manuscript.
    """
    if (getattr(frappe.local, "request", None) is not None
            and set(frappe.form_dict) - {"manuscript", "offset", "limit", "cmd", "_", "_lang"}):
        deny()
    assigned = _identity()
    offset, limit = _page(offset, limit)
    params = [frappe.session.user]
    condition = ""
    if manuscript:
        manuscript = get_authorized_manuscript(manuscript).name
        condition = " AND s.manuscript=%s"
        params.append(manuscript)
    rows = frappe.db.sql(
        "SELECT s.name,`tabRG Manuscript`.title AS manuscript_title,v.version AS version_number "
        "FROM `tabRG Manuscript Selection` s "
        "JOIN `tabRG Manuscript` ON `tabRG Manuscript`.name=s.manuscript "
        "JOIN `tabRG Manuscript Version` v ON v.name=s.manuscript_version AND v.manuscript=s.manuscript "
        "WHERE s.private_user=%s AND s.status IN ('active','expired')" + condition
        + " AND " + permission_sql("RG Manuscript", assigned=assigned)
        + " ORDER BY s.creation DESC,s.name LIMIT %s OFFSET %s FOR UPDATE",
        (*params, limit + 1, offset), as_dict=True)
    now = now_datetime()
    active_count = len(frappe.db.sql(
        "SELECT name FROM `tabRG Manuscript Selection` WHERE private_user=%s "
        "AND status='active' AND expires_at>%s FOR UPDATE", (frappe.session.user, now)))
    items = []
    for row in rows[:limit]:
        selection = _row("RG Manuscript Selection", row.name)
        item = _public(selection, SELECTION_FIELDS)
        item.update(manuscript_title=row.manuscript_title, version_number=row.version_number,
                    effective_status="active" if selection.status == "active"
                    and get_datetime(selection.expires_at) > now else "expired")
        item["saved_suggestions"] = frappe.db.count("RG Manuscript Review", {
            "source_selection": selection.name, "manuscript": selection.manuscript,
            "manuscript_version": selection.manuscript_version,
            "author": frappe.session.user, "kind": "ai_suggestion"})
        items.append(item)
    return {"items": items, "has_more": len(rows) > limit, "offset": offset,
            "active_count": active_count, "active_limit": MAX_ACTIVE_SELECTIONS}


@frappe.whitelist(methods=["POST"])
def revoke_selection(name):
    _write({"name"})
    # Revocation remains available after natural expiry.
    initial = frappe.db.get_value("RG Manuscript Selection", name, "manuscript")
    get_authorized_manuscript(initial)
    selection = _row("RG Manuscript Selection", name)
    if selection.private_user != frappe.session.user:
        deny()
    selection.status = "revoked"
    _save(selection)
    return {"name": selection.name, "status": selection.status}


def save_ai_suggestion(selection, suggestion, reason="", operation_id=None):
    """Called only by the existing independent, idempotent confirmation transaction."""
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 140:
        deny()
    source = read_selection(selection)
    content = _text(suggestion, 10000, "AI修改建议")
    reason = _text(reason, 4000, "AI建议理由", False)
    name = hashlib.sha256(("manuscript-suggestion:" + operation_id).encode()).hexdigest()[:32]
    found = frappe.db.sql("SELECT name FROM `tabRG Manuscript Review` WHERE name=%s FOR UPDATE", (name,))
    if found:
        prior = _row("RG Manuscript Review", name)
        if (prior.ai_operation != operation_id or prior.source_selection != source.name
                or prior.author != frappe.session.user or prior.content != content or (prior.reason or "") != reason):
            deny()
        return _review_public(prior)
    review = _insert("RG Manuscript Review", name=name, manuscript=source.manuscript,
                     manuscript_version=source.manuscript_version, kind="ai_suggestion",
                     author=frappe.session.user, content=content, original_text=source.original_text,
                     reason=reason, source_selection=source.name, source_sha256=source.sha256,
                     anchor=source.anchor, ai_operation=operation_id, status="open", recorded_at=now_datetime())
    return _review_public(review)


class ManuscriptDocument(Document):
    """No generic save, delete, realtime broadcast or hidden Version copies."""
    def autoname(self):
        if not self.name:
            self.name = frappe.generate_hash(length=32)

    def check_if_latest(self):
        global_lock()
        return super().check_if_latest()

    def validate(self):
        if self.flags.get("rg_manuscript") is not TOKEN:
            deny()
        if self.is_new():
            return
        old = self.get_doc_before_save()
        mutable = {"RG Manuscript": {"status", "current_version", "revision"},
                   "RG Manuscript Review": {"status", "status_history"},
                   "RG Manuscript Selection": {"status"}}
        allowed = mutable.get(self.doctype, set())
        for field in self.meta.fields:
            if field.fieldname not in allowed and self.get(field.fieldname) != old.get(field.fieldname):
                deny()
        if self.doctype == "RG Manuscript Version":
            deny()
        if self.doctype == "RG Manuscript Review":
            before, after = _review_status_history(old), _review_status_history(self)
            if self.status == old.status:
                if self.status_history != old.status_history:
                    deny()
            elif (after["complete"] != before["complete"]
                    or after["events"][:-1] != before["events"]
                    or len(after["events"]) != len(before["events"]) + 1
                    or after["events"][-1]["before"] != old.status
                    or after["events"][-1]["after"] != self.status
                    or after["events"][-1]["actor"] != frappe.session.user):
                deny()

    def on_trash(self):
        deny()

    def notify_update(self):
        return
