"""Exercise the real HTTP boundary using only this project's fictional accounts."""
import concurrent.futures
import hashlib
import http.cookiejar
import json
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks/20261106-frappe-v1"
BASE = "http://localhost:8088"
ACCOUNTS = {
    "teacher": "teacher.demo@example.invalid",
    "one": "student.one.demo@example.invalid",
    "two": "student.two.demo@example.invalid",
    "secretary": "secretary.demo@example.invalid",
}
RESULTS = []
IDS = {}
EXTRA_TEACHER = {}


def check(name, condition, **detail):
    RESULTS.append({"check": name, "passed": bool(condition), **detail})
    if not condition:
        raise AssertionError(name)


def pdf_bytes(label="RG fictional HTTP validation"):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources <<>> /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
        f"<< /Title ({label}) >>".encode("ascii"),
    ]
    data, offsets = bytearray(b"%PDF-1.4\n%UTF8\n"), [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R /Info 5 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(data)


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.csrf = None

    def request(self, path, method="GET", data=None, raw=None, content_type=None):
        headers = {"Accept-Language": "zh-CN"}
        if data is not None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if self.csrf and method != "GET":
            headers["X-Frappe-CSRF-Token"] = self.csrf
        request = urllib.request.Request(BASE + path, data=raw, headers=headers, method=method)
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

    def login(self, key, passwords):
        return self.login_user(ACCOUNTS[key], passwords[ACCOUNTS[key]], key)

    def login_user(self, username, password, label):
        status, value, _ = self.request("/api/method/login", "POST", {
            "usr": username, "pwd": password,
        })
        check("登录 " + label, status == 200 and value.get("message") == "Logged In", status=status)
        home = value.get("home_page") or "/desk"
        if not home.startswith("/"):
            home = "/" + home
        status, _, html = self.request(home)
        match = re.search(rb"(?:frappe\.|window\.)?csrf_token[\"']?\s*[:=]\s*[\"']([^\"']+)", html)
        check("原生 CSRF " + label, status == 200 and match is not None, status=status)
        self.csrf = match[1].decode()
        return self

    def rpc(self, method, data=None):
        return self.request("/api/method/research_group.services." + method, "POST", data or {})

    def create(self, doctype, fields):
        status, value, _ = self.request("/api/resource/" + urllib.parse.quote(doctype), "POST", fields)
        check("创建 " + doctype, status == 200 and bool(value.get("data", {}).get("name")), status=status)
        return value["data"]

    def get(self, doctype, name):
        return self.request("/api/resource/" + urllib.parse.quote(doctype) + "/" + urllib.parse.quote(name))

    def upload(self, method, fields, content, filename="validation.pdf"):
        return self.multipart("research_group.services." + method, fields, content, filename)

    def multipart(self, command, fields, content, filename="validation.pdf"):
        boundary = "RGValidationBoundary20261106"
        pieces = []
        for key, value in fields.items():
            pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
        pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n').encode())
        pieces.extend([content, f"\r\n--{boundary}--\r\n".encode()])
        return self.request("/api/method/" + command, "POST",
                            raw=b"".join(pieces), content_type="multipart/form-data; boundary=" + boundary)

    def count(self, doctype, filters=None):
        query = urllib.parse.urlencode({"doctype": doctype, "filters": json.dumps(filters or {})})
        status, value, _ = self.request("/api/method/frappe.client.get_count?" + query)
        check("读取 " + doctype + " 数量", status == 200 and isinstance(value.get("message"), int), status=status)
        return value["message"]


def native_temp_paths(filenames):
    """Read only the exact local paths used by this run's native upload cases."""
    if any(not re.fullmatch(r"rg-http-[a-z0-9-]+\.pdf", name) for name in filenames):
        raise ValueError("Unexpected HTTP probe filename")
    probe = (
        "import json,os,sys; from pathlib import Path; "
        "root=Path('/home/frappe/frappe-bench/sites')/os.environ.get('SITE_NAME','lab.localhost'); "
        "paths=[root/area/'files'/('.temp-'+name) for area in ('public','private') for name in sys.argv[1:]]; "
        "print(json.dumps([str(path) for path in paths if path.exists()]))"
    )
    result = subprocess.run([
        "docker", "compose", "--env-file", "deploy/.env", "-f", "deploy/compose.yaml",
        "exec", "-T", "backend", "python", "-c", probe, *filenames,
    ], cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def verify_native_upload_guard(clients, administrator, content, nonce):
    cases = []
    for key in ("teacher", "one", "secretary"):
        for spelling, command in (("short", "upload_file"), ("qualified", "frappe.handler.upload_file")):
            for private in (0, 1):
                for chunk in (False, True):
                    filename = f"rg-http-{nonce}-{key}-{spelling}-{private}-{int(chunk)}.pdf"
                    fields = {"is_private": private}
                    if chunk:
                        fields.update(chunk_index=0, total_chunk_count=2, chunk_byte_offset=0,
                                      total_file_size=len(content) * 2)
                    cases.append((key, command, filename, fields))
    names = [case[2] for case in cases]
    check("原生上传探针开始前无临时文件", not native_temp_paths(names))
    before = administrator.count("File")
    try:
        for key, command, filename, fields in cases:
            status, _, _ = clients[key].multipart(command, fields, content, filename)
            check("HTTP 原生上传写前拒绝 " + filename, status == 403, status=status)
    finally:
        remaining = native_temp_paths(names)
        check("HTTP 普通及非末块上传未写公开或私有临时文件", not remaining, paths=remaining)
    check("HTTP 原生上传拒绝不增加 File", administrator.count("File") == before)
    # Operational Administrator retains the pinned native handler and multipart path.
    status, value, _ = administrator.multipart("frappe.handler.upload_file", {"is_private": 1},
        pdf_bytes("RG native Administrator " + nonce), f"rg-http-{nonce}-admin.pdf")
    check("HTTP 运维管理员保留原生上传", status == 200 and bool((value or {}).get("message", {}).get("name")), status=status)


def local_administrator():
    # Executed only by this explicit local verifier; never prints or records credentials.
    values = {}
    for line in (ROOT / "deploy/.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("ADMIN_PASSWORD="):
            values["password"] = line.partition("=")[2].strip().strip("\"'")
    if not values.get("password"):
        raise RuntimeError("Local ADMIN_PASSWORD is required for isolated HTTP fixtures")
    return Client().login_user("Administrator", values["password"], "本机验收管理员")


def cleanup_extra_teacher():
    """Delete only the account exclusively created by this successful verification."""
    if not EXTRA_TEACHER:
        return
    administrator, email = EXTRA_TEACHER["administrator"], EXTRA_TEACHER["email"]
    if not re.fullmatch(r"http-review-[a-f0-9]{12}@example\.invalid", email):
        raise ValueError("Unexpected extra teacher identity")
    doctypes = ("RG Topic", "RG Progress", "RG Appointment", "RG Discussion Record",
                "RG Group Meeting", "RG Meeting Report", "RG Material Version", "File")
    referenced = any(administrator.count(doctype, {"owner": email}) for doctype in doctypes)
    referenced = referenced or any(administrator.count(doctype, {"teacher": email})
                                  for doctype in ("RG Appointment", "RG Discussion Record"))
    referenced = referenced or bool(administrator.count("RG Group Meeting", {"publisher": email}))
    check("额外教师无业务引用才回收", not referenced)
    if EXTRA_TEACHER.get("member_created"):
        status, value, _ = administrator.get("RG Member", email)
        check("回收仅针对本次虚构成员", status == 200 and value["data"].get("user") == email
              and value["data"].get("is_demo") == 1, status=status)
        status, _, _ = administrator.request("/api/resource/RG%20Member/" + urllib.parse.quote(email), "DELETE")
        check("回收本次额外成员", status == 202, status=status)
    status, _, _ = administrator.request("/api/resource/User/" + urllib.parse.quote(email), "DELETE")
    check("回收本次额外教师账号", status == 202, status=status)
    IDS["unrelated_teacher_cleaned"] = True


def main():
    (TASK / "work").mkdir(parents=True, exist_ok=True)
    (TASK / "records").mkdir(parents=True, exist_ok=True)
    passwords = json.loads((ROOT / "runtime/secrets/demo-credentials.json").read_text(encoding="utf-8"))
    clients = {key: Client().login(key, passwords) for key in ACCOUNTS}
    teacher, one, two, secretary = (clients[key] for key in ("teacher", "one", "two", "secretary"))
    administrator = local_administrator()
    nonce = secrets.token_hex(6)
    unrelated_email = f"http-review-{nonce}@example.invalid"
    unrelated_password = secrets.token_urlsafe(32)
    administrator.create("User", {"email": unrelated_email, "first_name": "虚构 HTTP 无关教师",
        "enabled": 1, "user_type": "System User", "language": "zh", "send_welcome_email": 0,
        "new_password": unrelated_password, "roles": [{"role": "课题组教师"}]})
    EXTRA_TEACHER.update(administrator=administrator, email=unrelated_email)
    administrator.create("RG Member", {"user": unrelated_email, "display_name": "虚构 HTTP 无关教师",
        "active": 1, "is_demo": 1})
    EXTRA_TEACHER["member_created"] = True
    unrelated_teacher = Client().login_user(unrelated_email, unrelated_password, "虚构无关教师")
    IDS["unrelated_teacher"] = unrelated_email
    verify_native_upload_guard(clients, administrator, pdf_bytes(), nonce)
    label = "HTTP 验证（虚构）" + datetime.now().strftime("%H%M%S")
    # Each run retains its fictional evidence. Allocate after approved slots so
    # running verification again does not collide with a previous run.
    query = urllib.parse.urlencode({"fields": json.dumps(["end_at"]), "filters": json.dumps({
        "teacher": ACCOUNTS["teacher"], "status": "已批准",
    }), "order_by": "end_at desc", "limit_page_length": 1})
    status, value, _ = teacher.request("/api/resource/RG%20Appointment?" + query)
    check("读取独立验收时段", status == 200 and isinstance(value.get("data"), list), status=status)
    start = datetime(2030, 2, 1, 14)
    if value["data"]:
        start = max(start, datetime.fromisoformat(value["data"][0]["end_at"]) + timedelta(days=1))
    end = start + timedelta(hours=1)
    topics = {key: teacher.create("RG Topic", {
        "title": label + key, "student": ACCOUNTS[key], "direction": "虚构方向", "priority": "中",
        "status": "进行中", "next_milestone": "虚构验收里程碑",
    })["name"] for key in ("one", "two")}
    appointments = {key: clients[key].create("RG Appointment", {
        "topic": topics[key], "teacher": ACCOUNTS["teacher"], "start_at": start.isoformat(" "),
        "end_at": end.isoformat(" "), "agenda": label,
    })["name"] for key in ("one", "two")}
    IDS.update(topics=topics, appointments=appointments)
    second_teacher = Client().login("teacher", passwords)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.rpc, "decide_appointment", {"name": name, "decision": "已批准"})
                   for client, name in zip((teacher, second_teacher), appointments.values())]
        statuses = [future.result()[0] for future in futures]
    check("HTTP 重叠审批最多一个成功", statuses.count(200) == 1, statuses=statuses)
    states = {key: clients[key].get("RG Appointment", name)[1]["data"]["status"] for key, name in appointments.items()}
    check("冲突项保留待审批", sorted(states.values()) == sorted(["已批准", "待审批"]))
    status, _, _ = one.rpc("decide_appointment", {"name": appointments["one"], "decision": "已批准"})
    check("HTTP 学生不可批准", status == 403, status=status)
    pending = next(key for key, state in states.items() if state == "待审批")
    status, _, _ = clients[pending].rpc("cancel_appointment", {"name": appointments[pending]})
    check("HTTP 申请人取消", status == 200, status=status)
    status, _, _ = clients[pending].rpc("archive_discussion", {"name": appointments[pending], "conclusion": label, "actions": label})
    check("HTTP 取消后不可归档", status == 417, status=status)

    content = pdf_bytes()
    (TASK / "work/ui-validation.pdf").write_bytes(content)
    status, value, _ = one.upload("upload_discussion_material", {"doctype": "RG Appointment", "name": appointments["one"]}, content)
    check("HTTP 私人材料上传", status == 200, status=status)
    private = value["message"]
    IDS["private_file"] = private
    native_zip = "/api/method/frappe.core.api.file.zip_files"
    status, _, archive_bytes = one.request(native_zip, "POST", {"files": [private["name"]]})
    check("HTTP 原生 ZIP 本人私人材料", status == 200, status=status)
    with ZipFile(BytesIO(archive_bytes)) as archive:
        check("HTTP 私人 ZIP 保留原字节", len(archive.namelist()) == 1 and archive.read(archive.namelist()[0]) == content)
    for key in ("two", "secretary"):
        status, _, body = clients[key].request(native_zip, "POST", {"files": [private["name"]]})
        check("HTTP 原生 ZIP 越权私人拒绝 " + key, status == 403 and not body.startswith(b"PK"), status=status)
    for key, client, expected in (("owner", one, 200), ("teacher", teacher, 200), ("other", two, 403),
                                  ("combined", secretary, 403), ("guest", Client(), 403)):
        status, _, body = client.request(private["file_url"])
        check("私人直链 " + key, status == expected and (body == content if expected == 200 else b"%PDF-" not in body), status=status)
    status, _, _ = two.get("RG Appointment", appointments["one"])
    check("HTTP 私人记录拒绝", status == 403, status=status)
    query = urllib.parse.urlencode({"fields": json.dumps(["name", "file_url"]), "filters": json.dumps({"name": private["name"]})})
    status, value, _ = secretary.request("/api/resource/File?" + query)
    check("HTTP 附件列表不泄露", status == 200 and value.get("data") == [], status=status)
    status, _, _ = one.request("/api/resource/File/" + private["name"], "PUT", {"is_private": 0})
    check("HTTP 材料不可转公开", status == 403, status=status)

    approved = next(key for key, state in states.items() if state == "已批准")
    status, value, _ = teacher.rpc("archive_discussion", {"name": appointments[approved],
        "conclusion": label + "结论", "actions": label + "后续行动"})
    check("HTTP 建立独立归档讨论夹具", status == 200, status=status)
    archived = value["message"]["name"]
    IDS["discussion_record"] = archived
    for doctype, name in (("RG Appointment", appointments["one"]), ("RG Discussion Record", archived)):
        fields = {"doctype": doctype, "name": name}
        before = administrator.count("File")
        status, _, _ = unrelated_teacher.upload("upload_discussion_material", fields, content)
        check("HTTP 无关教师不可上传 " + doctype, status == 403, status=status)
        check("HTTP 无关教师拒绝不增加 File " + doctype, administrator.count("File") == before)
        status, value, _ = teacher.upload("upload_discussion_material", fields, content)
        check("HTTP 指定教师可上传 " + doctype, status == 200 and bool(value.get("message", {}).get("name")), status=status)

    meeting = secretary.create("RG Group Meeting", {"title": label, "start_at": "2030-02-02 14:00:00",
                                                      "end_at": "2030-02-02 16:00:00", "location": "虚构会议室"})
    status, value, _ = secretary.rpc("assign_reporter", {"meeting": meeting["name"], "student": ACCOUNTS["one"]})
    check("HTTP 指定报告人", status == 200, status=status)
    report = value["message"]["name"]
    status, _, _ = secretary.rpc("publish_meeting", {"name": meeting["name"]})
    check("HTTP 发布组会", status == 200, status=status)
    IDS.update(meeting=meeting["name"], report=report)
    fields = {"report": report, "topic": label}
    for name, client, payload, expected in (
        ("非报告人", two, content, 403), ("伪 PDF", one, b"%PDF-fake\n%%EOF", 417),
        ("超过50MB", one, b"x" * (50 * 1024 * 1024 + 1), 417),
    ):
        status, _, _ = client.upload("upload_report_material", fields, payload)
        check("HTTP 拒绝 " + name, status == expected, status=status)
        check(name + "失败不产生提交", one.get("RG Meeting Report", report)[1]["data"]["latest_version"] == 0)
    status, value, _ = one.upload("upload_report_material", fields, content, "first.pdf")
    check("HTTP 首次材料按时", status == 200 and value["message"]["version"] == 1 and value["message"]["late"] == 0, status=status)
    status, _, _ = secretary.request("/api/resource/RG%20Group%20Meeting/" + meeting["name"], "PUT", {"deadline": "2026-11-05 23:59:00"})
    check("HTTP 发布者调整截止", status == 200, status=status)
    status, value, _ = one.upload("upload_report_material", fields, content, "second.pdf")
    check("HTTP 重传产生迟交新版本", status == 200 and value["message"]["version"] == 2 and value["message"]["late"] == 1, status=status)
    public_url = value["message"]["file_url"]
    status, version_row, _ = one.get("RG Material Version", value["message"]["name"])
    check("HTTP 公开材料 File 可见", status == 200 and bool(version_row.get("data", {}).get("material_file")), status=status)
    public_file = version_row["data"]["material_file"]
    status, _, archive_bytes = two.request(native_zip, "POST", {"files": [public_file]})
    check("HTTP 原生 ZIP 已发布材料", status == 200, status=status)
    with ZipFile(BytesIO(archive_bytes)) as archive:
        check("HTTP 公开 ZIP 保留原字节", len(archive.namelist()) == 1 and archive.read(archive.namelist()[0]) == content)
    for names in ([public_file, private["name"]], [private["name"], public_file]):
        status, _, body = two.request(native_zip, "POST", {"files": names})
        check("HTTP 原生 ZIP 混合权限整包拒绝", status == 403 and not body.startswith(b"PK"), status=status)
    for key, client in clients.items():
        status, _, body = client.request(public_url)
        check("已发布材料可读 " + key, status == 200 and hashlib.sha256(body).digest() == hashlib.sha256(content).digest(), status=status)
    path = "/api/method/research_group.services.material_history?" + urllib.parse.urlencode({"report": report})
    status, value, _ = two.request(path)
    rows = value["message"]
    check("HTTP 旧版及原迟交标记保留", status == 200 and [row["version"] for row in rows] == [2, 1]
          and [row["late"] for row in rows] == [1, 0])


if __name__ == "__main__":
    error = None
    try:
        main()
    except Exception as exception:
        error = type(exception).__name__ + ": " + str(exception)
    finally:
        if error is None:
            try:
                cleanup_extra_teacher()
            except Exception as exception:
                error = type(exception).__name__ + ": " + str(exception)
        if error and EXTRA_TEACHER:
            IDS["unrelated_teacher_retained_for_diagnosis"] = EXTRA_TEACHER["email"]
    output = {"base_url": BASE, "fictional_only": True, "checks": RESULTS, "records": IDS, "error": error}
    destination = TASK / "records/http-verification.json"
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": sum(item["passed"] for item in RESULTS), "total": len(RESULTS), "error": error}, ensure_ascii=False))
    sys.exit(1 if error else 0)
