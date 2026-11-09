"""Check credential races and build-context admission without running Docker.

Run on Windows and Linux to exercise each native build launcher. Test copies
replace only the frozen revision with an isolated local repository's revision;
Docker is a local stub that records when admission reaches the build step.
"""
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks" / "20261106-frappe-v1"
PIN = "74421fd01047290635742d3fb85f7fd7eebbde86"


def run(command, cwd, **kwargs):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30, **kwargs)


def check_exclusive_creation(scratch):
    source = ROOT / "scripts" / "create-local-config.py"
    spec = importlib.util.spec_from_file_location("rg_private_config_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = scratch / "private-config"
    barrier = threading.Barrier(16)

    def contender(number):
        payload = f"fictional-test-payload-{number}\n".encode()
        barrier.wait(timeout=10)
        return payload, module.write_new_private_file(destination, payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(contender, range(16)))
    winners = [payload for payload, created in results if created]
    assert len(winners) == 1 and destination.read_bytes() == winners[0]
    assert module.write_new_private_file(destination, b"must-not-replace") is False
    assert destination.read_bytes() == winners[0]

    real_fdopen = module.os.fdopen

    class FailedWrite:
        def __init__(self, descriptor, mode, failure, replace=None):
            self.destination = real_fdopen(descriptor, mode)
            self.failure = failure
            self.replace = replace

        def __enter__(self):
            return self

        def __exit__(self, *exception):
            return self.destination.__exit__(*exception)

        def write(self, content):
            self.destination.write(content[:3])
            self.destination.flush()
            if self.replace:
                self.destination.close()
                self.replace()
            raise self.failure

    for stage in ("write", "fsync"):
        partial = scratch / ("partial-" + stage)
        failure = OSError("injected " + stage + " failure")
        if stage == "write":
            patcher = mock.patch.object(module.os, "fdopen", side_effect=lambda fd, mode:
                                       FailedWrite(fd, mode, failure))
        else:
            patcher = mock.patch.object(module.os, "fsync", side_effect=failure)
        with patcher:
            try:
                module.write_new_private_file(partial, b"incomplete-credential\n")
            except OSError as caught:
                assert caught is failure
            else:
                raise AssertionError(stage + " failure was not propagated")
        assert not partial.exists(), stage + " left its partial credential"
        assert module.write_new_private_file(partial, b"complete-retry\n") is True
        assert partial.read_bytes() == b"complete-retry\n"

    replaced = scratch / "replaced-credential"
    replacement = scratch / "concurrent-credential"
    replacement.write_bytes(b"concurrent-creator-must-survive\n")
    failure = OSError("injected write failure after concurrent replacement")
    with mock.patch.object(module.os, "fdopen", side_effect=lambda fd, mode:
                           FailedWrite(fd, mode, failure,
                                       lambda: os.replace(replacement, replaced))):
        try:
            module.write_new_private_file(replaced, b"incomplete-credential\n")
        except OSError as caught:
            assert caught is failure
        else:
            raise AssertionError("replacement failure was not propagated")
    assert replaced.read_bytes() == b"concurrent-creator-must-survive\n"
    with mock.patch.object(module.os, "fdopen", side_effect=AssertionError("existing file opened")), \
            mock.patch.object(module.os, "fsync", side_effect=AssertionError("existing file synced")):
        assert module.write_new_private_file(replaced, b"must-not-replace") is False
    assert replaced.read_bytes() == b"concurrent-creator-must-survive\n"

    return {"exclusive_creation": True, "concurrent_contenders": 16,
            "existing_bytes_preserved": True,
            "failed_write_and_fsync_reclaimed": True,
            "retry_after_failed_creation": True,
            "concurrent_replacement_preserved": True,
            "observed_file_mode": oct(destination.stat().st_mode & 0o777)}


def check_context_admission(scratch):
    project = scratch / "project"
    base = project / "deploy" / "base"
    context = project / "runtime" / "base-context"
    base.mkdir(parents=True)
    context.mkdir(parents=True)
    marker = scratch / "docker-stub-reached"
    git = shutil.which("git")
    assert git, "git is required"
    assert run([git, "init", "--quiet"], context).returncode == 0
    resource = context / "resource.txt"
    resource.write_bytes(b"frozen-resource\n")
    (context / ".gitignore").write_bytes(b"ignored.bin\n")
    assert run([git, "add", "."], context).returncode == 0
    assert run([git, "-c", "user.name=RG test", "-c", "user.email=test@example.invalid",
                "commit", "--quiet", "-m", "isolated build guard fixture"], context).returncode == 0
    revision = run([git, "rev-parse", "HEAD"], context).stdout.strip()
    extension = "ps1" if os.name == "nt" else "sh"
    source = ROOT / "deploy" / "base" / ("build." + extension)
    source_bytes = source.read_bytes()
    text = source_bytes.decode("utf-8")
    assert text.count(PIN) == 1
    launcher = base / source.name
    launcher.write_bytes(text.replace(PIN, revision).encode("utf-8"))
    environment = os.environ.copy()
    environment["RG_TEST_DOCKER_MARKER"] = str(marker)
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        assert shell, "PowerShell is required"
        wrapper = scratch / "run-build.ps1"
        script_path = str(launcher).replace("'", "''")
        wrapper.write_bytes(("function docker {\n"
                             "    [IO.File]::WriteAllText($env:RG_TEST_DOCKER_MARKER, 'stub')\n"
                             "    $global:LASTEXITCODE = 0\n"
                             "}\n"
                             f"& '{script_path}'\n").encode("utf-8"))
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]
    else:
        binary = scratch / "bin"
        binary.mkdir()
        stub = binary / "docker"
        stub.write_bytes(b'#!/bin/sh\nprintf stub > "$RG_TEST_DOCKER_MARKER"\n')
        stub.chmod(0o755)
        environment["PATH"] = str(binary) + os.pathsep + environment["PATH"]
        command = ["bash", str(launcher)]

    clean = run(command, project, env=environment)
    assert clean.returncode == 0 and marker.exists(), ("clean context rejected", clean.stderr[-1000:])
    marker.unlink()
    cases = (("tracked", resource, b"changed-resource\n"),
             ("untracked", context / "extra.txt", b"extra\n"),
             ("ignored", context / "ignored.bin", b"extra\n"))
    for name, path, payload in cases:
        path.write_bytes(payload)
        result = run(command, project, env=environment)
        assert result.returncode != 0 and not marker.exists(), (name, result.stderr[-1000:])
        if path == resource:
            path.write_bytes(b"frozen-resource\n")
        else:
            path.unlink()
    return {"launcher": str(source.relative_to(ROOT)),
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "clean_context_reaches_stub": True,
            "tracked_untracked_ignored_changes_rejected": True,
            "actual_docker_called": False}


def main():
    scratch_parent = TASK / "work" / "scratch"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="deploy-guards-", dir=scratch_parent))
    receipt = {"platform": os.name, "scratch": str(scratch),
               "credentials": check_exclusive_creation(scratch),
               "build": check_context_admission(scratch)}
    records = TASK / "records"
    records.mkdir(parents=True, exist_ok=True)
    destination = records / ("deploy-guards-" + os.name + ".json")
    destination.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
