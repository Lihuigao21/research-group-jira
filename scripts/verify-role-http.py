"""Run role collaboration acceptance on an isolated, capture-only loopback site."""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import secrets
import sys

SOURCE = Path(__file__).resolve().parent / "role-collaboration"
sys.path.insert(0, str(SOURCE))
import acceptance_config as cfg

SUITES = ("fixtures", "accounts", "portals", "notifications", "meals", "manuscripts", "realtime",
          "native-envelope", "legacy-core", "legacy-surveys", "legacy-ai")
CURRENT_SUITES = SUITES[:7]


def load(name):
    path = SOURCE / name
    spec = importlib.util.spec_from_file_location("role_acceptance_" + name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute(suite):
    ctx = cfg.current()
    import role_http as h
    h.RESULTS = []
    if suite == "fixtures":
        request = {"site": ctx.site, "base": ctx.base, "expected_db_name": ctx.expected_db_name}
        source = (SOURCE / "prepare-fixtures.py").read_bytes().decode("utf-8")
        value = cfg.private_python(source + "\nprint(json.dumps(prepare(" + repr(request) + ")))\n")
        if not value.get("ok"):
            raise RuntimeError("Fictional fixture preparation failed")
        cfg.save_json(ctx.receipt("fixtures-http-" + ctx.attempt + ".json"), value)
        return True
    if suite.startswith("legacy-"):
        return load("run-legacy-http.py").main(suite.removeprefix("legacy-")) == 0
    module = load("verify-" + suite + "-http.py")
    argv = [str(SOURCE / ("verify-" + suite + "-http.py"))]
    if suite == "portals":
        argv += ["--base", ctx.base, "--output", str(ctx.receipt("portals-http-" + ctx.attempt + ".json"))]
    elif suite == "notifications":
        argv += ["--base", ctx.base, "--receipt", "notifications-http-" + ctx.attempt + ".json"]
    elif suite == "manuscripts":
        argv += ["--base", ctx.base, "--credentials", str(ctx.credentials_path), "--attempt", ctx.attempt]
    previous = sys.argv
    sys.argv = argv
    try:
        return module.main() == 0
    finally:
        sys.argv = previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", nargs="?", default="all", choices=("all", *SUITES),
                        help="all runs the seven current suites in order; native-envelope and legacy suites are explicit")
    parser.add_argument("--base", default="http://localhost:8089")
    parser.add_argument("--credentials", type=Path, required=True, help="private JSON; never printed")
    parser.add_argument("--container", required=True, help="separate candidate backend container")
    parser.add_argument("--site", default="lab.localhost")
    parser.add_argument("--expected-db-name", help="exact generated database name for an independently verified candidate")
    parser.add_argument("--task-dir", type=Path, required=True, help="current project tasks/<task> output root")
    parser.add_argument("--attempt", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4))
    parser.add_argument("--check-paths", action="store_true", help="validate CLI/private input/output paths only; no runtime calls or writes")
    args = parser.parse_args()
    try:
        ctx = cfg.configure(args)
        cfg.credentials(required=(*cfg.DEMO_USERS, "Administrator") if args.suite == "legacy-core" else cfg.DEMO_USERS)
    except (ValueError, OSError, KeyError) as error:
        # Only configuration errors constructed by our validators are shown.
        # JSON parser messages/paths and OS exception text may be sensitive.
        message = str(error) if type(error) is ValueError and not isinstance(error, json.JSONDecodeError) else "Invalid private input or output path"
        parser.error(message)
    suites = list(CURRENT_SUITES if args.suite == "all" else (args.suite,))
    if args.check_paths:
        print(json.dumps({"ok": True, "runtime_invoked": False, "writes": False, "base": ctx.base,
                          "suites": suites, "attempt": ctx.attempt, "records": str(ctx.records),
                          "scratch": str(ctx.scratch), "credentials_checked": True}, ensure_ascii=False))
        return 0
    ctx.begin()
    summary = {"ok": False, "attempt": ctx.attempt, "base": ctx.base, "fictional_only": True,
               "started_at": datetime.now(timezone.utc).isoformat(), "suites": [],
               "sources": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted(SOURCE.glob("*.py"))}, "paid_api_called": False}
    try:
        summary["preflight"] = cfg.preflight()
        for suite in suites:
            # Suite output is captured privately. The public runner emits only
            # the final safe summary path and suite result booleans.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                ok = execute(suite)
            summary["suites"].append({"suite": suite, "ok": ok})
            if not ok:
                break
        summary["ok"] = len(summary["suites"]) == len(suites) and all(row["ok"] for row in summary["suites"])
    except BaseException as error:
        summary["error_type"] = type(error).__name__
    finally:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        output = cfg.save_json(ctx.receipt("summary.json"), summary)
    print(json.dumps({"ok": summary["ok"], "suites": summary["suites"], "receipt": str(output)}, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
