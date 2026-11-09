"""Managed attachments always stay private and keep their original parent."""
import hashlib
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import struct
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile
from xml.etree import ElementTree

import frappe
import olefile
from frappe.core.doctype.file.file import File
from frappe.core.doctype.file.utils import get_content_hash
from pypdf import PdfReader

from research_group.policy import FILE_TOKEN, MANAGED, deny

SURVEY_FILE_DOCTYPES = ("RG Survey", "RG Survey Question", "RG Survey Response")
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
# Bound each XML parser input and retained Python entries independently of the
# package's expanded-byte limit. Content Types/presentation keep tighter caps.
MAX_XML_PART_BYTES = 16 * 1024 * 1024
MAX_PRESENTATION_XML_BYTES = 8 * 1024 * 1024
MAX_PACKAGE_XML_ENTRIES = 100_000
# Both original_filename and native File.file_name are Data(140); native naming
# can append six characters after validation when the physical name collides.
MAX_FILENAME_CHARS = 140 - 6
# Frappe 16.55.2 can append a six-character hash when a physical name collides.
MAX_FILENAME_BYTES = 255 - 6
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
OFFICE_REL_NAMESPACES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
PRESENTATION_NAMESPACES = (
    "http://schemas.openxmlformats.org/presentationml/2006/main",
    "http://purl.oclc.org/ooxml/presentationml/main",
)
PRESENTATION_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
SLIDE_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
WORD_NAMESPACES = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
WORD_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
MAX_MANUSCRIPT_TEXT = 500_000
MAX_MANUSCRIPT_PARAGRAPHS = 5000


def max_bytes():
    return int(frappe.conf.get("rg_max_file_size") or DEFAULT_MAX_BYTES)


def _ppt_record(stream, offset, record_type=None, options=None, limit=None):
    """Read only a bounded MS-PPT record header, never an unchecked offset."""
    limit = len(stream) if limit is None else limit
    if offset < 0 or offset + 8 > limit:
        raise ValueError("PPT record offset")
    flags, kind, size = struct.unpack_from("<HHI", stream, offset)
    end = offset + 8 + size
    if not kind or end > limit:
        raise ValueError("PPT record boundary")
    if record_type is not None and kind != record_type:
        raise ValueError("PPT record type")
    if options is not None and flags != options:
        raise ValueError("PPT record options")
    return offset + 8, end


def _validate_ppt(content):
    """Minimum unencrypted PPT structure, not a PowerPoint rendering validator.

    Follow MS-PPT 2.1.2/2.3: CurrentUserAtom -> UserEditAtom chain ->
    PersistDirectoryAtom -> the live DocumentContainer. Unknown child records
    remain supported, but their declared boundaries must stay in the container.
    """
    if not olefile.isOleFile(BytesIO(content)):
        raise ValueError("OLE")
    with olefile.OleFileIO(BytesIO(content), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        streams = {}
        for name in ("Current User", "PowerPoint Document"):
            if not ole.exists(name) or not 8 <= ole.get_size(name) <= len(content):
                raise ValueError("PowerPoint stream")
            streams[name] = ole.openstream(name).read()
    current, document = streams["Current User"], streams["PowerPoint Document"]
    body, end = _ppt_record(current, 0, 0x0FF6, 0)
    if end - body < 24:
        raise ValueError("CurrentUserAtom size")
    size, token, edit = struct.unpack_from("<III", current, body)
    username_size, version = struct.unpack_from("<HH", current, body + 12)
    base_size = 24 + username_size
    # Legacy writers omit the optional Unicode username from rh.recLen.
    if (size != 20 or token != 0xE391C05F or username_size > 255 or version != 0x03F4
            or current[body + 16:body + 18] != b"\x03\x00"
            or end - body not in {base_size, base_size + 2 * username_size}
            or len(current) - body not in {base_size, base_size + 2 * username_size}):
        raise ValueError("CurrentUserAtom fields")
    if struct.unpack_from("<I", current, body + 20 + username_size)[0] not in {8, 9}:
        raise ValueError("CurrentUserAtom release")

    seen_edits = set()
    document_offset = None
    document_limit = None
    while edit:
        if edit in seen_edits:
            raise ValueError("PPT edit cycle")
        seen_edits.add(edit)
        body, end = _ppt_record(document, edit, 0x0FF5, 0)
        if end - body not in {28, 32} or document[body + 6:body + 8] != b"\x00\x03":
            raise ValueError("UserEditAtom fields")
        previous, directory, doc_id, seed = struct.unpack_from("<IIII", document, body + 8)
        if not previous < directory < edit or doc_id != 1 or not 1 <= seed <= 0xFFFFF:
            raise ValueError("UserEditAtom offsets")
        start, stop = _ppt_record(document, directory, 0x1772, 0, limit=edit)
        ids = set()
        while start < stop:
            if start + 4 > stop:
                raise ValueError("PersistDirectoryAtom entry")
            entry = struct.unpack_from("<I", document, start)[0]
            first, count = entry & 0xFFFFF, entry >> 20
            start += 4
            if not first or not count or first + count - 1 > min(seed, 0xFFFFE) or start + count * 4 > stop:
                raise ValueError("PersistDirectoryAtom range")
            for i in range(count):
                persist_id = first + i
                offset = struct.unpack_from("<I", document, start + 4 * i)[0]
                if persist_id in ids or offset < previous:
                    raise ValueError("PersistDirectoryAtom duplicate/offset")
                ids.add(persist_id)
                _ppt_record(document, offset, limit=directory)
                # Newest directory wins; older edits can supply an unchanged document.
                if persist_id == doc_id and document_offset is None:
                    document_offset, document_limit = offset, directory
            start += count * 4
        if not ids:
            raise ValueError("empty PersistDirectoryAtom")
        edit = previous
    if not seen_edits or document_offset is None:
        raise ValueError("missing live document")
    body, end = _ppt_record(document, document_offset, 0x03E8, 0x000F, limit=document_limit)
    child, child_end = _ppt_record(document, body, 0x03E9, 0x0001, limit=end)
    if child_end - child != 40:
        raise ValueError("DocumentAtom size")
    if any(not 0x240 <= n <= 0x7E00 for n in struct.unpack_from("<IIII", document, child)):
        raise ValueError("DocumentAtom dimensions")
    cursor, found_end = child_end, False
    while cursor < end:
        start, stop = _ppt_record(document, cursor, limit=end)
        flags, kind = struct.unpack_from("<HH", document, cursor)
        if kind == 0x03EA:
            if flags or stop != start:
                raise ValueError("EndDocumentAtom")
            found_end = True
        cursor = stop
    if not found_end:
        raise ValueError("missing EndDocumentAtom")


def _part_name(name, decoded=False):
    """Canonical ZIP member name; encoded separators/dot segments are unsafe."""
    if not decoded and any(encoded in name.lower() for encoded in ("%2f", "%5c")):
        raise ValueError("OPC encoded separator")
    part = name if decoded else unquote(name, errors="strict")
    if (not part or part.startswith("/") or "\\" in part or "\x00" in part
            or any(ord(c) < 32 for c in part)
            or any(p in {"", ".", ".."} for p in part.split("/"))):
        raise ValueError("OPC part path")
    return part


def _relationship_target(source, target):
    if (any(ord(c) < 32 for c in target)
            or any(encoded in target.lower() for encoded in ("%2f", "%5c"))):
        raise ValueError("OPC relationship path")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("OPC internal URI")
    decoded = unquote(parsed.path, errors="strict")
    if not decoded or "\\" in decoded or "\x00" in decoded or any(ord(c) < 32 for c in decoded):
        raise ValueError("OPC relationship path")
    parts = [] if decoded.startswith("/") else source.split("/")[:-1]
    for part in decoded.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                raise ValueError("OPC relationship escapes package")
            parts.pop()
        else:
            parts.append(part)
    return _part_name("/".join(parts), decoded=True)


class _PackageXMLBudget:
    """One package-wide allowance, charged before retaining summary entries."""
    def __init__(self, limit=None):
        self.limit = MAX_PACKAGE_XML_ENTRIES if limit is None else limit
        self.used = 0

    def consume(self, count=1):
        if count > self.limit - self.used:
            raise ValueError("OPC XML retained-entry budget")
        self.used += count


class _PackageXML:
    """Streaming summary avoids constructing 250MB XML trees or expanding DTDs."""
    def __init__(self, budget=None):
        self.budget = _PackageXMLBudget() if budget is None else budget
        self.root = None
        self.depth = 0
        self.children = []
        self.references = set()
        self.slides = []
        self.slide_ids = set()

    def start(self, tag, attrs):
        if self.root is None:
            self.root = tag
        if "{http://www.w3.org/XML/1998/namespace}base" in attrs:
            raise ValueError("OPC xml:base")
        if self.depth == 1 and self.root in {
            f"{{{PACKAGE_REL_NS}}}Relationships", f"{{{CONTENT_TYPES_NS}}}Types",
        }:
            # Charge both the child record and every copied attribute before
            # dict(attrs) can retain an attacker-controlled number of entries.
            self.budget.consume(1 + len(attrs))
            self.children.append((tag, dict(attrs)))
        for ns in OFFICE_REL_NAMESPACES:
            for key in ("id", "embed", "link"):
                attr = f"{{{ns}}}{key}"
                if attr in attrs:
                    if not attrs[attr]:
                        raise ValueError("empty OPC reference")
                    if attrs[attr] not in self.references:
                        self.budget.consume()
                        self.references.add(attrs[attr])
        if tag in {f"{{{p}}}sldId" for p in PRESENTATION_NAMESPACES}:
            values = [attrs[f"{{{ns}}}id"] for ns in OFFICE_REL_NAMESPACES if f"{{{ns}}}id" in attrs]
            if len(values) != 1 or values[0] in self.slide_ids:
                raise ValueError("missing/duplicate slide relationship id")
            self.budget.consume(2)  # List entry and duplicate-detection set entry.
            self.slides.append(values[0])
            self.slide_ids.add(values[0])
        self.depth += 1

    def end(self, tag):
        self.depth -= 1

    def data(self, data):
        pass

    def doctype(self, *args):
        # This is called for UTF-16/UTF-32 DTDs too, before entity expansion.
        raise ValueError("XML DTD/entity declarations")

    def close(self):
        return self


def _read_package_xml(archive, info, budget=None):
    if info.file_size > MAX_XML_PART_BYTES:
        raise ValueError("XML part size")
    parser = ElementTree.XMLParser(target=_PackageXML(budget))
    with archive.open(info) as stream:
        total = 0
        while data := stream.read(64 * 1024):
            total += len(data)
            if total > min(info.file_size, MAX_XML_PART_BYTES):
                raise ValueError("XML expanded size")
            parser.feed(data)
    return parser.close()


def _validate_pptx(content):
    """Check OPC connectivity and XML safety, without claiming rendering fidelity."""
    with ZipFile(BytesIO(content)) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError("expanded size")
        parts = {}
        for info in infos:
            if info.flag_bits & 1:
                raise ValueError("encrypted ZIP")
            # ZipInfo can normalize backslashes or truncate at NUL depending on
            # the host. Validate the original archive spelling consistently.
            raw_name = info.orig_filename
            name = _part_name(raw_name.rstrip("/") if info.is_dir() else raw_name)
            if info.is_dir():
                continue
            if name in parts or "vbaproject" in name.lower():
                raise ValueError("duplicate/macro part")
            parts[name] = info
        if not {"[Content_Types].xml", "_rels/.rels"} <= parts.keys():
            raise ValueError("OPC entry parts")
        if parts["[Content_Types].xml"].file_size > 1024 * 1024:
            raise ValueError("content types size")
        budget = _PackageXMLBudget()
        types = _read_package_xml(archive, parts["[Content_Types].xml"], budget)
        if types.root != f"{{{CONTENT_TYPES_NS}}}Types":
            raise ValueError("content types root")
        overrides, defaults = {}, {}
        for tag, attrs in types.children:
            content_type = attrs.get("ContentType", "")
            if not content_type or "macroenabled" in content_type.lower() or "vbaproject" in content_type.lower():
                raise ValueError("content type")
            if tag == f"{{{CONTENT_TYPES_NS}}}Override":
                raw = attrs.get("PartName", "")
                if not raw.startswith("/"):
                    raise ValueError("override path")
                name = _part_name(raw[1:])
                if name in overrides or name not in parts:
                    raise ValueError("duplicate/missing override part")
                budget.consume()
                overrides[name] = content_type
            elif tag == f"{{{CONTENT_TYPES_NS}}}Default":
                extension = attrs.get("Extension", "").lower()
                if not extension or extension in defaults:
                    raise ValueError("duplicate/empty default type")
                budget.consume()
                defaults[extension] = content_type
            else:
                raise ValueError("content types element")

        def content_type(name):
            extension = "rels" if name.endswith(".rels") else PurePosixPath(name).suffix[1:].lower()
            return overrides.get(name) or defaults.get(extension)

        summaries, relationships = {}, {}
        for name, info in parts.items():
            if name == "[Content_Types].xml":
                continue
            mime = content_type(name)
            if not mime:
                raise ValueError("missing part content type")
            if name.endswith((".xml", ".rels")) or mime.endswith(("+xml", "/xml")):
                if mime == PRESENTATION_CONTENT_TYPE and info.file_size > MAX_PRESENTATION_XML_BYTES:
                    raise ValueError("presentation size")
                budget.consume()
                summaries[name] = _read_package_xml(archive, info, budget)
            if name.endswith(".rels"):
                path = PurePosixPath(name)
                if name == "_rels/.rels":
                    source = ""
                elif path.parent.name == "_rels":
                    source = str(path.parent.parent / path.name[:-5])
                    if source not in parts:
                        raise ValueError("missing relationship source")
                else:
                    raise ValueError("relationship part path")
                summary = summaries[name]
                if summary.root != f"{{{PACKAGE_REL_NS}}}Relationships":
                    raise ValueError("relationship root")
                mapping = {}
                for tag, attrs in summary.children:
                    rid, kind, target = attrs.get("Id"), attrs.get("Type"), attrs.get("Target")
                    mode = attrs.get("TargetMode", "Internal")
                    if (tag != f"{{{PACKAGE_REL_NS}}}Relationship" or not rid or rid in mapping
                            or not kind or not target or mode not in {"Internal", "External"}):
                        raise ValueError("relationship fields")
                    resolved = _relationship_target(source, target) if mode == "Internal" else None
                    if resolved is not None and resolved not in parts:
                        raise ValueError("dangling relationship")
                    budget.consume()
                    mapping[rid] = (kind, resolved)
                budget.consume()
                relationships[source] = mapping
        main = [target for kind, target in relationships.get("", {}).values()
                if kind in {ns + "/officeDocument" for ns in OFFICE_REL_NAMESPACES}]
        if len(main) != 1 or not main[0] or content_type(main[0]) != PRESENTATION_CONTENT_TYPE:
            raise ValueError("OPC presentation entry")
        presentation = main[0]
        summary = summaries.get(presentation)
        if not summary or summary.root not in {f"{{{ns}}}presentation" for ns in PRESENTATION_NAMESPACES}:
            raise ValueError("presentation root")
        for source, xml in summaries.items():
            if not xml.references <= relationships.get(source, {}).keys():
                raise ValueError("missing referenced relationship")
        for rid in summary.slides:
            kind, target = relationships.get(presentation, {}).get(rid, (None, None))
            slide = summaries.get(target)
            if (kind not in {ns + "/slide" for ns in OFFICE_REL_NAMESPACES} or not target
                    or content_type(target) != SLIDE_CONTENT_TYPE or not slide
                    or slide.root not in {f"{{{ns}}}sld" for ns in PRESENTATION_NAMESPACES}):
                raise ValueError("presentation slide reference")
        # Read non-XML members too, so corrupt CRCs cannot remain unnoticed.
        if archive.testzip() is not None:
            raise ValueError("CRC")


def validate_material(filename, content):
    if not isinstance(content, bytes) or not content:
        frappe.throw("材料不能为空。")
    if len(content) > max_bytes():
        frappe.throw("材料超过允许大小（默认 50MB）。")
    if not isinstance(filename, str) or not filename or any(c in filename for c in ("/", "\\", "\x00")):
        frappe.throw("材料文件名无效。")
    if len(filename) > MAX_FILENAME_CHARS:
        frappe.throw("材料文件名不能超过 134 个字符（含扩展名，已为原生存储名称预留 6 个字符）。")
    if len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
        frappe.throw("材料文件名的 UTF-8 长度不能超过 249 字节（含扩展名，已预留存储后缀）；请缩短文件名。")
    ext = PurePosixPath(filename).suffix.lower()
    if ext not in {".pdf", ".ppt", ".pptx"}:
        frappe.throw("仅支持 PPT、PPTX 或 PDF。")
    try:
        if ext == ".pdf":
            if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-4096:]:
                raise ValueError("signature")
            pdf = PdfReader(BytesIO(content), strict=True)
            if pdf.is_encrypted or len(pdf.pages) < 1:
                raise ValueError("empty/encrypted")
        elif ext == ".ppt":
            _validate_ppt(content)
        else:
            _validate_pptx(content)
    except Exception:
        # Never echo parser content or file bytes to an untrusted caller.
        frappe.throw("文件内容与扩展名不符、已损坏或不受支持。", exc=frappe.ValidationError)
    return ext


class _WordText(_PackageXML):
    """Bounded text extraction; no Office renderer, fields, links or macros run."""
    def __init__(self, budget=None):
        super().__init__(budget)
        self.body_count = 0
        self.paragraphs = []
        self.paragraph_contexts = []
        self.paragraph_count = 0
        self.textbox_depth = 0
        self.run_depths = []
        self.text_depth = 0
        self.text_size = 0

    def _append_text(self, text):
        if not self.paragraph_contexts or not text:
            return
        if len(text) > MAX_MANUSCRIPT_TEXT - self.text_size:
            raise ValueError("Word text limit")
        self.budget.consume()
        self.text_size += len(text)
        self.paragraph_contexts[-1][0].append(text)

    def _flush_paragraph(self, context):
        text = "".join(context[0])
        if text:
            if len(self.paragraphs) >= MAX_MANUSCRIPT_PARAGRAPHS:
                raise ValueError("Word paragraph limit")
            self.budget.consume()
            self.paragraphs.append(text)
        context[0].clear()

    def start(self, tag, attrs):
        super().start(tag, attrs)
        if tag in {f"{{{ns}}}body" for ns in WORD_NAMESPACES}:
            self.body_count += 1
        if tag in {f"{{{ns}}}txbxContent" for ns in WORD_NAMESPACES}:
            self.textbox_depth += 1
        if tag in {f"{{{ns}}}p" for ns in WORD_NAMESPACES}:
            if self.paragraph_contexts:
                if self.textbox_depth <= self.paragraph_contexts[-1][1]:
                    raise ValueError("nested Word paragraph outside textbox")
                # A textbox can live inside an outer paragraph's drawing run.
                # Keep XML text order without fusing its before/after segments
                # or duplicating the textbox's independently selected text.
                self._flush_paragraph(self.paragraph_contexts[-1])
            if self.paragraph_count >= MAX_MANUSCRIPT_PARAGRAPHS:
                raise ValueError("Word paragraph limit")
            self.budget.consume()
            self.paragraph_count += 1
            self.paragraph_contexts.append(([], self.textbox_depth))
        if tag in {f"{{{ns}}}t" for ns in WORD_NAMESPACES}:
            self.text_depth += 1
        if tag in {f"{{{ns}}}r" for ns in WORD_NAMESPACES}:
            self.budget.consume()
            self.run_depths.append(self.depth)
        # w:pPr/w:tabs/w:tab defines a tab stop, not a run character. A
        # textbox can itself be inside an outer run, so require a direct child.
        run_character = self.run_depths and self.depth == self.run_depths[-1] + 1
        if run_character and tag in {f"{{{ns}}}tab" for ns in WORD_NAMESPACES}:
            self._append_text("\t")
        if run_character and tag in {f"{{{ns}}}{name}" for ns in WORD_NAMESPACES for name in ("br", "cr")}:
            self._append_text("\n")
        if tag in {f"{{{ns}}}altChunk" for ns in WORD_NAMESPACES}:
            raise ValueError("Word active import")

    def data(self, data):
        if self.text_depth:
            self._append_text(data)

    def end(self, tag):
        if tag in {f"{{{ns}}}t" for ns in WORD_NAMESPACES}:
            self.text_depth -= 1
        if tag in {f"{{{ns}}}p" for ns in WORD_NAMESPACES}:
            self._flush_paragraph(self.paragraph_contexts.pop())
        if tag in {f"{{{ns}}}txbxContent" for ns in WORD_NAMESPACES}:
            self.textbox_depth -= 1
        if tag in {f"{{{ns}}}r" for ns in WORD_NAMESPACES}:
            self.run_depths.pop()
        super().end(tag)


def _read_word_xml(archive, info, budget):
    if info.file_size > MAX_PRESENTATION_XML_BYTES:
        raise ValueError("Word document size")
    parser = ElementTree.XMLParser(target=_WordText(budget))
    with archive.open(info) as stream:
        total = 0
        while data := stream.read(64 * 1024):
            total += len(data)
            if total > min(info.file_size, MAX_PRESENTATION_XML_BYTES):
                raise ValueError("Word expanded size")
            parser.feed(data)
    return parser.close()


def _validate_docx(content):
    """Validate a real non-macro Word OPC graph before any persistent write."""
    with ZipFile(BytesIO(content)) as archive:
        infos = archive.infolist()
        if len(infos) > 5000 or sum(item.file_size for item in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError("Word expanded size")
        parts = {}
        for info in infos:
            if info.flag_bits & 1:
                raise ValueError("encrypted ZIP")
            raw = info.orig_filename
            name = _part_name(raw.rstrip("/") if info.is_dir() else raw)
            if info.is_dir():
                continue
            lower = name.lower()
            if (name in parts or "vbaproject" in lower or lower.startswith("word/embeddings/")
                    or lower.startswith("word/activex/")):
                raise ValueError("Word duplicate/active part")
            parts[name] = info
        if not {"[Content_Types].xml", "_rels/.rels"} <= parts.keys():
            raise ValueError("Word OPC entry")
        if parts["[Content_Types].xml"].file_size > 1024 * 1024:
            raise ValueError("Word content types size")
        budget = _PackageXMLBudget()
        types = _read_package_xml(archive, parts["[Content_Types].xml"], budget)
        if types.root != f"{{{CONTENT_TYPES_NS}}}Types":
            raise ValueError("Word content types root")
        defaults, overrides = {}, {}
        for tag, attrs in types.children:
            mime = attrs.get("ContentType", "")
            if not mime or any(x in mime.lower() for x in ("macroenabled", "vbaproject", "activex")):
                raise ValueError("Word content type")
            if tag == f"{{{CONTENT_TYPES_NS}}}Override":
                raw = attrs.get("PartName", "")
                if not raw.startswith("/"):
                    raise ValueError("Word override path")
                name = _part_name(raw[1:])
                if name in overrides or name not in parts:
                    raise ValueError("Word override target")
                overrides[name] = mime
            elif tag == f"{{{CONTENT_TYPES_NS}}}Default":
                extension = attrs.get("Extension", "").lower()
                if not extension or extension in defaults:
                    raise ValueError("Word default type")
                defaults[extension] = mime
            else:
                raise ValueError("Word type element")
            budget.consume()

        def content_type(name):
            extension = "rels" if name.endswith(".rels") else PurePosixPath(name).suffix[1:].lower()
            return overrides.get(name) or defaults.get(extension)

        summaries, relationships = {}, {}
        for name, info in parts.items():
            if name == "[Content_Types].xml":
                continue
            mime = content_type(name)
            if not mime:
                raise ValueError("Word missing content type")
            if name.endswith((".xml", ".rels")) or mime.endswith(("+xml", "/xml")):
                budget.consume()
                summaries[name] = (_read_word_xml(archive, info, budget) if mime == WORD_CONTENT_TYPE
                                   else _read_package_xml(archive, info, budget))
            if not name.endswith(".rels"):
                continue
            path = PurePosixPath(name)
            if name == "_rels/.rels":
                source = ""
            elif path.parent.name == "_rels":
                source = str(path.parent.parent / path.name[:-5])
                if source not in parts:
                    raise ValueError("Word relationship source")
            else:
                raise ValueError("Word relationship path")
            summary = summaries[name]
            if summary.root != f"{{{PACKAGE_REL_NS}}}Relationships":
                raise ValueError("Word relationship root")
            mapping = {}
            for tag, attrs in summary.children:
                rid, kind, target = attrs.get("Id"), attrs.get("Type"), attrs.get("Target")
                mode = attrs.get("TargetMode", "Internal")
                if (tag != f"{{{PACKAGE_REL_NS}}}Relationship" or not rid or rid in mapping
                        or not kind or not target or mode not in {"Internal", "External"}):
                    raise ValueError("Word relationship fields")
                if any(kind.lower().endswith("/" + x) for x in (
                    "vbaproject", "oleobject", "package", "afchunk", "attachedtemplate", "control",
                )):
                    raise ValueError("Word active relationship")
                if mode == "External":
                    # Passive hyperlinks are retained as document data, never fetched.
                    if (not kind.endswith("/hyperlink") or urlsplit(target).scheme.lower()
                            not in {"https", "http", "mailto"} or any(ord(c) < 32 for c in target)):
                        raise ValueError("Word external relationship")
                    resolved = None
                else:
                    resolved = _relationship_target(source, target)
                    if resolved not in parts:
                        raise ValueError("Word dangling relationship")
                budget.consume()
                mapping[rid] = (kind, resolved)
            relationships[source] = mapping
        main = [target for kind, target in relationships.get("", {}).values()
                if kind in {ns + "/officeDocument" for ns in OFFICE_REL_NAMESPACES}]
        if len(main) != 1 or not main[0] or content_type(main[0]) != WORD_CONTENT_TYPE:
            raise ValueError("Word document entry")
        document = summaries.get(main[0])
        if (not isinstance(document, _WordText) or document.body_count != 1
                or document.root not in {f"{{{ns}}}document" for ns in WORD_NAMESPACES}):
            raise ValueError("Word document body")
        for source, summary in summaries.items():
            if not summary.references <= relationships.get(source, {}).keys():
                raise ValueError("Word missing reference")
        if archive.testzip() is not None:
            raise ValueError("Word CRC")
        return document.paragraphs


def validate_manuscript(filename, content):
    if not isinstance(content, bytes) or not content or len(content) > max_bytes():
        frappe.throw("论文文件为空或超过允许大小（默认 50MB）。")
    _validate_filename_spelling(filename)
    if len(filename) > MAX_FILENAME_CHARS or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
        frappe.throw("论文文件名过长，请缩短后重试。")
    extension = PurePosixPath(filename).suffix.lower()
    if extension not in {".pdf", ".docx"}:
        frappe.throw("论文版本仅支持 PDF 或 DOCX。")
    if extension == ".pdf":
        try:
            if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-4096:]:
                raise ValueError("PDF signature")
            # Xref/object streams can already expand during PdfReader's
            # structural validation. Keep that first parser outside the web
            # process with the same limits as subsequent explicit text reads.
            from research_group.manuscripts import _parse_pdf_isolated
            _parse_pdf_isolated(content, validate_only=True)
        except Exception:
            frappe.throw("PDF内容已损坏、不受支持或超过解析上限。", frappe.ValidationError)
    else:
        try:
            _validate_docx(content)
        except Exception:
            frappe.throw("DOCX内容已损坏、包含不支持的活动内容或超过解析上限。", frappe.ValidationError)
    return extension


def _file_reference_identities(url, *, deletion=False):
    """Resolve local URLs, including the basename target of native thumbnail deletion."""
    if not isinstance(url, str) or not url:
        return set()
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    identities = set()
    local = not (parsed.scheme or parsed.netloc)
    if not local:
        from frappe.utils import get_url
        local = url.startswith(get_url().rstrip("/") + "/")
    if local:
        for prefix, directory in (("/private/files/", "private"), ("/files/", "public")):
            if path.startswith(prefix):
                identities.add(str(Path(frappe.get_site_path(directory, "files", path[len(prefix):])).resolve()))
                break
    if deletion:
        # Native delete_file uses this mapping, rather than get_full_path.
        # In particular /files/./x and arbitrary thumbnail URLs map to private/x.
        parent, basename = os.path.split(url.strip("/"))
        if basename:
            directory = "public" if parent == "files" else "private"
            identities.add(str(Path(frappe.get_site_path(directory, "files", basename)).resolve()))
    return identities


def _reference_identities(doc, fields=("file_url", "thumbnail_url"), *, deletion=False):
    identities = set()
    for field in fields:
        identities.update(_file_reference_identities(doc.get(field), deletion=deletion))
    return identities


def _managed_file_rows():
    return frappe.get_all(
        "File", filters={"attached_to_doctype": ["in", list(MANAGED)]},
        fields=["name", "file_url", "thumbnail_url", "content_hash"], limit_page_length=0,
    )


def _references_managed_file(documents, *, fields=("file_url", "thumbnail_url"),
                             include_hash=False, deletion=False, managed_rows=None):
    identities, hashes = set(), set()
    for doc in documents:
        if doc:
            identities.update(_reference_identities(doc, fields, deletion=deletion))
            if include_hash and doc.get("content_hash"):
                hashes.add(doc.get("content_hash"))
    if not identities and not hashes:
        return False
    rows = _managed_file_rows() if managed_rows is None else managed_rows
    return any(
        identities.intersection(_reference_identities(row))
        or (hashes and row.get("content_hash") in hashes) for row in rows
    )


def is_managed_file_alias(doc):
    """An unmanaged row must not grant native access to a managed physical blob."""
    return doc.get("attached_to_doctype") not in MANAGED and _references_managed_file((doc,))


def is_survey_file(doc):
    """Surveys support no attachments; keep this separate from material scope."""
    return doc.get("attached_to_doctype") in SURVEY_FILE_DOCTYPES


def managed_file_alias_names():
    """Keep pre-existing aliases out of native File lists as well as single reads."""
    rows = frappe.get_all(
        "File", filters={"is_folder": 0},
        fields=["name", "attached_to_doctype", "file_url", "thumbnail_url"], limit_page_length=0,
    )
    managed_identities = set()
    for row in rows:
        if row.attached_to_doctype in MANAGED:
            managed_identities.update(_reference_identities(row))
    return [row.name for row in rows if row.attached_to_doctype not in MANAGED
            and managed_identities.intersection(_reference_identities(row))]


def _validate_filename_spelling(filename):
    if (not isinstance(filename, str) or not filename or filename in {".", ".."}
            or any(c in filename for c in ("%", "?", "#", "/", "\\", "\x00"))
            or any(ord(c) < 32 for c in filename)):
        frappe.throw("文件名不能包含 %、?、#、路径分隔符或控制字符。")


def _safe_storage_name(filename):
    """Keep native File metadata/path stable through its later HTML sanitizer.

    Call only after validating the original name and document format. The
    version row retains that original name; native File needs a storage name,
    not user-authored markup. Preserve ordinary existing naming behavior.
    """
    _validate_filename_spelling(filename)
    if any(character in filename for character in '<>&"\''):
        extension = PurePosixPath(filename).suffix.lower()
        return "rg-file-" + hashlib.sha256(filename.encode("utf-8")).hexdigest() + extension
    return filename


def _validate_local_file_spelling(doc):
    if doc.get("is_folder") or doc.is_remote_file:
        return
    if doc.get("file_name"):
        _validate_filename_spelling(doc.file_name)
    for field in ("file_url", "thumbnail_url"):
        url = doc.get(field)
        if not url or urlsplit(url).scheme:
            continue
        if (any(c in url for c in ("%", "?", "#", "\\", "\x00"))
                or any(part in {".", ".."} for part in url.split("/"))
                or "//" in url or any(ord(c) < 32 for c in url)):
            frappe.throw("本地文件路径必须使用原始、规范的文件名。")


def _collision_filename(filename):
    """Reserve six suffix characters without exceeding native filename limits."""
    stem, extension = os.path.splitext(filename)
    suffix = frappe.generate_hash(length=6)
    while stem and (len(stem + suffix + extension) > 140
                    or len((stem + suffix + extension).encode("utf-8")) > 255):
        stem = stem[:-1]
    return stem + suffix + extension


class ResearchGroupFile(File):
    def _guard(self):
        old = self.get_doc_before_save() if not self.is_new() else None
        from research_group.collaboration_security import is_internal_file
        # All other collaboration tables are attachment-free. This check also
        # runs before native File.before_insert performs its first physical write.
        for candidate in (self, old):
            if (candidate and is_internal_file(candidate)
                    and candidate.get("attached_to_doctype") != "RG Manuscript Version"):
                deny()
        from research_group.ai.native import is_ai_file
        if is_ai_file(self) or (old and is_ai_file(old)):
            frappe.throw("AI会话、记忆和计划不支持附件。", frappe.PermissionError)
        if is_survey_file(self) or (old and is_survey_file(old)):
            frappe.throw("投票与问卷不支持附件。", frappe.PermissionError)
        managed = self.attached_to_doctype in MANAGED or (old and old.attached_to_doctype in MANAGED)
        referenced = _references_managed_file((self, old), include_hash=True, deletion=True)
        if managed or referenced:
            if self.flags.get("rg_file") is not FILE_TOKEN:
                frappe.throw("受管材料只能通过专用材料入口上传，且不可改为公开、替换或删除。", frappe.PermissionError)
            if not self.is_private:
                deny()
            if old:
                for f in ("attached_to_doctype", "attached_to_name", "file_url", "thumbnail_url", "is_private", "content_hash"):
                    if self.get(f) != old.get(f):
                        deny()

    def before_insert(self):
        self._guard()
        _validate_local_file_spelling(self)
        visible_name = self.file_name if self.flags.get("rg_file") is FILE_TOKEN else None
        if not self.is_folder and self.get("content"):
            # Native File.before_insert writes the blob before validate runs.
            # Hash actual, natively decoded bytes now; a client-supplied hash
            # must never bypass the managed-content guard or trigger a public
            # write. Preserve that decoded content for the later native call.
            self.content = self.get_content()
            self.content_hash = get_content_hash(self.content)
        self._guard()
        super().before_insert()
        if visible_name:
            self.file_name = visible_name

    def validate(self):
        self._guard()  # must precede native handle_is_private_changed and its filesystem move
        _validate_local_file_spelling(self)
        super().validate()

    def on_trash(self):
        if self.attached_to_doctype == "RG Manuscript Version":
            deny()
        if is_survey_file(self) and frappe.session.user != "Administrator":
            deny()
        if self.attached_to_doctype in MANAGED and frappe.session.user != "Administrator":
            deny()
        old = self.get_doc_before_save()
        if (self.attached_to_doctype not in MANAGED
                and _references_managed_file((self, old), deletion=True)):
            deny()
        if _references_managed_file((self, old), fields=("thumbnail_url",), deletion=True):
            deny()
        super().on_trash()

    def delete_file_data_content(self, only_thumbnail=False):
        # Recheck at the actual after-commit/rollback deletion boundary too.
        if _references_managed_file((self,), fields=("thumbnail_url",), deletion=True):
            deny()
        if not only_thumbnail and _references_managed_file((self,), fields=("file_url",), deletion=True):
            if self.attached_to_doctype not in MANAGED:
                deny()
            only_thumbnail = True  # a retained managed row still owns the blob
        return super().delete_file_data_content(only_thumbnail=only_thumbnail)

    def get_content(self, encodings=None):
        from research_group.ai.native import is_ai_file
        from research_group.collaboration_security import is_internal_file
        if is_internal_file(self) and self.attached_to_doctype != "RG Manuscript Version":
            deny()
        if is_ai_file(self):
            deny()
        if is_survey_file(self) and frappe.session.user != "Administrator":
            deny()
        if is_managed_file_alias(self):
            deny()
        return super().get_content(encodings=encodings)

    def is_downloadable(self):
        from research_group.permissions import file_has_permission
        if not file_has_permission(self, "read"):
            return False
        if self.attached_to_doctype == "RG Manuscript Version":
            # Its dedicated author/reviewer policy above is the download policy;
            # the parent intentionally denies native JSON/CRUD to every role.
            return True
        return super().is_downloadable()

    def save_file_on_filesystem(self):
        # Native save_file also allocates a new URL for an existing, unrelated
        # File when overwrite=False; reserve that new path atomically as well.
        self.flags.rg_allocate_file = True
        try:
            return super().save_file_on_filesystem()
        finally:
            self.flags.pop("rg_allocate_file", None)

    def write_file(self):
        self._guard()
        if self.is_remote_file:
            return super().write_file()
        _validate_local_file_spelling(self)
        if not self.is_new() and not self.flags.get("rg_allocate_file"):
            return super().write_file()  # preserve unrelated native in-place operations
        self.validate_file_path()
        self.validate_file_url()
        if isinstance(self._content, str):
            self._content = self._content.encode()
        self.check_content()
        initial_url = self.file_url
        initial_name = os.path.basename(initial_url)
        for attempt in range(100):
            if attempt:
                self.file_url = initial_url.rsplit("/", 1)[0] + "/" + _collision_filename(initial_name)
            path = Path(self.get_full_path()).resolve()
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
                break
            except FileExistsError:
                continue
        else:
            frappe.throw("无法分配独立文件名，请重试。")
        tracker = self.flags.get("rg_created_files")
        if tracker is not None:
            tracker.append({"path": str(path), "file_url": self.file_url,
                            "content_hash": self.content_hash, "file": self})
        self.flags.new_file = True
        frappe.db.after_rollback.add(self.on_rollback)
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(self._content)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            for flag in ("new_file", "original_content", "original_path"):
                self.flags.pop(flag, None)
            raise
        return str(path)


def save_managed_file(filename, content, doctype, name, created_files=None):
    if doctype == "RG Manuscript Version":
        validate_manuscript(filename, content)
    else:
        validate_material(filename, content)
    # Native before_insert writes bytes before BaseDocument sanitizes Data
    # fields. Establish the final safe name before creating/writing the File so
    # file_name, file_url and rollback's captured path cannot later diverge.
    storage_name = _safe_storage_name(filename)
    file = frappe.get_doc({
        "doctype": "File", "file_name": storage_name, "content": content,
        "is_private": 1, "attached_to_doctype": doctype, "attached_to_name": name,
    })
    file.flags.rg_file = FILE_TOKEN
    if created_files is not None:
        file.flags.rg_created_files = created_files
    return file.insert(ignore_permissions=True)


def read_stored_file_bytes(file, max_read_bytes=None):
    """Read persisted bytes without File.get_content's encoding detection/cache."""
    from research_group.ai.native import is_ai_file
    from research_group.collaboration_security import is_internal_file
    if is_internal_file(file) and file.get("attached_to_doctype") != "RG Manuscript Version":
        deny()
    if is_ai_file(file):
        deny()
    if is_survey_file(file) and frappe.session.user != "Administrator":
        deny()
    if is_managed_file_alias(file):
        deny()
    if file.is_folder:
        frappe.throw("不能读取文件夹内容。")
    file.validate_file_path()
    if file.file_url:
        file.validate_file_url()
    with open(file.get_full_path(), "rb") as source:
        if max_read_bytes is None:
            return source.read()
        # The database file_size is insufficient: use the live file and still
        # bound read(), including when a non-managed native File grows meanwhile.
        if max_read_bytes < 0 or os.fstat(source.fileno()).st_size > max_read_bytes:
            frappe.throw("打包总大小超过 250MB，请分别下载。")
        content = source.read(max_read_bytes + 1)
        if len(content) > max_read_bytes:
            frappe.throw("打包总大小超过 250MB，请分别下载。")
        return content


def _hide_survey_attachments():
    # Native docinfo uses get_all(File), bypassing File's query hooks. Keep
    # legacy rows in storage without returning their names/URLs or log links.
    info = frappe.response.get("docinfo")
    if (frappe.session.user != "Administrator" and info
            and info.get("doctype") in SURVEY_FILE_DOCTYPES):
        info["attachments"] = []
        info["attachment_logs"] = []


@frappe.whitelist()
def load_document(doctype, name):
    from frappe.desk.form.load import getdoc

    from research_group.ai.storage import guard_native
    guard_native(doctype)
    from research_group.collaboration_security import guard_native as guard_collaboration
    guard_collaboration(doctype)
    result = getdoc(doctype, name)  # preserve native parent authorization
    _hide_survey_attachments()
    return result


@frappe.whitelist()
def load_document_info(doc=None, doctype=None, name=None):
    from frappe.desk.form.load import get_docinfo

    from research_group.ai.storage import guard_native
    parsed = frappe.parse_json(doc) if isinstance(doc, str) else doc
    guard_native(doctype or (parsed or {}).get("doctype"))
    from research_group.collaboration_security import guard_native as guard_collaboration
    guard_collaboration(doctype or (parsed or {}).get("doctype"))
    result = get_docinfo(doc=doc, doctype=doctype, name=name)
    _hide_survey_attachments()
    return result


@frappe.whitelist()
def filtered_attachments(dt, dn, filters):
    from frappe.desk.form.load import get_filtered_attachments

    from research_group.ai.storage import guard_native
    guard_native(dt)
    from research_group.collaboration_security import guard_native as guard_collaboration
    guard_collaboration(dt)
    result = get_filtered_attachments(dt, dn, filters)  # retain native validation and authorization
    if dt in SURVEY_FILE_DOCTYPES and frappe.session.user != "Administrator":
        return []
    return result


@frappe.whitelist(allow_guest=True)
def upload_file():
    """Restrict the native multipart/chunk entry before its first temporary write."""
    from research_group.policy import ROLES
    from research_group.ai.storage import guard_native
    guard_native(frappe.form_dict.get("doctype"))
    from research_group.collaboration_security import guard_native as guard_collaboration
    guard_collaboration(frappe.form_dict.get("doctype"))

    if frappe.session.user != "Administrator" and set(frappe.get_roles()).intersection(ROLES):
        frappe.throw("请通过课题组记录中的专用材料入口上传。", frappe.PermissionError)
    if getattr(frappe.local, "request", None) and "file" in frappe.request.files:
        filename = frappe.request.files["file"].filename
        _validate_filename_spelling(filename)
        # Keep native Administrator uploads while preventing its temporary path
        # from overwriting a retained material named .temp-<filename>.
        from frappe.utils import cint
        prefix = "/private/files/" if cint(frappe.form_dict.is_private) else "/files/"
        if _references_managed_file(({"file_url": prefix + ".temp-" + filename},)):
            deny()
    from frappe.handler import upload_file as native_upload_file
    return native_upload_file()


@frappe.whitelist()
def zip_files(files):
    """Native zip helper imports File directly; route it through the same policy."""
    from zipfile import ZIP_DEFLATED

    names = frappe.parse_json(files)
    if not isinstance(names, list) or len(names) > 100:
        frappe.throw("一次最多打包 100 个文件。")
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        total = 0
        for name in names:
            file = frappe.get_doc("File", name)
            # Internal manuscript parents have no native JSON DocPerm. Their
            # download method already applies the dedicated current participant
            # policy, including private storage and aliases, before disk reads.
            native_read = file.attached_to_doctype == "RG Manuscript Version" or file.has_permission("read")
            if not native_read or not file.is_downloadable():
                deny()
            content = read_stored_file_bytes(file, max_read_bytes=MAX_ARCHIVE_BYTES - total)
            total += len(content)
            archive.writestr(file.name + "_" + file.file_name, content)
    frappe.response.update({"filename": "materials.zip", "filecontent": output.getvalue(), "type": "download"})

