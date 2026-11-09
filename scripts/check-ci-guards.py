"""Exercise the real CI launcher with fake installers and no database/network."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "ci-frappe-tests.sh"
FAKE_PROGRAM = """#!/usr/bin/env bash
set -euo pipefail
printf '%s' "${0##*/}" >> "$RG_CI_TEST_CALLS"
printf '\\t%s' "$@" >> "$RG_CI_TEST_CALLS"
printf '\\n' >> "$RG_CI_TEST_CALLS"
if [[ "${1:-}" == -m && "${2:-}" == venv ]]; then
  mkdir -p -- "$3/bin"
  cp -- "$0" "$3/bin/python"
  cp -- "$0" "$3/bin/pip"
fi
"""


def snapshot(folder):
    """Only traverse the small fixture, retaining files and symlink targets."""
    result = {}
    for path in [folder, *sorted(folder.rglob("*"))]:
        key = path.relative_to(folder).as_posix()
        if path.is_symlink():
            result[key] = ["link", os.readlink(path)]
        elif path.is_dir():
            result[key] = ["directory"]
        else:
            result[key] = ["file", hashlib.sha256(path.read_bytes()).hexdigest()]
    return result


def main():
    bash = os.environ.get("RG_CI_TEST_BASH") or shutil.which("bash")
    if not bash:
        raise SystemExit("Bash is required to test the actual CI launcher.")
    parent = Path(os.environ.get("RG_CI_GUARD_WORKDIR") or os.environ.get("RUNNER_TEMP")
                  or ROOT / "runtime" / "ci-guard-check" / "scratch").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    checks, skipped = [], []
    with tempfile.TemporaryDirectory(prefix="rg-ci-guards-", dir=parent) as temporary:
        folder = Path(temporary)
        programs = folder / "programs"
        programs.mkdir()
        for name in ("git", "python-fixture"):
            program = programs / name
            program.write_bytes(FAKE_PROGRAM.encode("utf-8"))
            program.chmod(0o755)

        def launch(case, bench, site="ci.localhost", cwd=ROOT):
            calls = folder / f"{case}.calls"
            env = dict(os.environ, PATH=str(programs) + os.pathsep + os.environ["PATH"],
                       BENCH_DIR=bench.as_posix(), SITE=site,
                       PYTHON=(programs / "python-fixture").as_posix(),
                       DB_ROOT_PASSWORD="ci-guard-test-only", RG_CI_TEST_CALLS=calls.as_posix())
            result = subprocess.run([bash, LAUNCHER.as_posix()], cwd=cwd, env=env,
                                    capture_output=True, text=True, encoding="utf-8", timeout=20)
            return result, calls

        existing = folder / "existing-bench"
        (existing / "sites" / "ci.localhost").mkdir(parents=True)
        (existing / "apps" / "research_group").mkdir(parents=True)
        (existing / "sites" / "ci.localhost" / "site_config.json").write_bytes(b'{"KEEP":"site"}\n')
        (existing / "sites" / "common_site_config.json").write_bytes(b'{"KEEP":"bench"}\n')
        (existing / "apps" / "research_group" / "KEEP").write_bytes(b"original application\x00")
        before = snapshot(existing)
        result, calls = launch("existing", existing)
        assert result.returncode != 0 and "Refusing existing BENCH_DIR" in result.stderr, result
        assert snapshot(existing) == before, "Existing bench files changed"
        assert not calls.exists(), "An installer ran for an existing bench"
        checks.append("existing bench preserved byte-for-byte; no installer called")

        existing_file = folder / "existing-file"
        existing_file.write_bytes(b"KEEP existing file\n")
        result, calls = launch("file", existing_file)
        assert result.returncode != 0 and "Refusing existing BENCH_DIR" in result.stderr, result
        assert existing_file.read_bytes() == b"KEEP existing file\n" and not calls.exists()
        checks.append("existing file rejected unchanged; no installer called")

        for name, target in (("existing-link", existing), ("broken-link", folder / "absent-target")):
            link = folder / name
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                skipped.append(f"{name}: symlink creation unavailable ({type(error).__name__})")
                continue
            result, calls = launch(name, link)
            assert result.returncode != 0 and "Refusing existing BENCH_DIR" in result.stderr, result
            assert link.is_symlink() and os.readlink(link) == str(target) and not calls.exists()
            assert snapshot(existing) == before
            checks.append(f"{name} rejected unchanged; no installer called")

        invalid_sites = ("production.localhost", "../ci.localhost", "/tmp/ci.localhost", "-ci.localhost",
                         "ci.localhost/other", "ci..localhost", "ci_BAD.localhost", "ci-.localhost",
                         "ci.other.localhost", "ci.localhost\n")
        for index, site in enumerate(invalid_sites):
            bench = folder / f"invalid-site-{index}" / "bench"
            result, calls = launch(f"invalid-{index}", bench, site)
            assert result.returncode != 0 and "SITE must be" in result.stderr, result
            assert not bench.parent.exists() and not calls.exists(), "Invalid SITE caused a write or install"
            checks.append(f"invalid SITE {index + 1} rejected before writes or installers")

        for index, site in enumerate(("ci.localhost", "ci-guard42.localhost")):
            bench = folder / f"fresh-bench-{index}"
            result, calls = launch(f"fresh-{index}", bench, site)
            assert result.returncode == 0, result.stderr
            commands = [line.split("\t") for line in calls.read_text(encoding="utf-8").splitlines()]
            new_site = [command for command in commands if "new-site" in command]
            assert len(new_site) == 1 and site in new_site[0] and "--force" not in new_site[0], commands
            assert any("run-tests" in command and "research_group" in command for command in commands), commands
            assert json.loads((bench / "sites" / "common_site_config.json").read_text(encoding="utf-8"))
            checks.append(f"fresh {site} reaches test command with fake installers and no --force")
        bench = folder / "fresh-relative-bench"
        result, calls = launch("fresh-relative", Path("fresh-relative-bench"), cwd=folder)
        assert result.returncode == 0, result.stderr
        commands = [line.split("\t") for line in calls.read_text(encoding="utf-8").splitlines()]
        assert any("new-site" in command and "--force" not in command for command in commands), commands
        assert any("run-tests" in command and "research_group" in command for command in commands), commands
        assert json.loads((bench / "sites" / "common_site_config.json").read_text(encoding="utf-8"))
        checks.append("relative BENCH_DIR reaches test command after the sites directory change")
    print(json.dumps({"check": "ci-launcher-guards", "ok": True, "checks": checks,
                      "count": len(checks), "skipped": skipped, "database_used": False,
                      "network_used": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
