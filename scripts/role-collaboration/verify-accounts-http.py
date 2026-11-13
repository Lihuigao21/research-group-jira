"""Real HTTP account acceptance, explicitly executed only by the integrator.

Default target: isolated candidate http://localhost:8089. No production writes.
Reads existing credential JSON, captures all helper output privately, and writes
only check labels/status codes/record IDs. No passwords, CSRF, links, cookies,
email bodies or raw HTTP responses are printed or saved in the report.
"""
import argparse
import copy
import http.cookiejar
import json
import queue
import re
import secrets
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import acceptance_config as cfg


ROOT = cfg.ROOT
TASK = cfg.current().task_dir
ACCOUNTS = {"secretary": "secretary.demo@example.invalid", "teacher": "teacher.demo@example.invalid"}
API = "/api/method/research_group.accounts."
RESET_ALIAS = "/api/method/frappe.core.doctype.user.user.reset_password"
PASSWORD_ALIAS = "/api/method/frappe.core.doctype.user.user.update_password"
PASSWORD = "A7!a-" + secrets.token_urlsafe(32)
NEXT_PASSWORD = "A7!a-" + secrets.token_urlsafe(32)
THIRD_PASSWORD = "A7!a-" + secrets.token_urlsafe(32)
NATIVE_SESSION_SHA256 = "3a8a2e1cc12b16b6ab4d4f001110cb25c8ec293fff132f4dd9068ebe4d69d8cf"
CHECKS = []
NATIVE_FAILURE_PHASES = frozenset({"resume", "command_wait", "command_read", "candidate_guard", "sql_before",
    "redis_before", "native_update", "native_commit", "redis_after", "sql_after", "verify_result", "stop",
    "eof", "invalid_frame", "unexpected_frame", "pipe_timeout"})


class NativeSessionProtocolFailure(RuntimeError):
    def __init__(self, phase):
        # Only fixed protocol phases may enter receipts; never reflect native
        # exception text, sid, Session data, or arbitrary helper output.
        self.phase = phase if isinstance(phase, str) and phase in NATIVE_FAILURE_PHASES else "unexpected_frame"
        super().__init__("native_session_private_pipe_failed")


def check(name, condition, *, status=None, record=None):
    item = {"check": name, "passed": bool(condition)}
    if status is not None:
        item["status"] = int(status)
    if record:
        item["record"] = str(record)
    CHECKS.append(item)
    if not condition:
        raise AssertionError(name)


def ok(name, response):
    status, value, _ = response
    check(name, status == 200 and isinstance(value, dict) and "message" in value, status=status)
    return value["message"]


def rejected(name, response, *, permission=False):
    status, _, _ = response
    check(name, status in ({401, 403} if permission else {400, 401, 403, 417, 422}), status=status)


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = None
        self.guest_csrf = None

    def clone(self):
        # A rejection response may clear cookies. Each negative probe must send
        # the original revoked sid, including the native endpoint probe.
        result = Client(self.base)
        for cookie in self.jar:
            result.jar.set_cookie(copy.copy(cookie))
        result.csrf, result.guest_csrf = self.csrf, self.guest_csrf
        return result

    def request(self, path, method="GET", data=None, *, origin=None, csrf=True, guest=True, api_token=None):
        headers = {"Accept-Language": "zh-CN"}
        if api_token is not None:
            headers["Authorization"] = "token " + api_token["api_key"] + ":" + api_token["api_secret"]
        if method != "GET":
            headers["Origin"] = self.base if origin is None else origin
            if csrf and self.csrf:
                headers["X-Frappe-CSRF-Token"] = self.csrf
            if guest and self.guest_csrf:
                headers["X-RG-CSRF-Token"] = self.guest_csrf
        raw = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=raw, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error
        body = response.read()
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            value = None
        return response.code, value, body

    def rpc(self, method, data=None, **options):
        return self.request(API + method, "POST", data or {}, **options)

    def guest_token(self):
        self.guest_csrf = ok("Guest双提交随机令牌", self.request(API + "csrf_token"))["token"]
        cookie = next((item for item in self.jar if item.name == "rg_account_csrf"), None)
        check("Guest令牌绑定HttpOnly SameSite cookie", bool(cookie and cookie.value == self.guest_csrf
              and cookie.has_nonstandard_attr("HttpOnly") and cookie.get_nonstandard_attr("SameSite") == "Lax"))
        return self

    def login(self, user, password, label):
        result = ok("原生登录 " + label, self.request("/api/method/login", "POST", {"usr": user, "pwd": password}))
        check("原生登录成功 " + label, result == "Logged In")
        status, _, body = self.request("/desk")
        match = re.search(rb"(?:frappe\.|window\.)?csrf_token[\"']?\s*[:=]\s*[\"']([^\"']+)", body)
        check("原生CSRF " + label, status == 200 and match is not None, status=status)
        self.csrf = match[1].decode("utf-8")
        return self

    def bootstrap(self, label="读取本人账号资料"):
        return ok(label, self.request(API + "bootstrap"))


class Fixture:
    def __init__(self, args, prefix):
        self.args = args
        self.prefix = prefix
        self.source = Path(__file__).with_name("accounts-http-fixture.py").read_bytes().decode("utf-8")
        self.legacy = None
        self.api_token_users = []

    def call(self, action, **values):
        request = {"action": action, "site": self.args.site, "base": self.args.base,
                   "expected_db_name": self.args.expected_db_name,
                   "prefix": self.prefix, **values}
        # Source + request travel over stdin, never process arguments/logs.
        source = self.source + "\nprint(json.dumps(private_main(" + repr(request) + ")))\n"
        process = subprocess.run(["docker", "exec", "-i", "--workdir", "/home/frappe/frappe-bench/sites",
                                  self.args.container, "/home/frappe/frappe-bench/env/bin/python", "-"],
                                 input=source.encode("utf-8"), capture_output=True, cwd=ROOT, timeout=90)
        if process.returncode:
            raise RuntimeError("candidate_helper_process_failed")
        try:
            result = json.loads(process.stdout)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("candidate_helper_response_invalid") from None
        if not result.get("ok"):
            # No captured response/error text is ever reflected in the report.
            label = result.get("check") or "candidate_helper_failed"
            check("候选辅助检查 " + label, False)
        return result["result"]

    def capture(self, template, user):
        value = self.call("capture", template=template, user=user)
        check("隔离捕获 " + template, value["status"] == "captured", record=value["event"])
        parsed = urllib.parse.urlsplit(value["link"])
        fragment = urllib.parse.parse_qs(parsed.fragment)
        key = "key" if template in {"registration_approved", "password_reset"} else "token"
        check("授权秘密仅在浏览器fragment " + template,
              len(fragment.get(key, [])) == 1 and key not in urllib.parse.parse_qs(parsed.query))
        return value, fragment[key][0]

    def native_session_flight(self, client, user):
        return NativeSessionFlight(self, client, user)

    def prepare_legacy_login_keys(self, client, user):
        sid = next((cookie.value for cookie in client.jar if cookie.name == "sid"), "")
        value = self.call("prepare_legacy_login_keys", user=user, sid=sid)
        self.legacy = {"user": user, "sid": sid, "key": value["key"], "login_token": value["login_token"]}
        check("候选夹具仅在私有缓存准备遗留登录授权", value.get("prepared") is True)

    def cleanup_legacy_login_keys(self):
        if self.legacy is None:
            return True
        try:
            success = self.call("cleanup_legacy_login_keys", **self.legacy).get("cleaned") is True
        except BaseException:
            success = False
        CHECKS.append({"check": "本次遗留登录key和sid-token夹具已清理", "passed": success})
        self.legacy = None
        return success

    def issue_api_token(self, user):
        # Register before issuing, so a partially failed helper is cleaned too.
        self.api_token_users.append(user)
        return self.call("issue_api_token", user=user)

    def cleanup_api_tokens(self):
        success = True
        for user in reversed(self.api_token_users):
            try:
                result = self.call("revoke_api_token", user=user)
                cleaned = result.get("cleaned") is True and result.get("member_restored") is True
            except BaseException:
                cleaned = False
            CHECKS.append({"check": "本次API token及临时成员状态已清理", "passed": cleaned})
            success = success and cleaned
        if success:
            self.api_token_users.clear()
        return success


class NativeSessionFlight:
    """Hold a real resumed Session in a second candidate process, over pipes."""
    BOOTSTRAP = ("import json,sys; packet=json.loads(sys.stdin.readline()); scope={}; "
                 "exec(compile(packet['source'],'accounts-http-fixture.py','exec'),scope); "
                 "scope['persistent_native_main'](packet['request'],sys.stdin,sys.stdout)")

    def __init__(self, fixture, client, user):
        self.fixture, self.client, self.user = fixture, client, user
        self.process = None
        self.messages = queue.Queue()
        self.iterations = 0

    def _read_frames(self):
        try:
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    value = {"ok": False, "stage": "invalid_frame"}
                if not isinstance(value, dict):
                    value = {"ok": False, "stage": "invalid_frame"}
                self.messages.put(value)
        finally:
            self.messages.put({"ok": False, "stage": "eof"})

    def _receive(self, stage):
        try:
            value = self.messages.get(timeout=35)
        except queue.Empty:
            raise NativeSessionProtocolFailure("pipe_timeout") from None
        if not value.get("ok") or value.get("stage") != stage:
            raise NativeSessionProtocolFailure(value.get("phase") or value.get("stage"))
        return value

    def _command(self, command):
        self.process.stdin.write(json.dumps({"command": command}) + "\n")
        self.process.stdin.flush()

    def __enter__(self):
        sid = next((cookie.value for cookie in self.client.jar if cookie.name == "sid"), "")
        check("在途原生Session使用真实登录sid", bool(sid and sid != "Guest"))
        args = self.fixture.args
        request = {"site": args.site, "base": args.base, "expected_db_name": args.expected_db_name,
                   "prefix": self.fixture.prefix,
                   "user": self.user, "sid": sid}
        self.process = subprocess.Popen([
            "docker", "exec", "-i", "--workdir", "/home/frappe/frappe-bench/sites", args.container,
            "/home/frappe/frappe-bench/env/bin/python", "-u", "-c", self.BOOTSTRAP],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1)
        # No sid, cookie, data, or helper source is placed in process arguments.
        threading.Thread(target=self._read_frames, daemon=True).start()
        try:
            self.process.stdin.write(json.dumps({"source": self.fixture.source, "request": request}) + "\n")
            self.process.stdin.flush()
            ready = self._receive("ready")
            check("独立持久进程真实resume同sid并释放SQL快照", ready["real_native_session_resumed"]
                  and ready["same_sid_retained"] and ready["database_snapshot_released"])
            check("候选原生Session源码与已读固定Frappe一致", ready.get("native_source_sha256") == NATIVE_SESSION_SHA256)
            return self
        except BaseException:
            self.close(require_clean=False)
            raise

    def refill(self, label):
        self._command("update")
        value = self._receive("updated")
        self.iterations += 1
        check(label, value.get("iteration") == self.iterations and all(value.get(key) is True for key in (
            "native_session_update_called", "native_update_returned_true", "sql_session_missing_before",
            "cache_absent_before", "late_cache_restored", "sql_session_missing_after")))

    def close(self, *, require_clean):
        if self.process is None:
            return
        clean = False
        try:
            if self.process.poll() is None:
                self._command("stop")
                stopped = self._receive("stopped")
                clean = stopped.get("updates") == self.iterations
            self.process.wait(timeout=10)
            clean = clean and self.process.returncode == 0
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
            pass
        finally:
            # Closing stdin also releases the helper if the host exits early;
            # its independent 90-second bound prevents an indefinite orphan.
            try:
                self.process.stdin.close()
            except OSError:
                pass
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if require_clean:
            check("在途原生Session进程已正常结束", clean)

    def __exit__(self, kind, value, traceback):
        self.close(require_clean=kind is None)


def scan_link(client, captured, fixture, user, state_action, state_field):
    before = fixture.call(state_action, user=user)
    parsed = urllib.parse.urlsplit(captured["link"])
    # Fragment is a browser-local value; HTTP GET sends no secret in its URL.
    path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
    status, _, _ = client.request(path)
    check("扫描验证链接GET成功且不消费授权", status == 200, status=status)
    after = fixture.call(state_action, user=user)
    check("GET未改变一次性授权状态", bool(before[state_field]) and before == after)


def stale_session_denied(client, label):
    rejected(label + "专用接口", client.clone().request(API + "bootstrap"), permission=True)
    rejected(label + "原生认证接口", client.clone().request("/api/method/frappe.auth.get_logged_user"), permission=True)


def load_credentials(path):
    return cfg.credentials(path, required=ACCOUNTS.values())


def native_link_login_closed(client, fixture, label, *, both_versions=False, sender=None):
    check(label + "以Guest而非已有登录cookie开始", not any(
        cookie.name == "sid" and cookie.value != "Guest" for cookie in client.jar))
    bases = ("/api/method/", "/api/v2/method/") if both_versions else ("/api/method/",)
    for base in bases:
        version = "v2" if "/v2/" in base else "v1"
        for method, field in (("login_via_key", "key"), ("login_via_token", "login_token")):
            probe = client.clone()
            # GET JSON body exercises the actual native GET consumer without
            # putting even this dead fixture secret into access-log query URLs.
            rejected(label + version + "原生GET替代登录被服务端拒绝 " + method,
                     probe.request(base + "frappe.www.login." + method,
                                   data={field: fixture.legacy[field]}), permission=True)
            check(label + version + "原生GET未创建登录sid " + method, not any(
                cookie.name == "sid" and cookie.value != "Guest" for cookie in probe.jar))
            rejected(label + version + "原生GET后仍不可调用认证接口 " + method,
                     probe.request("/api/method/frappe.auth.get_logged_user"), permission=True)
        rejected(label + version + "Guest不能发原生邮箱登录链接", client.request(
            base + "frappe.www.login.send_login_link", "POST", {"email": fixture.legacy["user"]}), permission=True)
        if sender is not None:
            rejected(label + version + "普通组员正确CSRF也不能发原生登录链接", sender.request(
                base + "frappe.www.login.send_login_link", "POST", {"email": fixture.legacy["user"]}), permission=True)
    state = fixture.call("legacy_login_keys_state", **fixture.legacy)
    check(label + "GET扫描未消费遗留key或sid-token", state["email_key_preserved"] and state["sid_token_preserved"])


def run(args):
    prefix = "rgah" + secrets.token_hex(5)
    fixture = Fixture(args, prefix)
    completed = False
    try:
        result = run_scenarios(args, fixture, prefix)
        completed = True
        return result
    finally:
        # Preserve User/application/audit evidence on failure, but remove only
        # the exact private legacy-key fixture entries owned by this run.
        tokens_cleaned = fixture.cleanup_api_tokens()
        cleaned = fixture.cleanup_legacy_login_keys()
        if not tokens_cleaned and completed:
            raise RuntimeError("api_token_fixture_cleanup_failed")
        if not cleaned and completed:
            raise RuntimeError("legacy_login_fixture_cleanup_failed")


def native_password_cases(args, fixture, prefix):
    """Original native endpoints remain usable for unrelated Frappe users."""
    user = prefix + "-native@fudan.edu.cn"
    created = fixture.call("prepare_native_account", user=user, password=PASSWORD)
    check("非课题组原生账号夹具未获组员权限", created["created"] and created["system_user"] and created["non_group"])
    before = fixture.call("native_password_state", user=user)
    plain = Client(args.base)  # Native pages do not supply the group Guest token.
    ok("非组员原生找回入口保留且不要求组员专用Guest令牌", plain.request(RESET_ALIAS, "POST", {"user": user}))
    reset = fixture.call("native_password_state", user=user)
    check("原生找回真实生成一次性key", reset["key_present"] and reset["generated"]
          and reset["key_digest"] != before["key_digest"])
    issued = fixture.call("issue_native_key", user=user)
    ok("非组员原生设密key经原端点消费", plain.request(PASSWORD_ALIAS, "POST", {
        "key": issued["key"], "new_password": NEXT_PASSWORD}))
    check("非组员原生设密消费key并清除摘要", not fixture.call("native_password_state", user=user)["key_present"])
    current = Client(args.base).login(user, NEXT_PASSWORD, "非组员设密后")
    rejected("原生恢复没有授予课题组业务权限", current.request(API + "bootstrap"), permission=True)
    ok("非组员原生旧密码改密保留", current.request(PASSWORD_ALIAS, "POST", {
        "old_password": NEXT_PASSWORD, "new_password": THIRD_PASSWORD, "logout_all_sessions": 0}))
    Client(args.base).login(user, THIRD_PASSWORD, "非组员改密后")


def notification_rpc(client, method, **values):
    return client.request("/api/method/research_group.notifications." + method, "POST", values)


def token_identity_cases(args, fixture, prefix, group_user):
    """Exercise real Authorization headers without a cookie/session fallback."""
    path = "/api/method/frappe.auth.get_logged_user"
    native_user = prefix + "-native@fudan.edu.cn"
    completed = False
    try:
        group_token = fixture.issue_api_token(group_user)
        native_token = fixture.issue_api_token(native_user)
        check("API token组员夹具已启用且成员有效", group_token["enabled"] and group_token["active"] and group_token["group"])
        check("API token非组员原生对照保持启用", native_token["enabled"] and not native_token["group"])
        # Every request starts with a new CookieJar; an earlier login cannot
        # satisfy any of these native endpoint assertions.
        check("活动组员token可调用原生身份接口", ok("活动组员token真实HTTP正例",
            Client(args.base).request(path, api_token=group_token)) == group_user)
        check("非组员token可调用原生身份接口", ok("非组员token真实HTTP对照",
            Client(args.base).request(path, api_token=native_token)) == native_user)
        paused = fixture.call("deactivate_token_member", user=group_user)
        check("仅成员active改变而原生User及token仍有效", paused["active"] is False
              and paused["enabled"] is True and paused["credentials_unchanged"] is True)
        rejected("User仍启用但成员单独停用后原生token接口拒绝",
                 Client(args.base).request(path, api_token=group_token), permission=True)
        check("成员停用不影响非组员原生token对照", ok("非组员token停用后对照",
            Client(args.base).request(path, api_token=native_token)) == native_user)
        completed = True
    finally:
        cleaned = fixture.cleanup_api_tokens()
        if not cleaned and completed:
            raise RuntimeError("api_token_fixture_cleanup_failed")
    rejected("清理后组员token不能再次认证", Client(args.base).request(path, api_token=group_token), permission=True)
    rejected("清理后非组员token不能再次认证", Client(args.base).request(path, api_token=native_token), permission=True)


def run_scenarios(args, fixture, prefix):
    preflight = fixture.call("preflight")
    check("独立候选数据库及邮件双重隔离", preflight["isolated_candidate"]
          and preflight["mail_mode"] == "capture" and preflight["native_mail_muted"])
    check("原生邮箱链接登录开关已关闭", preflight["native_email_link_enabled"] is False)
    passwords = load_credentials(args.credentials)
    secretary = Client(args.base).login(ACCOUNTS["secretary"], passwords[ACCOUNTS["secretary"]], "秘书")
    teacher = Client(args.base).login(ACCOUNTS["teacher"], passwords[ACCOUNTS["teacher"]], "教师")
    guest = Client(args.base).guest_token()
    native_password_cases(args, fixture, prefix)
    # Native recovery deliberately preserves its own muted Email Queue.
    # The group workflow must still create no native mail after that case.
    group_mail_baseline = fixture.call("preflight")["email_queue_count"]
    email = prefix + "-student@m.fudan.edu.cn"
    new_email = prefix + "-contact@fudan.edu.cn"
    student_id = ("HTTP" + prefix[4:] + "A").upper()
    registration = {"display_name": "虚构HTTP账号", "student_id": student_id, "email": email.upper()}

    rejected("Guest缺少CSRF不能注册", guest.rpc("request_registration", registration, guest=False), permission=True)
    rejected("Guest正确CSRF仍须同源来源", guest.rpc("request_registration", registration, origin=""), permission=True)
    rejected("Guest正确CSRF但外部Origin不能注册", guest.rpc("request_registration", registration,
              origin="https://external.invalid"), permission=True)
    rejected("Guest写接口拒绝参数注入", guest.rpc("request_registration", {**registration, "roles": ["RG Teacher"]}),
             permission=True)
    check("Guest失败写请求未创建申请", not fixture.call("registration", user=email)["exists"])
    for index, address in enumerate(("student@fudan.edu.cn.evil.invalid", "student@m.fudan.edu.cn.evil.invalid",
                                     "student@department.fudan.edu.cn", "student@fudan.edu.cn@evil.invalid",
                                     "Student <student@fudan.edu.cn>", "student@example.invalid"), 1):
        rejected("精确邮箱域边界 " + str(index), guest.rpc("request_registration", {**registration, "email": address}))
    generic = ok("精确m.fudan.edu.cn注册申请", guest.rpc("request_registration", registration))
    check("注册统一回复不返回申请ID或令牌", set(generic) == {"ok", "message"} and generic["ok"])
    state = fixture.call("registration", user=email)
    application = state["name"]
    check("待验证申请未创建User", state["status"] == "pending_verification" and not state["user_exists"], record=application)
    rejected("秘书不能跳过邮箱验证审批", secretary.rpc("review_registration", {
        "application": application, "decision": "approve"}))
    verification, token = fixture.capture("registration_verification", email)
    fault = fixture.call("mark_auth_failure", user=email, template="registration_verification", event=verification["event"])
    check("注册恢复使用本轮隔离已知失败夹具", fault["injected_known_failure"])
    recoverable = ok("秘书核对未登录申请人的失败回执", notification_rpc(secretary, "registration_mail_status",
        application=application, include_recovery=1))
    check("验证失败只返回原安全状态和事件恢复动作", recoverable["recovery"] == {verification["event"]: "reissue"}
          and token not in json.dumps(recoverable) and "body_ciphertext" not in json.dumps(recoverable))
    rejected("Guest不能按申请ID查询投递或授权恢复", notification_rpc(guest, "registration_mail_status",
        application=application, include_recovery=1), permission=True)
    rejected("教师不能恢复未入组申请的验证邮件", notification_rpc(teacher, "retry_event", name=verification["event"]), permission=True)
    rejected("恢复入口拒绝任意收件人注入", notification_rpc(secretary, "retry_event", name=verification["event"],
        recipient_email=prefix + "-other@fudan.edu.cn"), permission=True)
    old_verification, old_token = verification, token
    replacement = ok("秘书受控重新签发当前验证链接", notification_rpc(secretary, "retry_event", name=verification["event"]))
    verification, token = fixture.capture("registration_verification", email)
    check("重新签发产生独立事件和新一次性令牌", replacement["name"] == verification["event"]
          and verification["event"] != old_verification["event"] and token != old_token)
    rejected("重新签发后原验证令牌失效", guest.rpc("verify_registration", {"application": application, "token": old_token}))
    rejected("旧失败事件不能重复签发", notification_rpc(secretary, "retry_event", name=old_verification["event"]))
    scan_link(guest, verification, fixture, email, "registration", "token_present")
    ok("邮箱验证后进入人工审核", guest.rpc("verify_registration", {"application": application, "token": token}))
    rejected("邮箱验证令牌不可重复消费", guest.rpc("verify_registration", {"application": application, "token": token}))
    boot = secretary.bootstrap("秘书读取待审申请")
    pending = [row for row in boot["registrations"] if row["name"] == application]
    check("待审列表显示真实申请与安全邮件摘要", len(pending) == 1 and pending[0]["status"] == "awaiting_approval"
          and isinstance(pending[0]["mail_status"], list) and token not in json.dumps(boot))
    approved = ok("秘书审批通过", secretary.rpc("review_registration", {
        "application": application, "decision": "approve", "note": "虚构候选HTTP验收"}))
    check("审批创建固定原始登录ID", approved.get("user") == email and approved.get("status") == "approved")
    repeated = ok("重复审批幂等", secretary.rpc("review_registration", {"application": application, "decision": "approve"}))
    check("重复审批仍指向同一User", repeated.get("user") == email)
    before_existing = fixture.call("registration", user=email)
    occupied = ok("已有User的申请使用统一回复", guest.rpc("request_registration", {
        **registration, "student_id": student_id + "X"}))
    check("注册不能接管或覆盖已有User", occupied == generic
          and fixture.call("registration", user=email) == before_existing)
    initial, key = fixture.capture("registration_approved", email)
    check("设密邮件仅发给已验证初始邮箱", initial["recipient"] == email)
    rejected("弱密码不能消费原生一次性key", guest.request(PASSWORD_ALIAS, "POST", {
        "key": key, "new_password": "passwordpassword"}))
    ok("原生更新密码别名执行受控设密", guest.request(PASSWORD_ALIAS, "POST", {"key": key, "new_password": PASSWORD}))
    rejected("设密key不可重复消费", guest.rpc("update_password", {"key": key, "new_password": PASSWORD}))

    student = Client(args.base).login(email, PASSWORD, "新学生")
    token_identity_cases(args, fixture, prefix, email)
    other_session = Client(args.base).login(email, PASSWORD, "新学生第二会话")
    check("有效sid可调用原生认证接口", ok("原生身份接口正例", other_session.request(
        "/api/method/frappe.auth.get_logged_user")) == email)
    own = student.bootstrap()
    check("登录后只有本人账号面板", own["profile"]["login_id"] == email and not own["can_manage"]
          and not own["registrations"] and not own["members"])
    fixture.prepare_legacy_login_keys(student, email)
    native_link_login_closed(guest, fixture, "安全变更前：", both_versions=True, sender=student)
    rejected("学生不能审批", student.rpc("review_registration", {"application": application, "decision": "reject"}), permission=True)
    rejected("已登录正确原生CSRF仍拒绝外部Origin", student.rpc("update_profile", {"display_name": "不可生效"},
              origin="https://external.invalid"), permission=True)
    rejected("已登录正确原生CSRF仍须同源来源", student.rpc("update_profile", {"display_name": "不可生效"},
              origin=""), permission=True)
    before_missing_csrf = student.bootstrap("读取缺少原生CSRF负例前资料")["profile"]
    missing_csrf = student.rpc("update_profile", {"display_name": "不可生效"}, csrf=False)
    # Frappe rejects missing native CSRF with CSRFTokenError / HTTP 400 before
    # dispatching the service. Keep this exception local to the CSRF probe;
    # authorization probes continue to require 401/403 in rejected().
    check("已登录缺少原生CSRF拒绝写入", missing_csrf[0] in {400, 401, 403}, status=missing_csrf[0])
    check("缺少原生CSRF请求未修改本人资料", student.bootstrap("读取缺少原生CSRF负例后资料")["profile"]
          == before_missing_csrf)
    rejected("本人资料接口拒绝隐藏身份字段", student.rpc("update_profile", {
        "display_name": "不可生效", "email": new_email, "student_id": "REPLACED", "enabled": 1}), permission=True)
    ok("本人更新姓名电话简介", student.rpc("update_profile", {"display_name": "虚构HTTP更新姓名",
        "phone": "00000000000", "bio": "仅限隔离候选的虚构验收资料"}))
    profile = student.bootstrap()["profile"]
    check("资料保存不改变固定ID或联系邮箱", profile["display_name"] == "虚构HTTP更新姓名"
          and profile["phone"] == "00000000000" and profile["login_id"] == email
          and profile["student_id"] == student_id and profile["contact_email"] == email)
    for method, values in (("set_student_status", {"user": ACCOUNTS["teacher"], "enabled": 0}),
                           ("update_student", {"user": ACCOUNTS["teacher"], "display_name": "不可生效"}),
                           ("resend_set_password", {"user": ACCOUNTS["teacher"]})):
        rejected("秘书不可操作教师 " + method, secretary.rpc(method, values), permission=True)
    check("教师身份保持有效", teacher.bootstrap()["profile"]["login_id"] == ACCOUNTS["teacher"])

    rejected("变更联系邮箱须验证当前密码", student.rpc("request_contact_change", {
        "email": new_email, "password": "incorrect-current-password"}))
    ok("当前密码发起联系邮箱验证", student.rpc("request_contact_change", {"email": new_email, "password": PASSWORD}))
    check("新邮箱验证前原邮箱继续有效", student.bootstrap()["profile"]["contact_email"] == email)
    contact, contact_token = fixture.capture("contact_verification", email)
    check("联系邮箱验证邮件发给新邮箱", contact["recipient"] == new_email)
    scan_link(student, contact, fixture, email, "state", "pending_token_present")
    changed = ok("本人明确确认新联系邮箱", student.rpc("verify_contact_change", {"token": contact_token}))
    check("联系邮箱变更保持固定ID并递增版本", changed["login_id"] == email and changed["contact_email"] == new_email
          and changed["email_version"] == 2)
    rejected("联系邮箱令牌不可重用", student.rpc("verify_contact_change", {"token": contact_token}))
    stale_session_denied(other_session, "联系邮箱变更撤销其他旧sid：")
    check("联系邮箱确认保留当前会话", student.bootstrap()["profile"]["contact_email"] == new_email)
    native_link_login_closed(guest, fixture, "联系邮箱变更后：")
    rejected("联系邮箱不能替代固定原生登录ID", Client(args.base).request("/api/method/login", "POST", {
        "usr": new_email, "pwd": PASSWORD}))
    native = fixture.call("state", user=email)
    check("底层User.email未被联系邮箱覆盖", native["native_email"] == email and native["contact_email"] == new_email)

    unknown_response = guest.request(RESET_ALIAS, "POST", {"user": prefix + "-missing@fudan.edu.cn"})
    unknown = ok("未知身份找回使用统一回复", unknown_response)
    known_response = guest.request(RESET_ALIAS, "POST", {"user": new_email})
    known = ok("原生找回别名按已验证联系邮箱解析", known_response)
    check("已知未知找回回复一致", unknown == known == generic)
    check("已知未知找回完整响应形状一致且无身份专属消息通道",
          unknown_response[0] == known_response[0] == 200
          and unknown_response[1] == known_response[1]
          and set(known_response[1]) == {"message"}
          and "_server_messages" not in known_response[1])
    older, old_reset_key = fixture.capture("password_reset", email)
    ok("再次找回轮换原生key", guest.rpc("request_password_reset", {"user": new_email}))
    reset, reset_key = fixture.capture("password_reset", email)
    check("找回轮换生成新事件和key", reset["event"] != older["event"] and reset_key != old_reset_key
          and reset["recipient"] == new_email)
    fault = fixture.call("mark_auth_failure", user=email, template="password_reset", event=reset["event"])
    check("本人恢复使用本轮隔离已知失败夹具", fault["injected_known_failure"])
    own_recovery = ok("本人查看设密失败及可恢复动作", notification_rpc(student, "automatic_mail_status", include_recovery=1))
    check("本人只能获得当前设密事件的安全恢复标识", own_recovery["recovery"].get(reset["event"]) == "reissue"
          and reset_key not in json.dumps(own_recovery))
    rejected("教师不能恢复他人的设密事件", notification_rpc(teacher, "retry_event", name=reset["event"]), permission=True)
    failed_reset, failed_key = reset, reset_key
    ok("本人受控重新签发设密邮件", notification_rpc(student, "retry_event", name=reset["event"]))
    reset, reset_key = fixture.capture("password_reset", email)
    check("本人恢复固定收件邮箱且轮换失效key", reset["event"] != failed_reset["event"]
          and reset_key != failed_key and reset["recipient"] == new_email)
    rejected("本人恢复后旧设密key失效", guest.rpc("update_password", {"key": failed_key, "new_password": NEXT_PASSWORD}))
    rejected("旧找回key立即失效", guest.rpc("update_password", {"key": old_reset_key, "new_password": NEXT_PASSWORD}))
    status, _, _ = guest.request("/rg_join?mode=password")
    check("设密页面GET不消费新key", status == 200, status=status)
    ok("新找回key更新密码", guest.rpc("update_password", {"key": reset_key, "new_password": NEXT_PASSWORD}))
    stale_session_denied(student, "找回密码撤销当前旧sid：")
    native_link_login_closed(guest, fixture, "找回密码后：")
    rejected("已消费找回key不可重放", guest.rpc("update_password", {"key": reset_key, "new_password": NEXT_PASSWORD}))
    student = Client(args.base).login(email, NEXT_PASSWORD, "找回后学生")
    rejected("本人改密拒绝错误旧密码", student.rpc("change_password", {"old_password": PASSWORD, "new_password": THIRD_PASSWORD}))
    with fixture.native_session_flight(student, email) as flight:
        ok("本人使用当前密码修改网站密码", student.rpc("change_password", {"old_password": NEXT_PASSWORD,
            "new_password": THIRD_PASSWORD}))
        flight.refill("真实在途Session.update提交后证明SQL已删而Redis确实回填：原生接口")
        rejected("真实late Redis仍拒绝原sid调用原生认证接口", student.clone().request(
            "/api/method/frappe.auth.get_logged_user"), permission=True)
        # The previous guard deletes the stale cache. Reproduce the real native
        # write a second time, so the dedicated API also sees a live late cache.
        flight.refill("真实在途Session.update再次证明SQL已删而Redis确实回填：专用接口")
        rejected("真实late Redis仍拒绝原sid调用专用接口", student.clone().request(API + "bootstrap"), permission=True)
    stale_session_denied(student, "本人改密撤销当前旧sid：")
    native_link_login_closed(guest, fixture, "本人改密后：")
    student = Client(args.base).login(email, THIRD_PASSWORD, "改密后学生")
    ok("秘书可维护普通学生资料", secretary.rpc("update_student", {"user": email, "display_name": "虚构秘书维护姓名"}))
    check("秘书维护保留已核实学号", student.bootstrap()["profile"]["student_id"] == student_id)
    ok("秘书暂停普通学生", secretary.rpc("set_student_status", {"user": email, "enabled": 0}))
    paused = fixture.call("state", user=email)
    check("暂停同步原生User及成员且保存本站来源", paused["enabled"] == paused["active"] == 0
          and paused["service_pause_present"])
    stale_session_denied(student, "暂停撤销旧sid：")
    native_link_login_closed(guest, fixture, "秘书暂停后：")
    rejected("暂停后原生登录不可用", Client(args.base).request("/api/method/login", "POST", {"usr": email, "pwd": THIRD_PASSWORD}))
    ok("秘书可恢复本站暂停的普通学生", secretary.rpc("set_student_status", {"user": email, "enabled": 1}))
    stale_session_denied(student, "恢复后原sid仍不可复活：")
    student = Client(args.base).login(email, THIRD_PASSWORD, "恢复后学生")
    fixture.call("external_lock", user=email)
    rejected("秘书不能解除管理员或其他原生流程的锁定", secretary.rpc("set_student_status", {"user": email, "enabled": 1}))
    check("外部锁定保持生效", fixture.call("state", user=email)["enabled"] == 0)
    native_link_login_closed(guest, fixture, "管理员锁定后：")
    stale_session_denied(student, "外部原生锁定撤销旧sid：")

    # Both exact allowed domains are exercised through public registration.
    second_email = prefix + "-second@fudan.edu.cn"
    ok("精确fudan.edu.cn注册申请", guest.rpc("request_registration", {"display_name": "虚构第二申请",
        "student_id": student_id[:-1] + "B", "email": second_email}))
    second_state = fixture.call("registration", user=second_email)
    second_capture, second_token = fixture.capture("registration_verification", second_email)
    ok("第二精确域邮箱验证", guest.rpc("verify_registration", {"application": second_state["name"], "token": second_token}))
    refusal = ok("秘书可拒绝已验证申请", secretary.rpc("review_registration", {
        "application": second_state["name"], "decision": "reject", "note": "虚构候选拒绝分支"}))
    check("拒绝申请不创建User", refusal["status"] == "rejected"
          and not fixture.call("registration", user=second_email)["user_exists"])
    legitimate_email = prefix + "-legitimate@fudan.edu.cn"
    ok("拒绝后同一学号可以重新申请", guest.rpc("request_registration", {"display_name": "虚构重新申请",
        "student_id": student_id[:-1] + "B", "email": legitimate_email}))
    check("重新申请获得新身份而原拒绝历史仍在", fixture.call("registration", user=legitimate_email)["status"] == "pending_verification"
          and fixture.call("registration", user=second_email)["status"] == "rejected")
    final = fixture.call("finale", user=email)
    check("所有邮件留在捕获队列且无SMTP结果", final["event_count"] > 0 and final["smtp_outcomes"] == 0)
    check("账号闭环未新增原生Email Queue", final["email_queue_count"] == group_mail_baseline)
    check("本次组员前缀仍未创建原生Email Queue", final["group_native_email_queue_count"] == 0)
    check("非组员原生恢复保留队列且静音未提交", final["native_email_queue_count"] >= 1
          and final["native_email_sent_count"] == 0)
    return {"prefix": prefix, "mail_mode": "capture", "real_smtp_sent": False,
            "native_session_source_sha256": NATIVE_SESSION_SHA256,
            "native_session_late_update_iterations": 2}


def main():
    args = cfg.current().namespace()
    report_path = cfg.current().receipt("accounts-http-" + args.attempt + ".json")
    passed, error_type, error_phase, context = False, None, None, {}
    try:
        context = run(args)
        passed = True
    except BaseException as error:
        error_type = type(error).__name__
        if isinstance(error, NativeSessionProtocolFailure):
            error_phase = error.phase
    report = {"ok": passed, "created_at": datetime.now(timezone.utc).isoformat(),
              "base": args.base, "checks": CHECKS, "error_type": error_type, "error_phase": error_phase, **context,
              "secret_policy": "No credentials, tokens, cookies, mail bodies, links, or raw responses recorded"}
    cfg.save_json(report_path, report)
    print(json.dumps({"ok": passed, "total": len(CHECKS), "report": str(report_path)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
