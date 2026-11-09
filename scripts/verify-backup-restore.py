"""Run an explicitly authorized local backup, independent restore and restart drill.

Without --execute --allow-restart this only prints the plan. Never logs secrets.
"""
import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("db", "redis-cache", "redis-queue", "backend", "websocket", "worker", "frontend")
COMPOSE = ["docker", "compose", "--env-file", str(ROOT / "deploy/.env"),
           "-f", str(ROOT / "deploy/compose.yaml")]
PYTHON = "/home/frappe/frappe-bench/env/bin/python"
WORKER = "/opt/research-group-scripts/backup-restore-worker.py"


class WorkerFailure(AssertionError):
    def __init__(self, action, result):
        super().__init__("worker_" + action + "_" + result.get("check", result.get("error_type", "failed")))
        self.safe_result = result


def require(condition, label):
    if not condition:
        raise AssertionError(label)


def command(arguments, input_data=None, timeout=180):
    process = subprocess.run(arguments, input=input_data, text=True, capture_output=True,
                             encoding="utf-8", cwd=ROOT, timeout=timeout)
    require(process.returncode == 0, "command_failed_see_phase_and_exit_code")
    return process.stdout


def worker(action, payload):
    process = subprocess.run([*COMPOSE, "exec", "-T", "backend", PYTHON, WORKER, action],
                             input=json.dumps(payload), text=True, encoding="utf-8", capture_output=True,
                             cwd=ROOT, timeout=240)
    require(bool(process.stdout.strip()), "worker_" + action + "_no_safe_result")
    result = json.loads(process.stdout.strip().splitlines()[-1])
    if process.returncode != 0 or not result.get("ok"):
        raise WorkerFailure(action, result)
    return result["result"]


def save(folder, name, value):
    (folder / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def equivalent(left, right):
    return all(left[key] == right[key] for key in ("tables", "files", "links", "material_integrity"))


def states():
    output = command([*COMPOSE, "ps", "--format", "json"]).strip()
    rows = json.loads(output) if output.startswith("[") else [json.loads(line) for line in output.splitlines() if line.strip()]
    mapping = {row["Service"]: row["ID"] for row in rows if row["Service"] in SERVICES}
    require(set(mapping) == set(SERVICES), "all_seven_services_present")
    output = command(["docker", "inspect", "--format", "{{json .State}}", *mapping.values()])
    result = {}
    for (service, identifier), line in zip(mapping.items(), output.splitlines()):
        state = json.loads(line)
        result[service] = {"id": identifier, "started_at": state["StartedAt"], "status": state["Status"],
                           "health": state.get("Health", {}).get("Status", "not_configured")}
    return result


def wait_ready():
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = states()
        if all(row["status"] == "running" and row["health"] not in {"starting", "unhealthy"}
               for row in current.values()):
            return current
        time.sleep(2)
    raise AssertionError("services_not_ready_within_120_seconds")


def env_values():
    values = {}
    for line in (ROOT / "deploy/.env").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"").strip("'")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-restart", action="store_true")
    parser.add_argument("--source-site", default="lab.localhost")
    parser.add_argument("--restore-site", default="restore.localhost")
    options = parser.parse_args()
    require(options.source_site != options.restore_site
            and re.fullmatch(r"restore(?:-[a-z0-9]+)*\.localhost", options.restore_site),
            "restore_site_must_be_independent_restore_localhost_name")
    if not (options.execute and options.allow_restart):
        print(f"Plan only: native DB+public/private files+config backup; fresh {options.restore_site}/database; "
              "compare business/file/link hashes; real restored-site HTTP permissions; restart seven "
              "project services; verify both sites and Redis AOF. Run only after explicit authorization "
              "using --execute --allow-restart.")
        return 0
    require(not (ROOT / "runtime/sites" / options.restore_site).exists(), "refuse_existing_restore_site")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    folder = ROOT / "tasks/20261106-frappe-v1/records" / ("backup-restore-" + run_id)
    folder.mkdir(parents=True)
    phase = "preflight"
    report = {"run_id": run_id, "source_site": options.source_site, "restore_site": options.restore_site,
              "authorized_execution": True, "completed": False, "checks": []}
    probe_key = "rg:backup-acceptance:" + run_id
    probe_value = secrets.token_hex(24)
    common = {"source_site": options.source_site, "restore_site": options.restore_site, "run_id": run_id}
    try:
        credentials = json.loads((ROOT / "runtime/secrets/demo-credentials.json").read_text(encoding="utf-8"))
        private_env = env_values()
        require(bool(private_env.get("DB_ROOT_PASSWORD")), "root_password_available_without_logging")
        save(folder, "services-before", wait_ready())
        phase = "backup"
        backup_result = worker("backup", common)
        save(folder, "backup", backup_result)
        report["checks"].append("native_database_public_private_config_backup_verified")
        phase = "independent_restore"
        restored = worker("restore", {**common, "components": backup_result["components"],
                                       "root_password": private_env["DB_ROOT_PASSWORD"]})
        save(folder, "restore", restored)
        require(equivalent(backup_result["source_snapshot"], restored["restore_snapshot"]), "restored_business_files_links_equal_source")
        report["checks"].append("independent_restore_counts_hashes_links_and_database_fields_match")
        phase = "restored_http_permissions"
        save(folder, "permissions-before-restart", worker("permissions", {**common, "passwords": credentials}))
        report["checks"].append("restored_site_actual_http_login_private_and_public_permissions_pass")
        phase = "redis_aof_probe"
        appendonly = command([*COMPOSE, "exec", "-T", "redis-queue", "redis-cli", "--raw", "CONFIG", "GET", "appendonly"])
        require(appendonly.strip().splitlines() == ["appendonly", "yes"], "queue_aof_enabled")
        require(command([*COMPOSE, "exec", "-T", "redis-queue", "redis-cli", "-x", "SET", probe_key], probe_value).strip() == "OK", "queue_probe_written")
        fsynced = command([*COMPOSE, "exec", "-T", "redis-queue", "redis-cli", "--raw", "WAITAOF", "1", "0", "5000"])
        require(fsynced.strip().splitlines()[0] == "1", "queue_probe_aof_fsynced")
        before_restart = states()
        save(folder, "restart-before", before_restart)
        phase = "restart"
        command([*COMPOSE, "restart", "db", "redis-cache", "redis-queue"], timeout=120)
        wait_ready()
        command([*COMPOSE, "restart", "backend", "websocket", "worker", "frontend"], timeout=120)
        after_restart = wait_ready()
        save(folder, "restart-after", after_restart)
        require(all(before_restart[service]["started_at"] != after_restart[service]["started_at"]
                    for service in SERVICES), "all_seven_services_restarted")
        require(command([*COMPOSE, "exec", "-T", "redis-queue", "redis-cli", "--raw", "GET", probe_key]).strip() == probe_value,
                "queue_probe_survived_aof_restart")
        require(command([*COMPOSE, "exec", "-T", "redis-cache", "redis-cli", "ping"]).strip() == "PONG",
                "redis_cache_responds_after_restart")
        report["checks"].append("seven_services_restarted_and_queue_aof_value_survived")
        phase = "post_restart_snapshots"
        for site, expected, label in ((options.source_site, backup_result["source_snapshot"], "source"),
                                      (options.restore_site, restored["restore_snapshot"], "restore")):
            current = worker("snapshot", {"site": site})
            save(folder, label + "-after-restart", current)
            require(equivalent(expected, current), label + "_records_bytes_links_persisted")
        phase = "post_restart_http_permissions"
        # Last-login has second resolution; avoid equality with the first proof.
        time.sleep(2)
        save(folder, "permissions-after-restart", worker("permissions", {**common, "passwords": credentials}))
        report["checks"].append("both_sites_persisted_and_restored_http_permissions_rechecked")
        report["completed"] = True
        report["queue_probe_value_sha256"] = hashlib.sha256(probe_value.encode()).hexdigest()
    except Exception as error:
        report["failed_phase"] = phase
        report["error_type"] = type(error).__name__
        if isinstance(error, AssertionError):
            report["failed_check"] = str(error)
        if isinstance(error, WorkerFailure):
            save(folder, "worker-failure", error.safe_result)
    finally:
        try:
            require(probe_key.startswith("rg:backup-acceptance:"), "exact_probe_cleanup_only")
            command([*COMPOSE, "exec", "-T", "redis-queue", "redis-cli", "DEL", probe_key], timeout=15)
            report["queue_probe_cleanup"] = True
        except Exception:
            report["queue_probe_cleanup"] = False
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        save(folder, "result", report)
        print(json.dumps({"completed": report["completed"], "record_directory": str(folder),
                          "failed_phase": report.get("failed_phase"), "failed_check": report.get("failed_check")}, ensure_ascii=False))
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    sys.exit(main())
