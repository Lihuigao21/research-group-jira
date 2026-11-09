"""Isolated storage guard checks; not a substitute for the real Frappe tests."""
import ast
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile


PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "src/research_group/research_group"
sys.path.insert(0, str(PROJECT / "src/research_group"))
MANAGED = ("RG Appointment", "RG Material Version", "RG Manuscript Version")
FILE_TOKEN = object()


class PermissionError(Exception):
    pass


class ValidationError(Exception):
    pass


class Data(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__


class NativeFile(Data):
    def __init__(self, **values):
        super().__init__(is_folder=False, file_name="same.txt", file_url="/private/files/same.txt",
                         is_private=1, attached_to_doctype=None, content_hash=None, **values)
        self.flags = Data()
        self._content = self.get("content", b"bytes")

    @property
    def is_remote_file(self):
        return bool(self.file_url and self.file_url.startswith("https://"))

    def is_new(self):
        return not self.get("existing")

    def get_doc_before_save(self):
        return self.get("old")

    def get_full_path(self):
        return str(self.root / "private/files" / self.file_url.rsplit("/", 1)[1])

    def validate_file_path(self):
        pass

    def validate_file_url(self):
        pass

    def check_content(self):
        pass

    def get_content(self, encodings=None):
        return self._content

    def before_insert(self):
        self.native_inserted = True

    def on_rollback(self):
        pass

    def delete_file_data_content(self, only_thumbnail=False):
        return only_thumbnail

    def write_file(self):
        self.native_written = True
        return self.get_full_path()


def load_storage(root):
    state = Data(rows=[], roles=[], root=Path(root))
    callbacks = Data(add=lambda callback: None)
    frappe = types.ModuleType("frappe")
    frappe.local = Data(request=None)
    frappe.session = Data(user="Administrator")
    frappe.response = {}
    frappe.form_dict = Data()
    frappe.PermissionError, frappe.ValidationError = PermissionError, ValidationError
    frappe.whitelist = lambda **kwargs: lambda function: function
    frappe.get_site_path = lambda *parts: str(state.root.joinpath(*parts))
    frappe.get_all = lambda *args, **kwargs: state.rows
    frappe.get_roles = lambda: state.roles
    frappe.generate_hash = lambda length: os.urandom(length).hex()[:length]
    frappe.db = Data(after_rollback=callbacks)

    def throw(message, exc=ValidationError, **kwargs):
        raise exc(message)

    frappe.throw = throw
    utils = types.ModuleType("frappe.utils")
    utils.get_url = lambda: "http://lab.localhost"
    utils.cint = lambda value: int(value or 0)
    handler = types.ModuleType("frappe.handler")
    handler.upload_file = lambda: {"native": True}
    policy = types.ModuleType("research_group.policy")
    policy.ROLES = ("business",)
    policy.deny = lambda: throw("denied", PermissionError)
    sys.modules.update({"frappe": frappe, "frappe.utils": utils,
                        "frappe.handler": handler, "research_group.policy": policy})
    from research_group.ai import AI_DOCTYPES
    ai_storage = types.ModuleType("research_group.ai.storage")
    guard_tree = ast.parse((SOURCE / "ai/storage.py").read_text(encoding="utf-8"))
    guard_function = next(node for node in guard_tree.body if isinstance(node, ast.FunctionDef)
                          and node.name == "guard_native")
    ai_storage.__dict__.update({"AI_DOCTYPES": AI_DOCTYPES, "deny": lambda: throw("denied", PermissionError)})
    exec(compile(ast.Module(body=[guard_function], type_ignores=[]), "ai/storage.py", "exec"), ai_storage.__dict__)
    sys.modules["research_group.ai.storage"] = ai_storage
    tree = ast.parse((SOURCE / "files.py").read_text(encoding="utf-8"))
    start = next(index for index, node in enumerate(tree.body)
                 if isinstance(node, ast.FunctionDef) and node.name == "_file_reference_identities")
    namespace = {"frappe": frappe, "os": os, "Path": Path, "unquote": unquote, "urlsplit": urlsplit,
                 "File": NativeFile, "MANAGED": MANAGED, "FILE_TOKEN": FILE_TOKEN,
                 "get_content_hash": lambda content: hashlib.md5(content).hexdigest(),
                 "deny": lambda: throw("denied", PermissionError), "BytesIO": BytesIO,
                 "ZipFile": ZipFile, "MAX_ARCHIVE_BYTES": 250 * 1024 * 1024}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == "SURVEY_FILE_DOCTYPES" for target in node.targets)):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE / "files.py"), "exec"), namespace)
    exec(compile(ast.Module(body=tree.body[start:], type_ignores=[]), str(SOURCE / "files.py"), "exec"), namespace)
    storage_module = types.ModuleType("research_group.files")
    storage_module.__dict__.update(namespace)
    sys.modules["research_group.files"] = storage_module
    cls = namespace["ResearchGroupFile"]
    cls.root = state.root
    (state.root / "private/files").mkdir(parents=True, exist_ok=True)
    return namespace, state, frappe


def allocation_worker():
    root, index = Path(sys.argv[2]), sys.argv[3]
    namespace, state, frappe = load_storage(root)
    file = namespace["ResearchGroupFile"]()
    file._content = ("different bytes " + index).encode()
    (root / (index + ".ready")).write_text("ready")
    deadline = time.monotonic() + 15
    while not (root / "start").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("worker barrier timed out")
        time.sleep(0.01)
    path = file.write_file()
    print(json.dumps({"path": path, "content": Path(path).read_text(), "pid": os.getpid()}))


class StorageGuardChecks(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / "tasks/20261106-frappe-v1/work/file-storage-host-check"
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.namespace, self.state, self.frappe = load_storage(self.temp.name)
        self.File = self.namespace["ResearchGroupFile"]
        self.managed = Data(name="managed", file_url="/private/files/retained.pdf", thumbnail_url=None,
                            attached_to_doctype="RG Appointment", content_hash="managedhash")
        self.state.rows = [self.managed]

    def test_ai_parent_stops_before_native_insert(self):
        from research_group.ai import AI_DOCTYPES
        for doctype in AI_DOCTYPES:
            file = self.File()
            file.attached_to_doctype = doctype
            with self.subTest(doctype=doctype), self.assertRaises(PermissionError):
                file.before_insert()
            self.assertFalse(file.get("native_inserted"))

    def test_collaboration_parent_stops_before_native_insert(self):
        from research_group.collaboration_security import INTERNAL_DOCTYPES
        for doctype in INTERNAL_DOCTYPES:
            file = self.File()
            file.attached_to_doctype = doctype
            with self.subTest(doctype=doctype), self.assertRaises(PermissionError):
                file.before_insert()
            self.assertFalse(file.get("native_inserted"))

    def test_current_old_thumbnail_and_delete_identities(self):
        alias = self.File()
        alias.file_url = "/private/files/./retained.pdf"
        self.assertTrue(self.namespace["is_managed_file_alias"](alias))
        for read in (alias._guard, alias.get_content, lambda: self.namespace["read_stored_file_bytes"](alias)):
            with self.assertRaises(PermissionError):
                read()
        alias.file_url = "/private/files/unrelated.txt"
        alias.thumbnail_url = "https://example.invalid/thumbnails/retained.pdf"
        with self.assertRaises(PermissionError):
            alias.delete_file_data_content(only_thumbnail=True)
        alias.thumbnail_url = None
        alias.existing = True
        alias.old = Data(file_url="/private/files/./retained.pdf")
        with self.assertRaises(PermissionError):
            alias._guard()

    def test_unsafe_generic_names_stop_before_native_insert(self):
        for name in ("a%b.txt", "a?b.txt", "a#b.txt", "../a.txt", "a\\b.txt", "a/./b.txt"):
            file = self.File()
            file.file_name = name
            with self.subTest(name=name), self.assertRaises(ValidationError):
                file.before_insert()
            self.assertFalse(file.get("native_inserted"))

    def test_file_read_and_list_policy_excludes_unmanaged_aliases(self):
        alias = Data(name="alias", attached_to_doctype=None, file_url="/private/files/./retained.pdf", thumbnail_url=None)
        unrelated = Data(name="ordinary", attached_to_doctype=None, file_url="/private/files/ordinary.txt", thumbnail_url=None)
        self.state.rows = [self.managed, alias, unrelated]
        self.assertEqual(self.namespace["managed_file_alias_names"](), ["alias"])
        # The real query uses filters; keep only managed rows for the single-row
        # guard, then compile the actual File permission hook in isolation.
        self.state.rows = [self.managed]
        tree = ast.parse((SOURCE / "permissions.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "file_has_permission")
        namespace = {"MANAGED": MANAGED, "READ": {"read"}, "frappe": self.frappe}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "permissions.py", "exec"), namespace)
        self.assertFalse(namespace["file_has_permission"](alias))
        self.assertTrue(namespace["file_has_permission"](unrelated))

    def test_existing_blob_and_failed_new_reservation_are_preserved(self):
        self.state.rows = []
        existing = self.state.root / "private/files/same.txt"
        existing.write_bytes(b"retained")
        file = self.File()
        tracker = []
        file.flags.rg_created_files = tracker
        with patch("os.fsync", side_effect=OSError("injected fsync failure")):
            with self.assertRaises(OSError):
                file.write_file()
        self.assertEqual(existing.read_bytes(), b"retained")
        self.assertEqual(len(tracker), 1)
        self.assertFalse(Path(tracker[0]["path"]).exists())
        self.assertNotEqual(Path(tracker[0]["path"]), existing)
        self.assertFalse(file.flags.get("new_file"))

    def test_unrelated_in_place_native_behavior_and_collision_budget(self):
        file = self.File()
        file.existing = True
        self.assertEqual(file.write_file(), file.get_full_path())
        self.assertTrue(file.native_written)
        candidate = self.namespace["_collision_filename"]("中" * 81 + "aa.pdf")
        self.assertLessEqual(len(candidate.encode("utf-8")), 255)
        self.assertLessEqual(len(candidate), 140)

    def test_generic_upload_guard_and_both_registered_commands(self):
        self.frappe.session.user = "business-user"
        self.state.roles = ["business"]
        with self.assertRaises(PermissionError):
            self.namespace["upload_file"]()
        self.frappe.session.user = "Administrator"
        self.assertEqual(self.namespace["upload_file"](), {"native": True})
        hooks = {}
        exec(compile((SOURCE / "hooks.py").read_text(encoding="utf-8"), "hooks.py", "exec"), hooks)
        for command in ("upload_file", "frappe.handler.upload_file"):
            self.assertEqual(hooks["override_whitelisted_methods"][command], "research_group.files.upload_file")

    def test_independent_process_exclusive_allocation(self):
        processes = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", self.temp.name, str(i)],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
        try:
            root = Path(self.temp.name)
            deadline = time.monotonic() + 15
            while not all((root / f"{i}.ready").exists() for i in range(2)):
                if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                    self.fail("allocation workers failed to reach the barrier")
                time.sleep(0.01)
            (root / "start").write_text("start")
            results = []
            for process in processes:
                out, err = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, err)
                results.append(json.loads(out.strip()))
            self.assertEqual(len({r["pid"] for r in results}), 2)
            self.assertEqual(len({r["path"] for r in results}), 2)
            for index, result in enumerate(results):
                self.assertEqual(Path(result["path"]).read_bytes(), ("different bytes " + str(index)).encode())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        allocation_worker()
    else:
        unittest.main(verbosity=2)
