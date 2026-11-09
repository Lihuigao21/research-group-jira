"""Verify the exact HTTP Host fix preserves the existing Socket.IO template."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "tasks/20261106-frappe-v1/work/frappe_docker/resources/core/nginx/nginx-template.conf"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


origin = load("rg_ai_origin", ROOT / "deploy/patch-nginx-origin.py")
host = load("rg_ai_host", ROOT / "deploy/patch-nginx-host.py")


class TestProxy(unittest.TestCase):
    def test_only_backend_authority_changes(self):
        before = origin.patch_template(TEMPLATE.read_bytes().decode("utf-8"))
        after = host.patch_template(before)
        self.assertEqual(after.replace(host.NEW, host.OLD), before)
        socket = lambda value: value.split("\tlocation /socket.io {", 1)[1].split("\n\t}", 1)[0]
        self.assertEqual(socket(after), socket(before))
        self.assertEqual(after.count(origin.ORIGIN_MAP), 1)
        self.assertIn("proxy_set_header Host $host;", socket(after))
        self.assertIn("proxy_set_header Origin $rg_socket_origin;", socket(after))
        self.assertEqual(after.count(host.NEW), 1)

    def test_drift_and_repeat_fail_before_writing(self):
        before = origin.patch_template(TEMPLATE.read_bytes().decode("utf-8"))
        scratch = ROOT / "tasks/20261107-ai-assistant/work"
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            target = Path(directory) / "frappe.conf.template"
            for value in (before.replace("@webserver {", "@unexpected {"), before + before, host.patch_template(before)):
                raw = value.encode("utf-8")
                target.write_bytes(raw)
                with self.assertRaises(AssertionError):
                    host.main(target)
                self.assertEqual(target.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
