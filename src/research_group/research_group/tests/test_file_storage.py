"""Real File lifecycle and independent-process physical allocation regressions."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from research_group.files import (
    ResearchGroupFile, read_stored_file_bytes, save_managed_file, zip_files,
)
from research_group.services import submit_material, upload_discussion_material
from research_group.tests import test_core as core


class TestFileStorage(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        # Reuse the existing fixture through its module, without importing or
        # subclassing its TestCase (which would register all core tests twice).
        self.fixture = core.TestResearchGroupCore(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.prefix, self.users = self.fixture.prefix, self.fixture.users
        self.aliases, self.native_files = [], []

    def tearDown(self):
        frappe.set_user("Administrator")
        for name in self.aliases:
            # Historical malicious aliases have no independent blob to remove.
            frappe.db.delete("File", {"name": name})
        for name in self.native_files:
            if frappe.db.exists("File", name):
                frappe.delete_doc("File", name, ignore_permissions=True, force=True)
        self.fixture.tearDown()
        super().tearDown()

    def _native_file(self, label="ordinary"):
        file = frappe.get_doc({"doctype": "File", "file_name": self.prefix + label + ".txt",
            "content": (self.prefix + label).encode(), "is_private": 1}).insert(ignore_permissions=True)
        self.native_files.append(file.name)
        return file

    def _historical_alias(self, managed, *, thumbnail=False):
        """Seed an already-existing bad real File row without writing a blob."""
        url = managed.file_url.rsplit("/", 1)
        alias = frappe.get_doc({"doctype": "File", "name": self.prefix + frappe.generate_hash(length=6),
            "file_name": "alias.pdf", "file_url": url[0] + "/./" + url[1],
            "thumbnail_url": None, "is_private": 1, "is_folder": 0, "folder": "Home",
            "owner": self.users["two"], "content_hash": None, "file_size": managed.file_size})
        if thumbnail:
            alias.file_url = "/private/files/" + self.prefix + "absent.txt"
            alias.thumbnail_url = url[0] + "/./" + url[1]
        alias.db_insert()
        self.aliases.append(alias.name)
        return frappe.get_doc("File", alias.name)

    def _managed_private(self):
        appointment = self.fixture.appointment()
        content = core.pdf_bytes(self.prefix + "private")
        file = save_managed_file(self.prefix + "private.pdf", content, "RG Appointment", appointment.name)
        return file, content

    def test_alias_read_list_zip_insert_and_delete_use_physical_identity(self):
        from frappe.client import get as api_get
        from frappe.core.doctype.file.utils import find_file_by_url

        managed, content = self._managed_private()
        frappe.set_user(self.users["two"])
        alias = self._historical_alias(managed)
        self.assertFalse(alias.content_hash)
        self.assertFalse(alias.has_permission("read"))
        self.assertFalse(alias.is_downloadable())
        self.assertIsNone(find_file_by_url(alias.file_url, alias.name))
        self.assertFalse(frappe.get_list("File", filters={"name": alias.name}, pluck="name"))
        for read in (lambda: api_get("File", alias.name), alias.get_content,
                     lambda: read_stored_file_bytes(alias), lambda: zip_files([alias.name])):
            frappe.response.pop("filecontent", None)
            with self.assertRaises(frappe.PermissionError):
                read()
            self.assertNotIn("filecontent", frappe.response)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc("File", alias.name, ignore_permissions=True, force=True)
        with patch.object(ResearchGroupFile, "write_file", side_effect=AssertionError("must reject before write")) as write:
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc({"doctype": "File", "file_name": "alias.pdf", "file_url": alias.file_url,
                    "is_private": 1, "content_hash": None}).insert(ignore_permissions=True)
            write.assert_not_called()
        self.assertEqual(Path(managed.get_full_path()).read_bytes(), content)

    def test_thumbnail_current_old_and_native_delete_targets_are_guarded(self):
        managed, content = self._managed_private()
        frappe.set_user(self.users["two"])
        ordinary = self._native_file()
        own_content = Path(ordinary.get_full_path()).read_bytes()
        ordinary.thumbnail_url = managed.file_url.replace("/files/", "/files/./")
        with self.assertRaises(frappe.PermissionError):
            ordinary.save(ignore_permissions=True)
        self.assertEqual(Path(ordinary.get_full_path()).read_bytes(), own_content)
        frappe.db.set_value("File", ordinary.name, "thumbnail_url", ordinary.thumbnail_url)
        old_alias = frappe.get_doc("File", ordinary.name)
        old_alias.thumbnail_url = None
        with self.assertRaises(frappe.PermissionError):
            old_alias.save(ignore_permissions=True)
        alias = self._historical_alias(managed, thumbnail=True)
        for delete in (lambda: frappe.delete_doc("File", alias.name, ignore_permissions=True, force=True),
                       lambda: alias.delete_file_data_content(only_thumbnail=True),
                       alias._delete_file_on_disk):
            with self.assertRaises(frappe.PermissionError):
                delete()
            self.assertEqual(Path(managed.get_full_path()).read_bytes(), content)
        # Native deletion maps even a foreign thumbnail basename into private/files.
        alias.thumbnail_url = "https://example.invalid/thumbs/" + Path(managed.file_url).name
        with self.assertRaises(frappe.PermissionError):
            alias.delete_file_data_content(only_thumbnail=True)
        frappe.db.set_value("File", ordinary.name, "thumbnail_url", None)
        ordinary = frappe.get_doc("File", ordinary.name)
        self.assertEqual(ordinary.get_content(), own_content.decode())
        ordinary.delete_file_data_content()
        self.assertFalse(Path(ordinary.get_full_path()).exists())
        self.assertEqual(Path(managed.get_full_path()).read_bytes(), content)

    def test_unsafe_names_cannot_collide_with_retained_material_or_generic_files(self):
        meeting, report = self.fixture.meeting()
        frappe.set_user(self.users["one"])
        filename = self.prefix + "collision_.pdf"
        content = core.pdf_bytes(self.prefix + "retained")
        result = submit_material(report.name, "保留原字节", filename, content)
        version = frappe.get_doc("RG Material Version", result["name"])
        retained = frappe.get_doc("File", version.material_file)
        self.assertEqual(retained.file_name, filename)
        for unsafe in (self.prefix + "collision" + c + ".pdf" for c in "%?#"):
            count = frappe.db.count("File")
            with self.subTest(filename=unsafe), patch.object(ResearchGroupFile, "write_file",
                    side_effect=AssertionError("unsafe filename reached physical write")) as write:
                with self.assertRaises(frappe.ValidationError):
                    submit_material(report.name, "拒绝碰撞", unsafe, core.pdf_bytes(unsafe))
                for private in (0, 1):
                    frappe.set_user("Administrator")
                    with self.assertRaises(frappe.ValidationError):
                        frappe.get_doc({"doctype": "File", "file_name": unsafe,
                            "content": core.pdf_bytes(unsafe + str(private)), "is_private": private}).insert(ignore_permissions=True)
                write.assert_not_called()
            frappe.set_user(self.users["one"])
            self.assertEqual(frappe.db.count("File"), count)
            self.assertEqual(Path(retained.get_full_path()).read_bytes(), content)
            self.assertEqual(retained.content_hash, hashlib.md5(content, usedforsecurity=False).hexdigest())
        for unsafe in ("../escape.txt", "a\\b.txt", "a/./b.txt", "a%2fb.txt"):
            with self.subTest(path=unsafe), self.assertRaises(frappe.ValidationError):
                frappe.get_doc({"doctype": "File", "file_name": unsafe,
                    "content": b"different bytes", "is_private": 1}).insert(ignore_permissions=True)
        self.assertEqual(frappe.db.count("RG Material Version", {"report": report.name}), 1)

    def test_registered_native_upload_guard_precedes_handler_and_keeps_administrator(self):
        from research_group.files import upload_file

        for command in ("upload_file", "frappe.handler.upload_file"):
            self.assertEqual(frappe.override_whitelisted_method(command), "research_group.files.upload_file")
        with patch("frappe.handler.upload_file", return_value={"native": True}) as native:
            for user in (self.users["one"], self.users["teacher"], self.users["secretary"]):
                frappe.set_user(user)
                with self.assertRaises(frappe.PermissionError):
                    upload_file()
            native.assert_not_called()
            frappe.set_user("Administrator")
            self.assertEqual(upload_file(), {"native": True})
            native.assert_called_once_with()

    def _physical_race(self, fail_second=False, discussion=False):
        reports = ([self.fixture.appointment(key) for key in ("one", "two")] if discussion
                   else [self.fixture.meeting()[1] for _ in range(2)])
        filename = self.prefix + "simultaneous.pdf"
        frappe.db.commit()
        self.fixture.committed_fixture = True
        directory = Path(frappe.get_site_path("private", "rg-test-concurrency"))
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=self.prefix, dir=directory) as temp:
            root = Path(temp).resolve()
            processes = []
            worker = "discussion_race_worker" if discussion else "storage_race_worker"
            script = f"from research_group.tests.test_file_storage import {worker}; {worker}()"
            try:
                for index, report in enumerate(reports):
                    job = {"report": report.name, "user": self.users["one"], "filename": filename,
                           "label": self.prefix + str(index), "fail": fail_second and index == 1}
                    if discussion:
                        job.update(appointment=report.name, user=self.users[("one", "two")[index]])
                    argv = [sys.executable, "-c", script, frappe.local.site,
                            str(Path(frappe.local.sites_path).resolve()), str(root), str(index), json.dumps(job)]
                    processes.append(subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                deadline = time.monotonic() + 35
                while not all((root / f"{index}.ready").exists() for index in range(2)):
                    if time.monotonic() > deadline or any(process.poll() is not None for process in processes):
                        raise AssertionError("File workers did not reach the physical allocation barrier")
                    time.sleep(0.05)
                selected = [json.loads((root / f"{index}.ready").read_text()) for index in range(2)]
                self.assertEqual(selected[0]["path"], selected[1]["path"], selected)
                (root / "start").write_text("start", encoding="ascii")
                results = []
                for process in processes:
                    out, err = process.communicate(timeout=45)
                    self.assertEqual(process.returncode, 0, err[-2000:])
                    results.append(json.loads(out.strip().splitlines()[-1]))
                self.assertEqual(len({result["connection"] for result in results}), 2)
                self.assertEqual([result["ok"] for result in results], [True, not fail_second], results)
                if fail_second:
                    self.assertEqual(results[1]["error"], "injected after physical write")
                frappe.db.rollback()  # discard the parent's pre-worker snapshot
                kept_paths = set()
                for index, report in enumerate(reports):
                    if discussion:
                        files = frappe.get_all("File", filters={"attached_to_doctype": "RG Appointment",
                            "attached_to_name": report.name}, pluck="name")
                        self.assertEqual(files, [results[index]["file"]])
                        file = frappe.get_doc("File", files[0])
                        expected = core.pdf_bytes(self.prefix + str(index))
                        stored = read_stored_file_bytes(file)
                        self.assertEqual(stored, expected)
                        self.assertEqual(results[index]["sha256"], hashlib.sha256(expected).hexdigest())
                        self.assertEqual(hashlib.sha256(stored).hexdigest(), results[index]["sha256"])
                        self.assertEqual(file.file_size, len(expected))
                        self.assertEqual(file.content_hash, hashlib.md5(expected, usedforsecurity=False).hexdigest())
                        self.assertEqual(file.file_name, filename)
                        self.assertEqual(file.owner, self.users[("one", "two")[index]])
                        self.assertTrue(file.is_private)
                        kept_paths.add(str(Path(file.get_full_path()).resolve()))
                        continue
                    rows = frappe.get_all("RG Material Version", filters={"report": report.name}, fields=["material_file", "sha256", "size_bytes"])
                    self.assertEqual(len(rows), int(index == 0 or not fail_second))
                    if rows:
                        file = frappe.get_doc("File", rows[0].material_file)
                        expected = core.pdf_bytes(self.prefix + str(index))
                        self.assertEqual(read_stored_file_bytes(file), expected)
                        self.assertEqual(rows[0].sha256, hashlib.sha256(expected).hexdigest())
                        self.assertEqual(rows[0].size_bytes, len(expected))
                        self.assertEqual(file.content_hash, hashlib.md5(expected, usedforsecurity=False).hexdigest())
                        self.assertEqual(file.file_name, filename)
                        kept_paths.add(str(Path(file.get_full_path()).resolve()))
                self.assertEqual(len(kept_paths), 1 if fail_second else 2)
                if discussion:
                    self.assertNotEqual(results[0]["sha256"], results[1]["sha256"])
                if fail_second:
                    failed_path = Path((root / "1.written").read_text())
                    self.assertNotIn(str(failed_path), kept_paths)
                    self.assertFalse(failed_path.exists())
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)

    def test_different_report_processes_reserve_distinct_physical_files(self):
        self._physical_race()

    def test_failed_process_compensation_preserves_other_report_bytes(self):
        self._physical_race(fail_second=True)

    def test_concurrent_discussion_endpoint_uploads_keep_distinct_original_bytes(self):
        self._physical_race(discussion=True)


def _write_at_physical_barrier(file, root, index, write_file):
    """Share the same pre-allocation barrier across primitive and endpoint races."""
    (root / f"{index}.ready").write_text(json.dumps({"path": str(Path(file.get_full_path()).resolve())}), encoding="utf-8")
    deadline = time.monotonic() + 35
    while not (root / "start").exists():
        if time.monotonic() > deadline:
            raise AssertionError("physical allocation barrier timed out")
        time.sleep(0.02)
    path = write_file(file)
    (root / f"{index}.written").write_text(str(Path(path).resolve()), encoding="utf-8")
    return path


def discussion_race_worker():
    """Use real multipart parsing and the authorized public service without naming hooks."""
    from io import BytesIO
    from werkzeug.test import EnvironBuilder
    from werkzeug.wrappers import Request

    site, sites_path, root_text, index, job_text = sys.argv[1:]
    root, job = Path(root_text), json.loads(job_text)
    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.set_user(job["user"])
    connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
    content = core.pdf_bytes(job["label"])
    builder = EnvironBuilder(method="POST",
        path="/api/method/research_group.services.upload_discussion_material",
        data={"doctype": "RG Appointment", "name": job["appointment"],
              "file": (BytesIO(content), job["filename"])})
    request = Request(builder.get_environ())
    frappe.local.request = request
    frappe.local.form_dict = frappe._dict(doctype="RG Appointment", name=job["appointment"])
    write_file = ResearchGroupFile.write_file

    def synchronized_write(file):
        return _write_at_physical_barrier(file, root, index, write_file)

    try:
        with patch.object(ResearchGroupFile, "write_file", synchronized_write):
            result = upload_discussion_material("RG Appointment", job["appointment"])
        file = frappe.get_doc("File", result["name"])
        stored_hash = hashlib.sha256(read_stored_file_bytes(file)).hexdigest()
        frappe.db.commit()
        print(json.dumps({"connection": connection, "ok": True, "file": file.name, "sha256": stored_hash}))
    except BaseException:
        frappe.db.rollback()
        raise
    finally:
        request.close()
        builder.close()
        frappe.destroy()


def storage_race_worker():
    """Run only in the isolated Frappe test subprocess selected by the test."""
    from research_group.controllers import RGMaterialVersion

    site, sites_path, root_text, index, job_text = sys.argv[1:]
    root, job = Path(root_text), json.loads(job_text)
    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.set_user(job["user"])
    connection = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
    write_file, after_insert = ResearchGroupFile.write_file, ResearchGroupFile.after_insert

    def wait_for(path):
        deadline = time.monotonic() + 35
        while not path.exists():
            if time.monotonic() > deadline:
                raise AssertionError("physical allocation barrier timed out")
            time.sleep(0.02)

    def synchronized_write(file):
        return _write_at_physical_barrier(file, root, index, write_file)

    def injected_after_insert(file):
        after_insert(file)
        if job["fail"]:
            wait_for(root / "0.committed")
            raise frappe.ValidationError("injected after physical write")

    def independent_version_name(version):
        # Isolate physical allocation from the unrelated global tabSeries lock.
        # The actual version validation, File lifecycle and transaction still run.
        version.name = root.name + "-material-" + index

    try:
        with patch.object(ResearchGroupFile, "write_file", synchronized_write), \
                patch.object(ResearchGroupFile, "after_insert", injected_after_insert), \
                patch.object(RGMaterialVersion, "autoname", independent_version_name, create=True):
            try:
                result = submit_material(job["report"], "独立报告并发", job["filename"], core.pdf_bytes(job["label"]))
            except frappe.ValidationError as error:
                # Deliberately commit after the service catches its savepoint
                # failure: only explicit compensation can remove this blob.
                frappe.db.commit()
                result = {"ok": False, "error": str(error)}
            else:
                frappe.db.commit()
                result = {"ok": True, "version": result["name"]}
                (root / f"{index}.committed").write_text("committed", encoding="ascii")
        print(json.dumps({"connection": connection, **result}))
    finally:
        frappe.destroy()
