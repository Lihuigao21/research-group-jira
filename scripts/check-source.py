"""Offline source checks. Does not claim Frappe runtime or browser acceptance."""
import ast
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "research_group" / "research_group"
COLLABORATION = {
    "RG Registration", "RG Account Profile", "RG Account Audit", "RG Message", "RG Mail Event",
    "RG Meal Round", "RG Meal Item", "RG Meal Order", "RG Meal Order Item", "RG Manuscript",
    "RG Manuscript Version", "RG Manuscript Review", "RG Manuscript Selection", "RG Research Direction",
    "RG Direction Topic", "RG Portal Settings", "RG Announcement",
}


def main():
    python_files = sorted(APP.rglob("*.py"))
    for file in python_files:
        ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    metadata = sorted((APP / "research_group").rglob("*.json"))
    doctypes = {}
    for file in metadata:
        doc = json.loads(file.read_text(encoding="utf-8"))
        if doc["doctype"] != "DocType":
            continue
        doctypes[doc["name"]] = doc
        fields = {f["fieldname"] for f in doc["fields"]}
        assert set(doc["field_order"]) == fields, file
        if doc.get("title_field"):
            assert doc["title_field"] in fields, file
        assert not doc["allow_rename"], file
        if doc.get("istable"):
            assert not doc["permissions"], file
        elif doc["name"].startswith("RG AI "):
            assert doc["track_changes"] == 0, "Private AI content must never enter native Version history"
            assert not doc["permissions"], "Internal AI tables must not become native sidebar or CRUD entries"
            assert not doc.get("show_name_in_global_search"), file
        elif doc["name"] in {"RG Survey", "RG Survey Response"}:
            assert doc["track_changes"] == 0, "Survey content must not leak through native Version history"
            if doc["name"] == "RG Survey Response":
                assert all(not p.get("create") and not p.get("write") for p in doc["permissions"]), file
        elif doc["name"] in COLLABORATION:
            assert not doc["permissions"], "Collaboration internals are available only through dedicated services"
            assert not doc.get("show_name_in_global_search"), file
            if doc["name"] not in {"RG Portal Settings", "RG Announcement", "RG Research Direction"}:
                assert not doc.get("track_changes"), "Private content and authentication keys must not enter Version"
        else:
            assert doc["track_changes"] == 1, file
        assert all(not p.get("share") and not p.get("delete") for p in doc["permissions"]), file
    expected = {"RG Member", "RG Topic", "RG Progress", "RG Appointment", "RG Discussion Record",
                "RG Group Meeting", "RG Meeting Report", "RG Material Version", "RG Survey",
                "RG Survey Question", "RG Survey Response"}
    expected |= {"RG AI State", "RG AI Conversation", "RG AI Message", "RG AI Memory", "RG AI Plan",
                 "RG AI Operation", "RG AI Run", "RG AI Tombstone"}
    expected |= COLLABORATION
    assert set(doctypes) == expected, set(doctypes)
    assert {name for name, doc in doctypes.items() if doc.get("istable")} == {
        "RG Survey Question", "RG Meal Item", "RG Meal Order Item", "RG Direction Topic",
    }
    for doc in doctypes.values():
        for field in doc["fields"]:
            if field.get("fieldtype") == "Link" and field["options"].startswith("RG "):
                assert field["options"] in doctypes
            if field.get("fieldtype") == "Table":
                assert field["options"] in doctypes and doctypes[field["options"]].get("istable"), file
    js_files = sorted((APP / "public" / "js").glob("*.js")) + sorted((APP / "research_group" / "page").rglob("*.js"))
    for file in js_files:
        subprocess.run(["node", "--check", str(file)], check=True, cwd=ROOT)
    shell_files = sorted((ROOT / "scripts").glob("*.sh"))
    assert all(b"\r" not in p.read_bytes() for p in shell_files), "Linux scripts must use LF"
    receipt = {"check": "offline-source", "ok": True, "python_modules": len(python_files),
               "metadata_files": len(metadata), "doctypes": len(doctypes), "js_files": len(js_files),
               "lf_shell_scripts": len(shell_files), "runtime_tested": False, "browser_tested": False}
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()

