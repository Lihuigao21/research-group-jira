"""Shared real-HTTP checks against only the task's isolated loopback candidate."""
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import acceptance_config as cfg

ROOT = cfg.ROOT
TASK = cfg.current().task_dir
BASE = cfg.current().base
ACCOUNTS = {
    "teacher": "teacher.demo@example.invalid",
    "one": "student.one.demo@example.invalid",
    "two": "student.two.demo@example.invalid",
    "secretary": "secretary.demo@example.invalid",
}
RESULTS = []


def check(name, condition, **detail):
    RESULTS.append({"check": name, "passed": bool(condition), **detail})
    if not condition:
        raise AssertionError(name)


def credentials():
    return cfg.credentials()


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar), cfg.LocalRedirects())
        self.csrf = None

    def request(self, path, method="GET", data=None, raw=None, content_type=None, headers=None):
        cfg.base_url(BASE)
        if BASE != cfg.current().base or not path.startswith("/") or path.startswith("//"):
            raise AssertionError("Only paths on the configured loopback candidate are permitted")
        request_headers = {"Accept-Language": "zh-CN"}
        if method != "GET":
            request_headers["Origin"] = BASE
            request_headers["Referer"] = BASE + "/desk"
            if self.csrf:
                request_headers["X-Frappe-CSRF-Token"] = self.csrf
        if data is not None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        if content_type:
            request_headers["Content-Type"] = content_type
        if headers:
            for key, value in headers.items():
                if value is None:
                    request_headers.pop(key, None)
                else:
                    request_headers[key] = value
        request = urllib.request.Request(BASE + path, data=raw, headers=request_headers, method=method)
        try:
            response = self.opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error
        body = response.read()
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        return response.code, payload, body

    def login(self, key, passwords=None):
        passwords = passwords or credentials()
        return self.login_user(ACCOUNTS[key], passwords[ACCOUNTS[key]], key)

    def login_user(self, username, password, label):
        status, value, _ = self.request("/api/method/login", "POST", {"usr": username, "pwd": password})
        check("login " + label, status == 200 and (value or {}).get("message") == "Logged In", status=status)
        home = (value or {}).get("home_page") or "/desk"
        status, _, html = self.request(home if home.startswith("/") else "/" + home)
        match = re.search(rb"(?:frappe\.|window\.)?csrf_token[\"']?\s*[:=]\s*[\"']([^\"']+)", html)
        check("native CSRF " + label, status == 200 and match is not None, status=status)
        self.csrf = match[1].decode()
        return self

    def get(self, doctype, name):
        return self.request("/api/resource/" + urllib.parse.quote(doctype) + "/" + urllib.parse.quote(name))

    def multipart(self, command, fields, content, filename="validation.docx", headers=None):
        boundary = "RGRoleCandidateBoundary20261108"
        parts = []
        for key, value in fields.items():
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        parts.extend([content, f"\r\n--{boundary}--\r\n".encode()])
        return self.request("/api/method/" + command, "POST", raw=b"".join(parts),
                            content_type="multipart/form-data; boundary=" + boundary, headers=headers)


def rpc(client, method, **data):
    return client.request("/api/method/research_group." + method, "POST", data)


def success(label, response):
    status, payload, _ = response
    check(label, status == 200 and "message" in (payload or {}), status=status)
    return payload["message"]


def denied(label, response, statuses=(400, 403, 405, 417)):
    status, _, _ = response
    check(label, status in statuses, status=status)


def save_receipt(name, error=None, records=None):
    result = {"base_url": BASE, "fictional_only": True, "checks": RESULTS,
              "records": records or {}, "error": error, "ok": error is None and all(x["passed"] for x in RESULTS)}
    return cfg.save_json(cfg.current().receipt(name), result)
