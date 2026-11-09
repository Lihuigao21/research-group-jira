"""Explicit run identity shared by the promoted role HTTP verifiers."""
from __future__ import annotations

from dataclasses import dataclass
import http.cookiejar
import json
from pathlib import Path
import re
import secrets
import socket
import subprocess
from types import SimpleNamespace
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parent
DEMO_USERS = ("teacher.demo@example.invalid", "student.one.demo@example.invalid",
              "student.two.demo@example.invalid", "secretary.demo@example.invalid")
_CURRENT = None


def localhost_ipv4_first(resolver):
    """Preserve DNS/HTTP identity; only prioritize the actual Docker bind."""
    def resolve(host, port, *args, **kwargs):
        rows = resolver(host, port, *args, **kwargs)
        return sorted(rows, key=lambda row: row[0] != socket.AF_INET) if host == "localhost" else rows

    resolve._role_localhost_ipv4_first = True
    return resolve


def install_localhost_ipv4_priority():
    if not getattr(socket.getaddrinfo, "_role_localhost_ipv4_first", False):
        socket.getaddrinfo = localhost_ipv4_first(socket.getaddrinfo)


def base_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                 and parsed.port is not None and 1 <= parsed.port <= 65535 and parsed.port != 8088
                 and not parsed.username and not parsed.password and parsed.path in {"", "/"}
                 and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Use an explicit loopback candidate HTTP port; port 8088 is excluded")
    return value.rstrip("/")


def safe_slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", value):
        raise ValueError("Attempt must be a unique 1-64 character ASCII slug")
    return value


def contained(path, parent):
    result, boundary = Path(path).resolve(), Path(parent).resolve()
    if result == boundary or not result.is_relative_to(boundary):
        raise ValueError("Output must stay inside the explicitly selected task")
    return result


@dataclass(frozen=True)
class Context:
    base: str
    credentials_path: Path
    container: str
    site: str
    task_dir: Path
    attempt: str

    @property
    def records(self):
        return contained(self.task_dir / "records/role-http" / self.attempt, self.task_dir)

    @property
    def scratch(self):
        return contained(self.task_dir / "work/role-http" / self.attempt, self.task_dir)

    def receipt(self, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json", name):
            raise ValueError("Receipt must be a simple JSON filename")
        return contained(self.records / name, self.records)

    def namespace(self):
        return SimpleNamespace(base=self.base, credentials=self.credentials_path, container=self.container,
                               site=self.site, task_dir=self.task_dir, attempt=self.attempt)

    def begin(self):
        # Reserve the identity before any HTTP/container operation. A failed
        # attempt remains occupied and requires a new name on the next run.
        self.records.parent.mkdir(parents=True, exist_ok=True)
        self.records.mkdir(exist_ok=False)
        self.scratch.parent.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(exist_ok=False)


def configure(args):
    global _CURRENT
    task = Path(args.task_dir).resolve()
    relative = task.relative_to((ROOT / "tasks").resolve())
    if len(relative.parts) != 1 or not (task / "TASK.md").is_file():
        raise ValueError("Select an existing project tasks/<task> directory containing TASK.md")
    private_path = Path(args.credentials).resolve()
    if not private_path.is_file() or private_path.suffix.lower() != ".json":
        raise ValueError("Credentials must be an existing private JSON file")
    if (private_path.is_relative_to((ROOT / "scripts").resolve())
            or private_path.is_relative_to((ROOT / "src").resolve())
            or private_path.is_relative_to((task / "records").resolve())):
        raise ValueError("Keep credentials outside source and acceptance records")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.container)
            or args.container.startswith("research-group-jira-")):
        raise ValueError("Select a separate candidate container, not the original application")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", args.site) or ".." in args.site:
        raise ValueError("Site must be a single safe Frappe site name")
    context = Context(base_url(args.base), private_path, args.container, args.site, task, safe_slug(args.attempt))
    if context.records.exists() or context.scratch.exists():
        raise ValueError("Attempt already exists; preserve it and choose a fresh name")
    install_localhost_ipv4_priority()
    _CURRENT = context
    return context


def current():
    if _CURRENT is None:
        raise RuntimeError("Use scripts/verify-role-http.py to bind a run identity first")
    return _CURRENT


def credentials(path=None, required=DEMO_USERS):
    source = Path(path).resolve() if path is not None else current().credentials_path
    if source != current().credentials_path:
        raise ValueError("Credentials must match the explicitly selected private JSON")
    value = json.loads(source.read_bytes())
    if isinstance(value, dict) and isinstance(value.get("passwords"), dict):
        value = value["passwords"]
    if not isinstance(value, dict) or any(not isinstance(value.get(user), str) or not value[user] for user in required):
        if "Administrator" in required:
            raise ValueError("legacy-core requires an Administrator key in the explicit credentials JSON")
        raise ValueError("Explicit credentials JSON is missing fictional demo account passwords")
    return value


class LocalRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        target = urllib.parse.urlsplit(urllib.parse.urljoin(request.full_url, new_url))
        origin = urllib.parse.urlsplit(current().base)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            raise RuntimeError("Cross-origin redirect rejected")
        return super().redirect_request(request, response, code, message, headers, new_url)


def private_python(source):
    ctx = current()
    process = subprocess.run(["docker", "exec", "-i", "--workdir", "/home/frappe/frappe-bench/sites",
                              ctx.container, "/home/frappe/frappe-bench/env/bin/python", "-"],
                             input=source.encode("utf-8"), capture_output=True, cwd=ROOT, timeout=90)
    if process.returncode:
        raise RuntimeError("Candidate private helper failed")
    try:
        return json.loads(process.stdout)
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("Candidate private helper returned an invalid frame") from None


def preflight():
    ctx = current()
    request = {"action": "preflight", "prefix": "rgah" + secrets.token_hex(5), "site": ctx.site, "base": ctx.base}
    source = (SOURCE / "accounts-http-fixture.py").read_bytes().decode("utf-8")
    admission = '''
def offline_admission(request):
    result = {"ok": False}
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            _connect_candidate(request)
            from research_group.ai.config import public_configuration
            if public_configuration()["mode"] != "mock":
                raise AssertionError("offline_mock_required")
            result = {"ok": True, "result": {"isolated_candidate": True, "mail_mode": "capture",
                      "native_mail_muted": True, "ai_mode": "mock"}}
    except BaseException:
        result = {"ok": False}
    finally:
        try:
            import frappe
            frappe.db.rollback()
            frappe.destroy()
        except BaseException:
            pass
    return result
'''
    result = private_python(source + admission + "\nprint(json.dumps(offline_admission(" + repr(request) + ")))\n")
    proof = result.get("result") or {}
    if not (result.get("ok") and proof.get("isolated_candidate") and proof.get("mail_mode") == "capture"
            and proof.get("native_mail_muted") and proof.get("ai_mode") == "mock"):
        raise RuntimeError("Isolated candidate database, capture-only mail and offline mock AI are required")
    return {"isolated_candidate": True, "mail_mode": "capture", "native_mail_muted": True, "ai_mode": "mock"}


def save_json(path, value):
    destination = contained(path, current().records)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return destination
