"""Container-side acceptance helper. Called only by verify-backup-restore.py."""
import contextlib
import gzip
import hashlib
import http.cookiejar
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BENCH = Path("/home/frappe/frappe-bench")
SITES = BENCH / "sites"
DOCTYPES = (
    "RG Member", "RG Topic", "RG Progress", "RG Appointment", "RG Discussion Record",
    "RG Group Meeting", "RG Meeting Report", "RG Material Version",
)
ACCOUNTS = {
    "teacher": "teacher.demo@example.invalid", "one": "student.one.demo@example.invalid",
    "two": "student.two.demo@example.invalid", "secretary": "secretary.demo@example.invalid",
}


def require(condition, label):
    if not condition:
        raise AssertionError(label)


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def connect(site):
    import frappe
    frappe.destroy()
    frappe.init(site)
    frappe.connect()
    frappe.set_user("Administrator")
    config = json.loads((SITES / site / "site_config.json").read_text())
    require(frappe.db.sql("SELECT DATABASE()")[0][0] == config["db_name"], "connected_to_expected_database")
    return config


def snapshot(site):
    import frappe
    from research_group.audit import material_integrity
    from research_group.files import read_stored_file_bytes
    connect(site)
    rows = {dt: frappe.get_all(dt, fields=["*"], order_by="name", limit_page_length=0) for dt in DOCTYPES}
    names = {dt: {row.name for row in values} for dt, values in rows.items()}
    links = {}
    for child, field, parent in (
        ("RG Progress", "topic", "RG Topic"), ("RG Appointment", "topic", "RG Topic"),
        ("RG Discussion Record", "appointment", "RG Appointment"),
        ("RG Discussion Record", "topic", "RG Topic"),
        ("RG Meeting Report", "meeting", "RG Group Meeting"),
        ("RG Material Version", "report", "RG Meeting Report"),
    ):
        require(all(row.get(field) in names[parent] for row in rows[child]), "business_parent_link_valid")
        links[child + "." + field] = len(rows[child])
    files = []
    for row in frappe.get_all("File", filters={"attached_to_doctype": ["in", list(DOCTYPES)]},
                              fields=["name"], order_by="name", limit_page_length=0):
        file = frappe.get_doc("File", row.name)
        require(file.attached_to_name in names[file.attached_to_doctype], "file_parent_exists")
        require(bool(file.is_private), "managed_file_stays_private")
        data = read_stored_file_bytes(file)
        files.append({"name": file.name, "file_name": file.file_name, "file_url": file.file_url,
                      "parent_doctype": file.attached_to_doctype, "parent_name": file.attached_to_name,
                      "is_private": bool(file.is_private), "size_bytes": len(data), "sha256": digest(data)})
    file_names = {file["name"] for file in files}
    require(all(row.material_file in file_names for row in rows["RG Material Version"]), "version_file_link_valid")
    links["RG Material Version.material_file"] = len(rows["RG Material Version"])
    links["managed_file.parent"] = len(files)
    integrity = material_integrity()
    require(integrity["ok"], "material_integrity_ok")
    return {"site": site, "tables": {dt: {"count": len(values), "sha256": digest(values)}
                                      for dt, values in rows.items()},
            "files": files, "links": links, "material_integrity": integrity}


def backup(payload):
    import frappe
    from frappe.utils.backups import BackupGenerator
    from frappe.utils.password import get_encryption_key
    site = payload["source_site"]
    before = snapshot(site)
    require(before["files"] and before["tables"]["RG Material Version"]["count"], "nonempty_material_fixture_required")
    require(not frappe.get_system_settings("encrypt_backup"), "local_acceptance_requires_unencrypted_native_backup")
    config_path = SITES / site / "site_config.json"
    original_config = json.loads(config_path.read_text())
    previous_key = frappe.conf.get("encryption_key")
    encryption_key = get_encryption_key()
    require(bool(encryption_key), "native_encryption_key_available")
    require(previous_key is None or encryption_key == previous_key, "existing_encryption_key_never_replaced")
    keyed_config = json.loads(config_path.read_text())
    require({key: value for key, value in original_config.items() if key != "encryption_key"}
            == {key: value for key, value in keyed_config.items() if key != "encryption_key"},
            "native_key_generation_changes_no_other_source_configuration")
    group = SITES / site / "private" / "backups" / ("acceptance-" + payload["run_id"])
    require(not group.exists(), "backup_group_must_be_new")
    group.mkdir(mode=0o700)
    paths = {"database": group / "database.sql.gz", "public_files": group / "files.tgz",
             "private_files": group / "private-files.tgz", "site_config": group / "site-config.json"}
    generator = BackupGenerator(
        frappe.conf.db_name, frappe.conf.db_user, frappe.conf.db_password,
        db_socket=frappe.conf.db_socket, db_host=frappe.conf.db_host,
        db_port=frappe.conf.db_port, db_type=frappe.conf.db_type,
        backup_path_db=str(paths["database"]), backup_path_files=str(paths["public_files"]),
        backup_path_private_files=str(paths["private_files"]), backup_path_conf=str(paths["site_config"]),
        compress_files=True, verbose=False,
    )
    generator.get_backup(ignore_files=False, force=True)
    for path in paths.values():
        require(path.is_file() and path.stat().st_size > 0, "all_four_backup_components_exist")
        path.chmod(0o600)
    with gzip.open(paths["database"], "rb") as sql:
        expected = {b"__Auth", *(dt.encode() for dt in DOCTYPES)}
        for line in sql:
            expected = {name for name in expected if name not in line}
        require(not expected, "database_dump_includes_auth_and_business_tables")
    with tarfile.open(paths["private_files"], "r:gz") as archive:
        members = {member.name for member in archive.getmembers() if member.isfile()}
    require(all(any(member.endswith(file["file_url"].lstrip("/")) for member in members)
                for file in before["files"]), "private_archive_contains_all_managed_files")
    after = snapshot(site)
    require(before == after, "source_stable_during_backup")
    return {"source_snapshot": before, "source_stable_during_backup": True,
            "source_encryption_key_generated_if_missing": previous_key is None,
            "existing_encryption_key_replaced": False,
            "components": {key: {"path": str(path), "size_bytes": path.stat().st_size,
                                   "sha256": digest(path.read_bytes())} for key, path in paths.items()}}


def restore(payload):
    import frappe
    from frappe.commands.site import _restore
    from frappe.installer import _new_site
    source, target = payload["source_site"], payload["restore_site"]
    require(source != target and re.fullmatch(r"restore(?:-[a-z0-9]+)*\.localhost", target),
            "independent_restore_site_required")
    require(not (SITES / target).exists(), "refuse_existing_restore_site")
    source_config = json.loads((SITES / source / "site_config.json").read_text())
    components = payload["components"]
    for component in components.values():
        path = Path(component["path"])
        require(path.is_relative_to(SITES / source / "private" / "backups"), "backup_path_inside_source_backups")
        require(path.is_file() and digest(path.read_bytes()) == component["sha256"], "backup_hash_before_restore")
    backed_config = json.loads(Path(components["site_config"]["path"]).read_text())
    require(bool(backed_config.get("encryption_key")), "backed_up_encryption_key_required")
    require(backed_config["encryption_key"] == source_config.get("encryption_key"), "backup_key_matches_source")
    db_name = "rg_restore_" + secrets.token_hex(6)
    require(db_name != source_config["db_name"], "restore_database_is_distinct")
    frappe.init(target, new_site=True)
    _new_site(db_name, target, db_root_username="root", db_root_password=payload["root_password"],
              admin_password=secrets.token_urlsafe(32), db_password=secrets.token_urlsafe(32),
              db_type="mariadb", db_host=frappe.conf.db_host or "db", db_port=frappe.conf.db_port or 3306,
              db_user=db_name, install_apps=["research_group"], mariadb_user_host_login_scope="%", verbose=False)
    config_path = SITES / target / "site_config.json"
    new_config = json.loads(config_path.read_text())
    connection = {key: value for key, value in new_config.items() if key.startswith("db_")}
    require(connection["db_name"] != source_config["db_name"], "fresh_restore_database_distinct")
    frappe.destroy()
    frappe.init(target)
    _restore(site=target, sql_file_path=components["database"]["path"],
             db_root_username="root", db_root_password=payload["root_password"],
             with_public_files=components["public_files"]["path"],
             with_private_files=components["private_files"]["path"], verbose=False, force=False)
    restored_config = json.loads(config_path.read_text())
    require({key: value for key, value in restored_config.items() if key.startswith("db_")} == connection,
            "native_restore_preserves_new_database_fields")
    for key in ("encryption_key", "rg_max_file_size", "max_file_size", "mute_emails", "rg_demo", "developer_mode"):
        if key in backed_config:
            restored_config[key] = backed_config[key]
    restored_config["mute_emails"] = 1
    config_path.write_text(json.dumps(restored_config, sort_keys=True, indent=2) + "\n")
    config_path.chmod(0o600)
    require({key: value for key, value in restored_config.items() if key.startswith("db_")} == connection,
            "configuration_merge_preserves_new_database_fields")
    frappe.destroy()
    for command in (("migrate",), ("disable-scheduler",)):
        subprocess.run(["bench", "--site", target, *command], cwd=BENCH, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    final = connect(target)
    require({key: value for key, value in final.items() if key.startswith("db_")} == connection,
            "migration_preserves_new_database_fields")
    require(final["encryption_key"] == backed_config["encryption_key"], "original_encryption_key_preserved")
    require(json.loads((SITES / source / "site_config.json").read_text()) == source_config,
            "source_site_config_unchanged")
    return {"restore_snapshot": snapshot(target), "database_distinct_from_source": True,
            "new_database_fields_preserved": True, "original_encryption_key_preserved": True,
            "source_site_config_unchanged": True}


def login_times(site):
    import frappe
    connect(site)
    return {key: str(frappe.db.get_value("User", email, "last_login")) for key, email in ACCOUNTS.items()}


class HttpClient:
    def __init__(self, site):
        self.site = site
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, path, data=None):
        raw = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request("http://127.0.0.1:8000" + path, data=raw,
                    headers={"X-Frappe-Site-Name": self.site, "Content-Type": "application/json"})
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        return response.code, response.read()


def permission_proof(payload):
    import frappe
    source, target = payload["source_site"], payload["restore_site"]
    source_before, target_before = login_times(source), login_times(target)
    private_file = None
    for name in frappe.get_all("File", filters={"attached_to_doctype": "RG Appointment"}, pluck="name"):
        candidate = frappe.get_doc("File", name)
        parent = frappe.get_doc("RG Appointment", candidate.attached_to_name)
        if parent.student == ACCOUNTS["one"] and parent.teacher == ACCOUNTS["teacher"]:
            private_file, private_parent = candidate, parent
            break
    require(private_file is not None, "private_permission_fixture_required")
    public_file = None
    for name in frappe.get_all("RG Material Version", pluck="name"):
        version = frappe.get_doc("RG Material Version", name)
        report = frappe.get_doc("RG Meeting Report", version.report)
        if frappe.db.get_value("RG Group Meeting", report.meeting, "status") == "已发布":
            public_file = frappe.get_doc("File", version.material_file)
            break
    require(public_file is not None, "published_material_fixture_required")
    from research_group.files import read_stored_file_bytes
    private_hash, public_hash = digest(read_stored_file_bytes(private_file)), digest(read_stored_file_bytes(public_file))
    checks = []
    for role, email in ACCOUNTS.items():
        client = HttpClient(target)
        status, body = client.request("/api/method/login", {"usr": email, "pwd": payload["passwords"][email]})
        require(status == 200 and json.loads(body).get("message") == "Logged In", "restored_http_login")
        expected = 200 if role in {"teacher", "one"} else 403
        path = "/api/resource/RG%20Appointment/" + urllib.parse.quote(private_parent.name, safe="")
        status, body = client.request(path)
        require(status == expected, "restored_private_record_permission")
        checks.append({"role": role, "check": "private_record", "status": status, "passed": True})
        status, body = client.request(private_file.unique_url)
        require(status == expected and (digest(body) == private_hash if expected == 200 else b"%PDF-" not in body),
                "restored_private_file_permission_and_bytes")
        checks.append({"role": role, "check": "private_file", "status": status, "passed": True})
        status, body = client.request(public_file.unique_url)
        require(status == 200 and digest(body) == public_hash, "restored_published_material_read_and_bytes")
        checks.append({"role": role, "check": "published_material", "status": status, "passed": True})
    status, body = HttpClient(target).request(private_file.unique_url)
    require(status == 403 and b"%PDF-" not in body, "restored_guest_private_file_denied")
    checks.append({"role": "guest", "check": "private_file", "status": status, "passed": True})
    target_after, source_after = login_times(target), login_times(source)
    require(all(target_after[role] != target_before[role] for role in ACCOUNTS), "http_logins_updated_restore_database")
    require(source_before == source_after, "http_restore_proof_did_not_login_to_source_database")
    return {"site": target, "checks": checks, "http_logins_updated_restore_database": True,
            "source_login_timestamps_unchanged": True}


def main():
    os.chdir(SITES)
    payload = json.load(sys.stdin)
    action = sys.argv[1]
    if action == "backup":
        return backup(payload)
    if action == "restore":
        return restore(payload)
    if action == "snapshot":
        return snapshot(payload["site"])
    if action == "permissions":
        return permission_proof(payload)
    raise AssertionError("unknown_worker_action")


if __name__ == "__main__":
    result, code = None, 0
    # Third-party setup/restore output may contain credentials in errors. Emit only
    # our safe result, exception class and fixed assertion labels; never raw logs.
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = {"ok": True, "result": main()}
    except BaseException as error:
        result = {"ok": False, "error_type": type(error).__name__}
        if isinstance(error, AssertionError):
            result["check"] = str(error)
        result["error_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno,
                                   "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]
        code = 1
    finally:
        try:
            import frappe
            frappe.destroy()
        except Exception:
            pass
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(code)
