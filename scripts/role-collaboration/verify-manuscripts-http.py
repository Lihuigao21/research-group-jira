"""Real manuscript HTTP acceptance against the isolated loopback candidate.

Run once per attempt. Uses only the four existing fictional demo accounts and
the pinned Apache POI DOCX fixture. Makes no Docker, shell, SQL or model calls.
Receipts contain assertion labels/statuses/IDs/hashes, never credentials,
cookies, CSRF tokens, selection text or response bodies. A failure stops
immediately; created fictional records remain identified for central cleanup.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import platform
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import acceptance_config as cfg
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = cfg.ROOT
TASK = cfg.current().task_dir
ACCOUNTS = {
    "teacher": "teacher.demo@example.invalid",
    "author": "student.one.demo@example.invalid",
    "other_student": "student.two.demo@example.invalid",
    "secretary": "secretary.demo@example.invalid",
}
FIXTURE = ROOT / "src/research_group/research_group/tests/fixtures/sample.docx"
FIXTURE_SHA256 = "be4fc1bf778ad8183f714e09b9df40669de34e9ecbc3adecfc8953f00749036f"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EXTERNAL_ORIGIN = "https://manuscript-http-origin.example.invalid"
MAX_RESPONSE = 5 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def base_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or parsed.port is None or parsed.port == 8088):
        raise argparse.ArgumentTypeError("Use a loopback candidate HTTP origin with an explicit port; 8088 is excluded")
    return value.rstrip("/")


def quote(value):
    return urllib.parse.quote(str(value), safe="")


class Receipt:
    def __init__(self, attempt, base):
        self.path = cfg.current().receipt(f"manuscripts-http-{attempt}.json")
        self.value = {
            "attempt": attempt, "started_utc": utc_now(), "base_url": base,
            "fictional_only": True, "transport": "real HTTP; no direct DB/container calls",
            "python": platform.python_version(),
            "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "fixture_sha256": FIXTURE_SHA256, "checks": [], "artifacts": {},
            "phase": "initializing", "ok": False, "complete": False,
        }
        # Exclusively reserve the attempt before the first possible HTTP write.
        with self.path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(self.value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    def save(self):
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def phase(self, label):
        self.value["phase"] = label
        self.save()

    def check(self, label, condition, status=None):
        item = {"check": label, "passed": bool(condition)}
        if status is not None:
            item["status"] = int(status)
        self.value["checks"].append(item)
        self.save()
        if not condition:
            raise AssertionError(label)

    def artifact(self, label, name):
        if not isinstance(name, str) or not 1 <= len(name) <= 240 or any(c in name for c in "\r\n\x00"):
            raise AssertionError("Unexpected artifact identifier")
        self.value["artifacts"][label] = name
        self.save()


class LocalRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, base):
        self.base = base

    def redirect_request(self, request, response, code, message, headers, new_url):
        target = urllib.parse.urlsplit(urllib.parse.urljoin(request.full_url, new_url))
        if (target.scheme, target.netloc) != urllib.parse.urlsplit(self.base)[:2]:
            raise RuntimeError("Refusing a redirect outside the candidate origin")
        if request.get_method() != "GET":
            raise RuntimeError("Refusing to redirect or replay a write")
        return super().redirect_request(request, response, code, message, headers, new_url)


class Client:
    def __init__(self, base, receipt, label):
        self.base, self.receipt, self.label = base, receipt, label
        self.csrf = None
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar), LocalRedirects(base))

    def request(self, path, method="GET", data=None, *, raw=None, content_type=None, headers=None):
        if not path.startswith("/") or path.startswith("//") or urllib.parse.urlsplit(path).netloc:
            raise ValueError("Only relative paths on the candidate are permitted")
        request_headers = {"Accept-Language": "zh-CN"}
        if method != "GET":
            request_headers.update(Origin=self.base, Referer=self.base + "/lab/manuscripts")
            if self.csrf:
                request_headers["X-Frappe-CSRF-Token"] = self.csrf
        if data is not None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        if content_type:
            request_headers["Content-Type"] = content_type
        for key, value in (headers or {}).items():
            if value is None:
                request_headers.pop(key, None)
            else:
                request_headers[key] = value
        request = urllib.request.Request(self.base + path, data=raw, headers=request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=35)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise RuntimeError("Candidate response exceeded the bounded HTTP receipt buffer")
            status = response.code
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            payload = None
        return status, payload, body

    def login(self, password):
        response = self.request("/api/method/login", "POST", {"usr": ACCOUNTS[self.label], "pwd": password})
        status, payload, _ = response
        self.receipt.check("login " + self.label,
                           status == 200 and isinstance(payload, dict) and payload.get("message") == "Logged In", status)
        home = payload.get("home_page") or "/desk"
        if not home.startswith("/"):
            home = "/" + home
        status, _, body = self.request(home)
        match = re.search(rb"(?:frappe\.|window\.)?csrf_token[\"']?\s*[:=]\s*[\"']([^\"']+)", body)
        self.receipt.check("native CSRF " + self.label, status == 200 and match is not None, status)
        self.csrf = match[1].decode("ascii")
        self.receipt.check("usable session CSRF " + self.label, self.csrf not in {"", "None", "null"})
        return self

    def rpc(self, method, data=None, **request_options):
        return self.request("/api/method/research_group.manuscripts." + method,
                            "POST", data or {}, **request_options)

    def multipart(self, command, fields, content, filename, *, headers=None):
        if not re.fullmatch(r"[A-Za-z0-9_<>&. -]{1,134}", filename):
            raise ValueError("Unexpected multipart filename")
        boundary = "RGManuscriptHTTP" + secrets.token_hex(12)
        pieces = []
        for key, value in fields.items():
            if not re.fullmatch(r"[a-z_]+", key) or "\r" in str(value) or "\n" in str(value):
                raise ValueError("Unexpected multipart field")
            pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
        pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {DOCX_TYPE}\r\n\r\n').encode())
        pieces.extend([content, f"\r\n--{boundary}--\r\n".encode()])
        return self.request("/api/method/" + command, "POST", raw=b"".join(pieces),
                            content_type="multipart/form-data; boundary=" + boundary, headers=headers)


def success(receipt, label, response):
    status, payload, _ = response
    receipt.check(label, status == 200 and isinstance(payload, dict) and "message" in payload, status)
    return payload["message"]


def denied(receipt, label, response, statuses=(403,)):
    status, payload, body = response
    # Preserve a newly returned ID if a negative write unexpectedly succeeded,
    # without saving its potentially private body or retrying the request.
    if status == 200 and isinstance(payload, dict):
        value = payload.get("message") or payload.get("data")
        if isinstance(value, dict) and isinstance(value.get("name"), str):
            receipt.artifact("unexpected_write_" + str(len(receipt.value["checks"])), value["name"])
    receipt.check(label, status in statuses and not body.startswith(b"PK\x03\x04"), status)


def all_manuscripts(client, receipt):
    rows = []
    for offset in range(0, 10001, 50):
        page = success(receipt, "bounded manuscript list " + client.label,
                       client.rpc("list_manuscripts", {"offset": offset, "limit": 50}))
        receipt.check("manuscript list shape " + client.label,
                      isinstance(page, dict) and isinstance(page.get("items"), list) and page.get("offset") == offset)
        rows.extend(page["items"])
        if not page.get("has_more"):
            return rows
    raise AssertionError("Manuscript listing exceeded the API's supported paging range")


def file_reference(receipt, version):
    parsed = urllib.parse.urlsplit(version.get("file_url") or "")
    values = urllib.parse.parse_qs(parsed.query)
    valid = (not parsed.scheme and not parsed.netloc and parsed.path.startswith("/private/files/")
             and not parsed.fragment and len(values.get("fid", [])) == 1)
    receipt.check("dedicated version returns a private fid URL", valid)
    return version["file_url"], parsed.path, values["fid"][0]


def assert_download(client, receipt, label, url, content):
    parsed = urllib.parse.urlsplit(url)
    safe_url = urllib.parse.urlunsplit(("", "", urllib.parse.quote(parsed.path, safe="/%"), parsed.query, ""))
    status, _, body = client.request(safe_url)
    receipt.check(label, status == 200 and body == content
                  and hashlib.sha256(body).digest() == hashlib.sha256(content).digest(), status)


def state(client, receipt, name):
    value = success(receipt, "read manuscript state " + client.label, client.rpc("get_manuscript", {"name": name}))
    receipt.check("history response shape", isinstance(value, dict) and isinstance(value.get("versions"), list)
                  and isinstance(value.get("reviews"), list) and isinstance(value.get("manuscript"), dict))
    return value


def snapshot(value):
    return (value["manuscript"]["revision"], value["manuscript"]["current_version"], value["manuscript"]["status"],
            tuple(row["name"] for row in value["versions"]),
            tuple((row["name"], row["status"]) for row in value["reviews"]))


def native_json_negative(client, receipt, doctype, name):
    prefix = client.label + " native " + doctype
    denied(receipt, prefix + " REST JSON", client.request("/api/resource/" + quote(doctype) + "/" + quote(name)))
    query = urllib.parse.urlencode({"doctype": doctype, "name": name})
    for command in ("frappe.client.get", "frappe.desk.form.load.getdoc", "frappe.desk.form.load.get_docinfo"):
        denied(receipt, prefix + " " + command, client.request("/api/method/" + command + "?" + query))
    query = urllib.parse.urlencode({"dt": doctype, "dn": name, "filters": "[]"})
    denied(receipt, prefix + " attachment loader", client.request(
        "/api/method/frappe.desk.form.load.get_filtered_attachments?" + query))


def literal_round_trip(args, receipt, author, teacher, licensed_docx):
    """Exact text from a real licensed DOCX derivative, through real HTTP saves.

    No model is contacted: the existing manual preview API proposes a fixed
    fictional AI-assisted draft; its independent confirmation path is real.
    """
    receipt.phase("literal DOCX source AI confirmation comment and reply byte preservation")
    original = "Literal <tag> & a < b > c; x <= y && y > z."
    title = "Fictional <tag> & a < b > c " + args.attempt
    anchor = "paragraph<1> & a < b > c"
    suggestion = "Fictional AI suggestion <tag> & retain a < b > c literally."
    reason = "Fictional reason <comparison> & a < b > c is source text."
    comment_text = "Fictional teacher <tag> & explain a < b > c."
    reply_text = "Fictional student <tag> & retained x < y > z."
    document = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t xml:space="preserve">' + escape(original)
                + '</w:t></w:r></w:p><w:sectPr/></w:body></w:document>')
    output = BytesIO()
    with ZipFile(BytesIO(licensed_docx)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info, document.encode("utf-8") if info.filename == "word/document.xml" else source.read(info))
    content = output.getvalue()
    content_sha = hashlib.sha256(content).hexdigest()
    source_sha = hashlib.sha256(original.encode("utf-8")).hexdigest()

    def exact(label, actual, expected):
        receipt.check(label, isinstance(actual, str) and actual.encode("utf-8") == expected.encode("utf-8")
                      and hashlib.sha256(actual.encode("utf-8")).digest() == hashlib.sha256(expected.encode("utf-8")).digest())

    manuscript = success(receipt, "literal author creates manuscript", author.rpc(
        "create_manuscript", {"title": title, "reviewer": ACCOUNTS["teacher"]}))
    name = manuscript["name"]
    receipt.artifact("literal_manuscript", name)
    exact("literal manuscript title bytes", manuscript.get("title"), title)
    filename = "rg-literal-<tag>&-" + args.attempt + ".docx"
    version = success(receipt, "literal licensed DOCX derivative upload", author.multipart(
        "research_group.manuscripts.upload_version", {"manuscript": name, "expected_revision": 0}, content, filename))
    receipt.artifact("literal_version", version["name"])
    exact("literal original filename bytes", version.get("original_filename"), filename)
    receipt.check("literal DOCX bytes are independently SHA bound", version.get("sha256") == content_sha
                  and version.get("size_bytes") == len(content))
    url, _, file_id = file_reference(receipt, version)
    receipt.artifact("literal_file", file_id)
    assert_download(teacher, receipt, "literal DOCX designated teacher original byte download", url, content)
    extracted = success(receipt, "literal source actual DOCX extraction", author.rpc("get_version_text", {"version": version["name"]}))
    paragraphs = extracted.get("paragraphs") or []
    receipt.check("literal DOCX contains exactly the expected extracted paragraph", len(paragraphs) == 1)
    exact("literal extraction UTF8 and SHA", paragraphs[0].get("text"), original)
    selection = success(receipt, "literal source explicit selection", author.rpc(
        "create_selection", {"version": version["name"], "original_text": original, "anchor": anchor}))
    receipt.artifact("literal_selection", selection["name"])
    exact("literal selected source UTF8 bytes", selection.get("original_text"), original)
    exact("literal selected anchor UTF8 bytes", selection.get("anchor"), anchor)
    receipt.check("literal source SHA matches actual original", selection.get("sha256") == source_sha
                  and selection.get("version_sha256") == content_sha)

    def ai(method, data=None):
        return author.request("/api/method/research_group.ai.api." + method, "POST", data or {})

    conversation = success(receipt, "literal AI private draft conversation", ai("create_conversation"))
    receipt.artifact("literal_ai_conversation", conversation["name"])
    pending = success(receipt, "literal AI suggestion independent preview", ai("propose_change", {
        "conversation": conversation["name"], "action": "manuscript_suggestion",
        "parameters": {"selection": selection["name"], "suggestion": suggestion, "reason": reason},
    }))
    receipt.artifact("literal_ai_operation", pending["name"])
    receipt.check("literal preview has not saved a formal review", not state(author, receipt, name)["reviews"])
    persisted_preview = success(receipt, "literal AI preview fresh HTTP read", ai("get_operation", {"operation_id": pending["name"]}))
    preview = persisted_preview.get("preview") or {}
    exact("literal preview original bytes", preview.get("original_text"), original)
    exact("literal preview suggestion bytes", (preview.get("parameters") or {}).get("suggestion"), suggestion)
    exact("literal preview reason bytes", (preview.get("parameters") or {}).get("reason"), reason)
    confirmed = success(receipt, "literal AI suggestion separately confirmed", ai(
        "decide_operation", {"operation_id": pending["name"], "decision": "confirm"}))
    receipt.check("literal AI confirmation consumed once", confirmed.get("status") == "consumed")
    saved_id = confirmed["result"]["name"]
    receipt.artifact("literal_ai_review", saved_id)
    again = success(receipt, "literal AI confirmation replay is idempotent", ai(
        "decide_operation", {"operation_id": pending["name"], "decision": "confirm"}))
    receipt.check("literal AI confirmation replay returns same review", again == confirmed)
    comment = success(receipt, "literal teacher saves comment", teacher.rpc(
        "add_review", {"version": version["name"], "content": comment_text, "anchor": anchor}))
    receipt.artifact("literal_teacher_comment", comment["name"])
    reply = success(receipt, "literal student saves reply", author.rpc(
        "reply_review", {"review": comment["name"], "content": reply_text}))
    receipt.artifact("literal_student_reply", reply["name"])
    success(receipt, "literal teacher closes comment without rewriting text", teacher.rpc(
        "resolve_review", {"review": comment["name"], "status": "closed"}))

    for client in (author, teacher):
        history = state(client, receipt, name)
        exact(client.label + " literal fresh title bytes", history["manuscript"].get("title"), title)
        exact(client.label + " literal fresh filename bytes", history["versions"][0].get("original_filename"), filename)
        rows = {row["name"]: row for row in history["reviews"]}
        receipt.check(client.label + " literal history contains exactly three reviews", set(rows) == {saved_id, comment["name"], reply["name"]})
        for field, expected in (("original_text", original), ("content", suggestion), ("reason", reason), ("anchor", anchor)):
            exact(client.label + " literal saved AI " + field, rows[saved_id].get(field), expected)
        receipt.check(client.label + " literal saved source SHA and version", rows[saved_id].get("source_sha256") == source_sha
                      and rows[saved_id].get("source_selection") == selection["name"]
                      and rows[saved_id].get("manuscript_version") == version["name"])
        exact(client.label + " literal comment bytes", rows[comment["name"]].get("content"), comment_text)
        exact(client.label + " literal comment anchor bytes", rows[comment["name"]].get("anchor"), anchor)
        exact(client.label + " literal reply bytes", rows[reply["name"]].get("content"), reply_text)
    fresh = success(receipt, "literal selection fresh HTTP read after confirmation", author.rpc("get_selection", {"name": selection["name"]}))
    exact("literal source still exact after confirmation", fresh.get("original_text"), original)
    receipt.check("literal source SHA remains stable", fresh.get("sha256") == source_sha)
    managed = success(receipt, "literal saved source remains owner-manageable", author.rpc("list_selections", {"manuscript": name}))
    source = next((row for row in managed.get("items", []) if row.get("name") == selection["name"]), {})
    receipt.check("literal management view binds exact own paragraph and saved suggestion",
                  source.get("original_text") == original and source.get("saved_suggestions") == 1
                  and source.get("private_user") == ACCOUNTS["author"])
    ai_reply = success(receipt, "literal author replies to saved AI advice", author.rpc(
        "reply_review", {"review": saved_id, "content": "Fictional follow-up on the selected advice."}))
    receipt.artifact("literal_ai_reply", ai_reply["name"])
    success(receipt, "literal selection explicit revoke after successful byte checks", author.rpc("revoke_selection", {"name": selection["name"]}))
    rows = {row["name"]: row for row in state(teacher, receipt, name)["reviews"]}
    receipt.check("literal revoked source blocks saved AI body", rows[saved_id].get("blocked") is True
                  and not {"original_text", "content", "reason", "anchor"}.intersection(rows[saved_id]))
    receipt.check("literal revoked source also blocks saved-advice follow-up body",
                  rows[ai_reply["name"]].get("blocked") is True and "content" not in rows[ai_reply["name"]])
    exact("literal source revoke retains independent teacher comment", rows[comment["name"]].get("content"), comment_text)
    exact("literal source revoke retains independent student reply", rows[reply["name"]].get("content"), reply_text)


def run(args, receipt):
    receipt.phase("load pinned fixture and fictional credentials")
    content = FIXTURE.read_bytes()
    receipt.check("licensed DOCX fixture exact bytes", len(content) == 14860
                  and hashlib.sha256(content).hexdigest() == FIXTURE_SHA256)
    passwords = cfg.credentials(args.credentials, required=ACCOUNTS.values())
    clients = {label: Client(args.base, receipt, label).login(passwords[email]) for label, email in ACCOUNTS.items()}
    del passwords
    author, teacher = clients["author"], clients["teacher"]
    title = "Fictional manuscript HTTP " + args.attempt

    receipt.phase("reject cross-origin and forged creation before valid creation")
    before_names = {row["name"] for row in all_manuscripts(author, receipt)}
    creation = {"title": title, "reviewer": ACCOUNTS["teacher"]}
    denied(receipt, "correct CSRF with foreign Origin rejects create", author.rpc(
        "create_manuscript", creation, headers={"Origin": EXTERNAL_ORIGIN}))
    denied(receipt, "foreign Referer without Origin rejects create", author.rpc(
        "create_manuscript", creation, headers={"Origin": None, "Referer": EXTERNAL_ORIGIN + "/form"}))
    denied(receipt, "missing Origin and Referer rejects create", author.rpc(
        "create_manuscript", creation, headers={"Origin": None, "Referer": None}))
    denied(receipt, "missing CSRF rejects create", author.rpc(
        "create_manuscript", creation, headers={"X-Frappe-CSRF-Token": None}), statuses=(400, 403))
    denied(receipt, "forged author rejects create", author.rpc(
        "create_manuscript", {**creation, "author": ACCOUNTS["other_student"]}))
    denied(receipt, "teacher cannot impersonate manuscript student", teacher.rpc("create_manuscript", creation))
    query = urllib.parse.urlencode(creation)
    denied(receipt, "GET cannot create manuscript", author.request(
        "/api/method/research_group.manuscripts.create_manuscript?" + query), statuses=(403, 405))
    receipt.check("rejected creations did not add an author manuscript",
                  {row["name"] for row in all_manuscripts(author, receipt)} == before_names)

    receipt.phase("student creates manuscript and uploads exact licensed DOCX")
    manuscript = success(receipt, "author creates manuscript", author.rpc("create_manuscript", creation))
    receipt.artifact("manuscript", manuscript["name"])
    name = manuscript["name"]
    receipt.check("server binds author reviewer and initial revision",
                  manuscript.get("author") == ACCOUNTS["author"] and manuscript.get("reviewer") == ACCOUNTS["teacher"]
                  and manuscript.get("revision") == 0)
    upload_fields = {"manuscript": name, "expected_revision": 0}
    command = "research_group.manuscripts.upload_version"
    filename = "rg-manuscript-" + args.attempt + ".docx"
    denied(receipt, "correct CSRF with foreign Origin rejects multipart", author.multipart(
        command, upload_fields, content, filename, headers={"Origin": EXTERNAL_ORIGIN}))
    denied(receipt, "forged uploader field rejects multipart", author.multipart(
        command, {**upload_fields, "submitted_by": ACCOUNTS["teacher"]}, content, filename))
    empty_history = state(author, receipt, name)
    receipt.check("rejected uploads retain revision zero and no version",
                  snapshot(empty_history)[:2] == (0, 0) and not empty_history["versions"])
    first = success(receipt, "author uploads DOCX draft", author.multipart(command, upload_fields, content, filename))
    receipt.artifact("version_1", first["name"])
    receipt.check("immutable version one hash size and server author",
                  first.get("sha256") == FIXTURE_SHA256 and first.get("size_bytes") == len(content)
                  and first.get("version") == 1 and first.get("submitted_by") == ACCOUNTS["author"])
    file_url, raw_url, file_id = file_reference(receipt, first)
    receipt.artifact("file_1", file_id)
    for client in (author, teacher):
        assert_download(client, receipt, client.label + " exact fid download", file_url, content)
        assert_download(client, receipt, client.label + " exact native path download", raw_url, content)
    zipped = teacher.request("/api/method/frappe.core.api.file.zip_files", "POST", {"files": json.dumps([file_id])})
    receipt.check("designated teacher native ZIP succeeds", zipped[0] == 200, zipped[0])
    with ZipFile(BytesIO(zipped[2])) as archive:
        receipt.check("designated teacher ZIP contains exact immutable DOCX", len(archive.infolist()) == 1
                      and archive.read(archive.infolist()[0]) == content)

    receipt.phase("teacher comment student reply and new draft preserve history")
    review_text = "Fictional HTTP review: clarify the independent error control."
    reply_text = "Fictional HTTP reply: the next version retains the control."
    review_fields = {"version": first["name"], "content": review_text, "anchor": "paragraph 1"}
    denied(receipt, "student cannot write teacher comment", author.rpc("add_review", review_fields))
    denied(receipt, "correct CSRF with foreign Origin rejects review", teacher.rpc(
        "add_review", review_fields, headers={"Origin": EXTERNAL_ORIGIN}))
    review = success(receipt, "designated teacher adds comment", teacher.rpc("add_review", review_fields))
    receipt.artifact("teacher_comment", review["name"])
    reply = success(receipt, "student replies to the exact comment", author.rpc(
        "reply_review", {"review": review["name"], "content": reply_text}))
    receipt.artifact("student_reply", reply["name"])
    receipt.check("reply preserves parent and immutable reviewed version",
                  reply.get("parent_review") == review["name"] and reply.get("manuscript_version") == first["name"])
    denied(receipt, "stale revision cannot create a second draft", author.multipart(
        command, {"manuscript": name, "expected_revision": 0}, content, "stale-" + filename), statuses=(409, 417))
    second = success(receipt, "student uploads revised immutable draft", author.multipart(
        command, {"manuscript": name, "expected_revision": 1, "parent_version": first["name"]},
        content, "revised-" + filename))
    receipt.artifact("version_2", second["name"])
    receipt.check("new draft chains original without replacing it", second.get("version") == 2
                  and second.get("parent_version") == first["name"] and second["name"] != first["name"])
    closed = success(receipt, "teacher closes addressed comment", teacher.rpc(
        "resolve_review", {"review": review["name"], "status": "closed"}))
    receipt.check("comment completion status", closed.get("status") == "closed")
    events = closed.get("status_history") or []
    receipt.check("human state transitions preserve exact actor previous next and action",
                  closed.get("status_history_complete") is True and
                  [(row.get("actor"), row.get("before"), row.get("after"), row.get("action")) for row in events]
                  == [(ACCOUNTS["author"], "open", "addressed", "reply"),
                      (ACCOUNTS["teacher"], "addressed", "closed", "resolve")])
    receipt.check("human state history contains bounded metadata and real timestamps only",
                  all(set(row) == {"actor", "at", "before", "after", "action"}
                      and isinstance(row.get("at"), str) and len(row["at"]) <= 40 for row in events)
                  and events[0]["at"] <= events[1]["at"]
                  and review_text not in json.dumps(events) and reply_text not in json.dumps(events))
    repeated = success(receipt, "teacher close retry retains the same state audit", teacher.rpc(
        "resolve_review", {"review": review["name"], "status": "closed"}))
    receipt.check("repeated closure does not manufacture a second event", repeated.get("status_history") == events)
    history = state(author, receipt, name)
    receipt.check("two versions and original comment reply remain intact",
                  history["manuscript"]["revision"] == 2 and len(history["versions"]) == 2
                  and {row["name"] for row in history["versions"]} == {first["name"], second["name"]}
                  and {row["name"]: row["content"] for row in history["reviews"]}
                  == {review["name"]: review_text, reply["name"]: reply_text})
    page = success(receipt, "version history page one", author.rpc("list_versions", {"manuscript": name, "limit": 1}))
    older = success(receipt, "version history page two", author.rpc("list_versions", {"manuscript": name, "limit": 1, "offset": 1}))
    receipt.check("version paging reaches original version", page.get("has_more") is True
                  and page["items"][0]["name"] == second["name"] and older["items"][0]["name"] == first["name"])
    page = success(receipt, "review history page one", teacher.rpc("list_reviews", {"manuscript": name, "limit": 1}))
    later = success(receipt, "review history page two", teacher.rpc("list_reviews", {"manuscript": name, "limit": 1, "offset": 1}))
    receipt.check("review paging reaches student reply", page.get("has_more") is True
                  and page["items"][0]["name"] == review["name"] and later["items"][0]["name"] == reply["name"])
    assert_download(teacher, receipt, "original version bytes survive revised draft", file_url, content)

    receipt.phase("explicit source selection owner restriction and revocation")
    extracted = success(receipt, "author extracts original version text", author.rpc("get_version_text", {"version": first["name"]}))
    paragraphs = extracted.get("paragraphs") or []
    original = next((row["text"].strip() for row in paragraphs if isinstance(row.get("text"), str) and row["text"].strip()), "")[:1500]
    receipt.check("licensed fixture exposes an actual nonempty paragraph", bool(original))
    selection_fields = {"version": first["name"], "original_text": original, "anchor": "paragraph 1"}
    denied(receipt, "forged selection owner rejects write", author.rpc(
        "create_selection", {**selection_fields, "private_user": ACCOUNTS["teacher"]}))
    denied(receipt, "correct CSRF with foreign Origin rejects selection", author.rpc(
        "create_selection", selection_fields, headers={"Origin": EXTERNAL_ORIGIN}))
    selection = success(receipt, "author explicitly selects source paragraph", author.rpc("create_selection", selection_fields))
    receipt.artifact("selection", selection["name"])
    receipt.check("selection is bound to immutable version and server owner", selection.get("version_sha256") == FIXTURE_SHA256
                  and selection.get("private_user") == ACCOUNTS["author"] and selection.get("original_text") == original)
    selected = success(receipt, "author can read own active selection", author.rpc("get_selection", {"name": selection["name"]}))
    receipt.check("selection read retains exact selected paragraph", selected.get("original_text") == original)
    managed = success(receipt, "author lists own source management state", author.rpc("list_selections", {"manuscript": name, "limit": 1}))
    receipt.check("selection manager returns only own readable source and capacity",
                  len(managed.get("items") or []) == 1 and managed["items"][0].get("name") == selection["name"]
                  and managed["items"][0].get("original_text") == original
                  and managed["items"][0].get("private_user") == ACCOUNTS["author"]
                  and managed.get("active_limit") == 20 and managed.get("active_count", 0) >= 1)
    teacher_managed = success(receipt, "designated teacher lists own selection management state", teacher.rpc(
        "list_selections", {"manuscript": name}))
    receipt.check("selection manager does not reveal another participant source",
                  teacher_managed.get("items") == [])
    denied(receipt, "forged selection management owner is rejected", author.rpc(
        "list_selections", {"manuscript": name, "private_user": ACCOUNTS["teacher"]}))
    for label in ("teacher", "other_student", "secretary"):
        denied(receipt, label + " cannot read another user's selection", clients[label].rpc("get_selection", {"name": selection["name"]}))

    receipt.phase("unrelated student and secretary denied dedicated and native aliases")
    baseline = snapshot(state(author, receipt, name))
    for label in ("other_student", "secretary"):
        client = clients[label]
        receipt.check(label + " list omits private manuscript", name not in {row["name"] for row in all_manuscripts(client, receipt)})
        for method, data in (
            ("get_manuscript", {"name": name}), ("list_versions", {"manuscript": name}),
            ("list_reviews", {"manuscript": name}), ("list_selections", {"manuscript": name}),
            ("get_version_text", {"version": first["name"]}),
            ("add_review", review_fields), ("reply_review", {"review": review["name"], "content": "forged"}),
            ("create_selection", selection_fields), ("revoke_selection", {"name": selection["name"]}),
        ):
            denied(receipt, label + " dedicated " + method, client.rpc(method, data))
        denied(receipt, label + " cannot append a foreign draft", client.multipart(
            command, {"manuscript": name, "expected_revision": 2}, content, label + "-" + filename))
        for url_label, url in (("fid", file_url), ("plain path", raw_url)):
            denied(receipt, label + " private download " + url_label, client.request(url), statuses=(403, 404))
        denied(receipt, label + " native ZIP download", client.request(
            "/api/method/frappe.core.api.file.zip_files", "POST", {"files": json.dumps([file_id])}))
        denied(receipt, label + " native File JSON", client.request("/api/resource/File/" + quote(file_id)))
        filters = urllib.parse.urlencode({"filters": json.dumps([["name", "=", file_id]]), "fields": '["name","file_url"]'})
        status, payload, _ = client.request("/api/resource/File?" + filters)
        receipt.check(label + " native File list excludes private file", status == 403
                      or (status == 200 and isinstance(payload, dict) and payload.get("data") == []), status)

    for client in (author, teacher, clients["other_student"], clients["secretary"]):
        for doctype, record in (("RG Manuscript", name), ("RG Manuscript Version", first["name"]),
                                ("RG Manuscript Review", review["name"]), ("RG Manuscript Selection", selection["name"])):
            native_json_negative(client, receipt, doctype, record)
    for client in (author, clients["other_student"], clients["secretary"]):
        alias_name = "alias-" + client.label + "-" + filename
        denied(receipt, client.label + " cannot insert detached File alias", client.request(
            "/api/resource/File", "POST", {"file_name": alias_name, "file_url": raw_url, "is_private": 1}))
        for native_command in ("upload_file", "frappe.handler.upload_file"):
            denied(receipt, client.label + " native attachment upload alias " + native_command, client.multipart(
                native_command, {"doctype": "RG Manuscript Version", "docname": first["name"], "is_private": 1},
                content, alias_name))
        denied(receipt, client.label + " cannot detach managed File with native update", client.request(
            "/api/resource/File/" + quote(file_id), "PUT", {"attached_to_doctype": "", "attached_to_name": ""}))
    receipt.check("all rejected writes preserve manuscript revisions and review history",
                  snapshot(state(author, receipt, name)) == baseline)
    assert_download(teacher, receipt, "file survives denied alias operations byte for byte", file_url, content)

    receipt.phase("explicit source revocation")
    denied(receipt, "foreign Origin cannot revoke own source selection", author.rpc(
        "revoke_selection", {"name": selection["name"]}, headers={"Origin": EXTERNAL_ORIGIN}))
    success(receipt, "selection remains active after denied foreign revocation", author.rpc("get_selection", {"name": selection["name"]}))
    revoked = success(receipt, "author revokes explicit source selection", author.rpc("revoke_selection", {"name": selection["name"]}))
    receipt.check("selection revoked status", revoked.get("status") == "revoked")
    denied(receipt, "revoked source cannot be read even by its owner", author.rpc("get_selection", {"name": selection["name"]}))
    managed = success(receipt, "revoked source removed from actionable own selection manager", author.rpc(
        "list_selections", {"manuscript": name}))
    receipt.check("revocation removes source management entry", managed.get("items") == [])
    receipt.check("selection revoke retains immutable collaboration history", snapshot(state(author, receipt, name)) == baseline)
    literal_round_trip(args, receipt, author, teacher, content)
    receipt.value.update(ok=True, complete=True, finished_utc=utc_now(), phase="complete",
                         cleanup="Two fictional manuscripts, three immutable versions, review/reply/AI records and revoked selections retained for central task cleanup")
    receipt.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", "--base-url", type=base_url, default="http://localhost:8089")
    parser.add_argument("--credentials", default=str(cfg.current().credentials_path))
    parser.add_argument("--attempt", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", args.attempt):
        parser.error("--attempt must be a unique 1-64 character ASCII slug")
    receipt = Receipt(args.attempt, args.base)
    try:
        run(args, receipt)
    except Exception as error:
        # Do not print str(error), a traceback, request URLs or response bodies:
        # those can contain credentials, source text or session state.
        receipt.value.update(error={"class": type(error).__name__, "phase": receipt.value["phase"]},
                             ok=False, complete=False, finished_utc=utc_now())
        receipt.save()
    print(json.dumps({"ok": receipt.value["ok"], "checks": len(receipt.value["checks"]),
                      "receipt": str(receipt.path)}, ensure_ascii=False))
    return 0 if receipt.value["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
