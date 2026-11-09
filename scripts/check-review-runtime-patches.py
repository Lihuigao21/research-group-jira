"""Isolated checks for the Origin template patch and restore-site guards only."""
import argparse
import contextlib
import importlib.util
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "tasks/20261106-frappe-v1/work/scratch"
TEMPLATE = ROOT / "tasks/20261106-frappe-v1/work/frappe_docker/resources/core/nginx/nginx-template.conf"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


origin = load("rg_origin_patch", ROOT / "deploy/patch-nginx-origin.py")
verify = load("rg_restore_guard", ROOT / "scripts/verify-backup-restore.py")
worker = load("rg_restore_worker_guard", ROOT / "scripts/backup-restore-worker.py")


class TestOriginTemplatePatch(unittest.TestCase):
    def test_pinned_template_cli_and_map_preserve_explicit_origins(self):
        raw = TEMPLATE.read_bytes()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            target = Path(directory) / "frappe.conf.template"
            target.write_bytes(raw)
            process = subprocess.run([sys.executable, str(ROOT / "deploy/patch-nginx-origin.py"), str(target)],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            rendered = target.read_text(encoding="utf-8")
        self.assertEqual(rendered.count(origin.ORIGIN_MAP), 1)
        self.assertLess(rendered.index(origin.ORIGIN_MAP), rendered.index(origin.SERVER_ANCHOR))
        self.assertEqual(rendered.count("proxy_set_header Origin $rg_socket_origin;"), 1)
        self.assertNotIn(origin.UPSTREAM_ORIGIN, rendered)
        self.assertEqual(rendered.replace(origin.ORIGIN_MAP, "").replace(
            "proxy_set_header Origin $rg_socket_origin;", origin.UPSTREAM_ORIGIN), raw.decode("utf-8"))
        # The map's default keeps every explicit Origin, including a foreign
        # value, for Frappe's existing check. Only the empty key gets a fallback.
        self.assertIn("\tdefault $http_origin;", origin.ORIGIN_MAP)
        self.assertIn('\t"" "$proxy_x_forwarded_proto://$http_host";', origin.ORIGIN_MAP)
        self.assertNotIn("FRAPPE_SITE_NAME_HEADER", origin.ORIGIN_MAP)
        containerfile = (ROOT / "deploy/Containerfile").read_text(encoding="utf-8")
        self.assertIn("COPY deploy/patch-nginx-origin.py /tmp/rg-patch-nginx-origin.py", containerfile)
        self.assertIn("RUN python /tmp/rg-patch-nginx-origin.py /templates/nginx/frappe.conf.template", containerfile)

    def test_template_drift_or_repeat_patch_fails_before_writing(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        invalid = (
            text.replace(origin.UPSTREAM_ORIGIN, ""),
            text + "\n" + origin.UPSTREAM_ORIGIN,
            text.replace(origin.SERVER_ANCHOR, "server {\n\tlisten 9999;"),
            text + "\n" + origin.SERVER_ANCHOR,
            origin.patch_template(text),
        )
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            target = Path(directory) / "frappe.conf.template"
            for value in invalid:
                raw = value.encode("utf-8")
                target.write_bytes(raw)
                with self.assertRaises(AssertionError):
                    origin.main(target)
                self.assertEqual(target.read_bytes(), raw)


class GuardPassed(Exception):
    pass


class TestRestoreSiteGuards(unittest.TestCase):
    def plan(self, source, target):
        output = StringIO()
        with patch.object(sys, "argv", ["verify-backup-restore.py", "--source-site", source, "--restore-site", target]), \
                patch.object(verify, "command", side_effect=AssertionError("no Docker")), \
                patch.object(verify, "worker", side_effect=AssertionError("no restore worker")), \
                contextlib.redirect_stdout(output):
            result = verify.main()
        return result, output.getvalue()

    def test_fresh_restore_names_and_plan_use_selected_site(self):
        for target in ("restore.localhost", "restore-review1.localhost", "restore-2026-review1.localhost"):
            result, output = self.plan("lab.localhost", target)
            self.assertEqual(result, 0)
            self.assertIn("fresh " + target + "/database", output)

    def test_invalid_or_same_source_is_rejected_before_any_execution(self):
        invalid = ("lab.localhost", "restore-review_1.localhost", "restore-Review1.localhost",
                   "restore-.localhost", "restore-review1.localhost.evil", "../restore.localhost",
                   "restore-review1.localhost/", "restore.localhost\n")
        for target in invalid:
            with self.subTest(target=target), self.assertRaisesRegex(AssertionError, "independent_restore_localhost_name"):
                self.plan("lab.localhost", target)
        with self.assertRaisesRegex(AssertionError, "independent_restore_localhost_name"):
            self.plan("restore-review1.localhost", "restore-review1.localhost")

    def test_host_refuses_existing_default_and_named_restore(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory:
            root = Path(directory)
            for target in ("restore.localhost", "restore-review1.localhost"):
                (root / "runtime/sites" / target).mkdir(parents=True)
                argv = ["verify-backup-restore.py", "--execute", "--allow-restart", "--restore-site", target]
                with patch.object(verify, "ROOT", root), patch.object(sys, "argv", argv), \
                        patch.object(verify, "command", side_effect=AssertionError("no Docker")), \
                        patch.object(verify, "worker", side_effect=AssertionError("no worker")):
                    with self.assertRaisesRegex(AssertionError, "refuse_existing_restore_site"):
                        verify.main()

    def test_container_guard_matches_host_without_entering_restore(self):
        modules = {name: types.ModuleType(name) for name in
                   ("frappe", "frappe.commands", "frappe.commands.site", "frappe.installer")}
        modules["frappe.commands.site"]._restore = lambda *a, **k: self.fail("must not restore")
        modules["frappe.installer"]._new_site = lambda *a, **k: self.fail("must not create a site")
        with tempfile.TemporaryDirectory(dir=SCRATCH) as directory, \
                patch.dict(sys.modules, modules), patch.object(worker, "SITES", Path(directory)):
            for target in ("restore.localhost", "restore-review1.localhost", "restore-2026-review1.localhost"):
                with patch.object(Path, "read_text", side_effect=GuardPassed) as read:
                    with self.assertRaises(GuardPassed):
                        worker.restore({"source_site": "lab.localhost", "restore_site": target})
                    read.assert_called_once()  # Passed guards; stopped before configuration/backup reads.
            for source, target in (("restore-review1.localhost", "restore-review1.localhost"),
                                   ("lab.localhost", "../restore.localhost"),
                                   ("lab.localhost", "restore-review_1.localhost"),
                                   ("lab.localhost", "restore.localhost\n")):
                with self.assertRaisesRegex(AssertionError, "independent_restore_site_required"):
                    worker.restore({"source_site": source, "restore_site": target})
            for target in ("restore.localhost", "restore-review1.localhost"):
                (worker.SITES / target).mkdir()
                with self.assertRaisesRegex(AssertionError, "refuse_existing_restore_site"):
                    worker.restore({"source_site": "lab.localhost", "restore_site": target})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    options = parser.parse_args()
    TEMPLATE = options.template.resolve()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[sys.argv[0]], verbosity=2)
