"""Isolated checks for persisted material verification and HTTP regression helpers."""
import ast
import hashlib
import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True


class ValidationError(Exception):
    pass


def load_submit(stored):
    path = ROOT / "src/research_group/research_group/services.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "submit_material")
    report = SimpleNamespace(name="report-1", meeting="meeting-1", student="student", latest_version=1, topic="original")
    meeting = SimpleNamespace(status="published", deadline=datetime(2030, 1, 1))
    events, versions = [], []

    def get_doc(doctype, *args, **kwargs):
        if isinstance(doctype, dict):
            version = SimpleNamespace(**doctype, name="version-2")
            versions.append(version)
            return version
        return {"RG Meeting Report": report, "RG Group Meeting": meeting}[doctype]

    def throw(message):
        raise ValidationError(message)

    def read(file, max_read_bytes=None):
        events.append("read")
        if max_read_bytes is not None and len(stored) > max_read_bytes:
            throw("bounded read")
        return stored

    db = SimpleNamespace(savepoint=Mock(), rollback=Mock(),
                         set_value=Mock(side_effect=lambda *a, **k: events.append("accept")))
    file = SimpleNamespace(name="file-2", unique_url="/private/files/retained.pdf")
    namespace = {
        "hashlib": hashlib,
        "frappe": SimpleNamespace(session=SimpleNamespace(user="student"), db=db,
                                  get_doc=get_doc, throw=throw, generate_hash=lambda **k: "savepoint"),
        "STUDENT": "student-role", "PUBLISHED": "published", "require_role": Mock(),
        "secure_get": Mock(return_value=report), "validate_material": Mock(), "_lock": Mock(),
        "now_datetime": lambda: datetime(2026, 11, 6), "get_datetime": lambda value: value,
        "save_internal": Mock(), "save_managed_file": Mock(return_value=file),
        "read_stored_file_bytes": Mock(side_effect=read), "_cleanup_created_material_files": Mock(),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace, report, versions, events


class PersistedMaterialChecks(unittest.TestCase):
    def test_matching_stored_bytes_are_checked_before_acceptance(self):
        content = b"original persisted bytes"
        ns, report, versions, events = load_submit(content)
        result = ns["submit_material"]("report-1", "accepted", "original.pdf", content)
        self.assertEqual(result["version"], 2)
        self.assertEqual(events, ["read", "accept"])
        self.assertEqual(versions[0].sha256, hashlib.sha256(content).hexdigest())
        ns["read_stored_file_bytes"].assert_called_once_with(ns["save_managed_file"].return_value,
                                                          max_read_bytes=len(content))
        self.assertEqual(report.latest_version, 2)
        ns["frappe"].db.rollback.assert_not_called()

    def test_corrupt_or_wrong_size_dedup_rolls_back_before_acceptance(self):
        content = b"original persisted bytes"
        for stored in (b"X" + content[1:], content[:-1], content + b"x"):
            with self.subTest(stored=stored):
                ns, report, versions, events = load_submit(stored)
                with self.assertRaises(ValidationError):
                    ns["submit_material"]("report-1", "rejected", "again.pdf", content)
                ns["frappe"].db.rollback.assert_called_once_with(save_point="rg_material_savepoint")
                ns["_cleanup_created_material_files"].assert_called_once_with([])
                ns["frappe"].db.set_value.assert_not_called()
                self.assertEqual(report.latest_version, 1)
                self.assertEqual(report.topic, "original")
                self.assertEqual(events, ["read"])


def load_http():
    spec = importlib.util.spec_from_file_location("rg_http_boundary_checks", ROOT / "scripts/verify-http.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HTTPRegressionHelpers(unittest.TestCase):
    def setUp(self):
        self.http = load_http()

    def test_native_multipart_contains_actual_chunk_fields_and_bytes(self):
        client = self.http.Client()
        client.request = Mock(return_value=(403, {}, b""))
        content = b"%PDF-binary\x00\xff"
        fields = {"is_private": 0, "chunk_index": 0, "total_chunk_count": 2,
                  "chunk_byte_offset": 0, "total_file_size": len(content) * 2}
        client.multipart("frappe.handler.upload_file", fields, content, "rg-http-case.pdf")
        args, kwargs = client.request.call_args
        self.assertEqual(args, ("/api/method/frappe.handler.upload_file", "POST"))
        self.assertTrue(kwargs["content_type"].startswith("multipart/form-data; boundary="))
        self.assertIn(content, kwargs["raw"])
        for key, value in fields.items():
            self.assertIn(f'name="{key}"\r\n\r\n{value}\r\n'.encode(), kwargs["raw"])

    def test_temp_probe_is_exact_and_read_only(self):
        with patch.object(self.http.subprocess, "run", return_value=SimpleNamespace(stdout="[]")) as run:
            self.assertEqual(self.http.native_temp_paths(["rg-http-case.pdf"]), [])
            args, kwargs = run.call_args
            command = args[0]
            self.assertIn("exec", command)
            self.assertEqual(command[-1], "rg-http-case.pdf")
            self.assertIn("for area in ('public','private')", command[-2])
            self.assertIn("('.temp-'+name)", command[-2])
            self.assertNotIn("write", command[-2])
            self.assertEqual(kwargs["cwd"], ROOT)
        with patch.object(self.http.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                self.http.native_temp_paths(["../private-file"])
            run.assert_not_called()

    def test_upload_cases_cover_both_commands_roles_privacies_and_nonfinal_chunks(self):
        clients = {key: SimpleNamespace(multipart=Mock(return_value=(403, {}, b"")))
                   for key in ("teacher", "one", "secretary")}
        administrator = SimpleNamespace(count=Mock(return_value=9),
            multipart=Mock(return_value=(200, {"message": {"name": "admin-file"}}, b"")))
        with patch.object(self.http, "native_temp_paths", return_value=[]) as probe:
            self.http.verify_native_upload_guard(clients, administrator, b"bytes", "123abc")
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args_list[0], probe.call_args_list[1])
        for client in clients.values():
            calls = client.multipart.call_args_list
            self.assertEqual(len(calls), 8)
            self.assertEqual({call.args[0] for call in calls}, {"upload_file", "frappe.handler.upload_file"})
            self.assertEqual({call.args[1]["is_private"] for call in calls}, {0, 1})
            chunked = [call for call in calls if "chunk_index" in call.args[1]]
            self.assertEqual(len(chunked), 4)
            self.assertTrue(all(call.args[1]["chunk_index"] == 0 and call.args[1]["total_chunk_count"] == 2
                                and call.args[1]["total_file_size"] > len(call.args[2]) for call in chunked))
        administrator.multipart.assert_called_once()

    def test_cleanup_refuses_an_unowned_or_business_referenced_identity(self):
        administrator = SimpleNamespace(count=Mock(return_value=1), request=Mock())
        self.http.EXTRA_TEACHER.update(administrator=administrator, email="existing@example.invalid")
        with self.assertRaises(ValueError):
            self.http.cleanup_extra_teacher()
        administrator.request.assert_not_called()
        self.http.EXTRA_TEACHER["email"] = "http-review-123456abcdef@example.invalid"
        with self.assertRaises(AssertionError):
            self.http.cleanup_extra_teacher()
        administrator.request.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
