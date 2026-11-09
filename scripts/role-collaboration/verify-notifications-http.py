"""Real HTTP acceptance for fictional users on the isolated mail candidate.

Preparation does not run this file. The integration owner runs it only after
seeding the four demo profiles and an isolated automatic mail event. No worker,
SMTP transport, database helper, or account-token operation is invoked here.
"""
import argparse
import json
import re
import secrets
import sys
import urllib.parse
from collections import Counter

import role_http as core
import acceptance_config as cfg


PREFIX = "/api/method/research_group.notifications."
SAFE_STATUS_FIELDS = {
    "name", "template", "status", "status_label", "attempts",
    "last_attempt_at", "submitted_at", "created_at",
}
MAIL_STATUSES = {
    "queued", "preparing", "sending", "captured", "unconfigured",
    "failed", "unknown", "submitted", "cancelled",
}


def rpc(client, method, **data):
    return core.rpc(client, "notifications." + method, **data)


def denied_write(label, client, method, data, **headers):
    core.denied(label, client.request(PREFIX + method, "POST", data, headers=headers))


def get_rpc(client, method, **data):
    query = urllib.parse.urlencode(data)
    return client.request(PREFIX + method + ("?" + query if query else ""))


def preview(client, label, subject, body, recipients, expected, sender):
    value = core.success(label, rpc(client, "preview_message", subject=subject, body=body,
                                   recipients=recipients, kind="important"))
    checks = {
        "subject_exact": value.get("subject") == subject,
        "body_exact": value.get("body") == body,
        "kind_exact": value.get("kind") == "important",
        "unpublished": value.get("status") == "preview",
        "recipient_count_exact": value.get("recipient_count") == len(expected),
        "recipient_identity_exact": value.get("recipients") == expected,
        "fixed_sender_exact": value.get("from_address") == sender,
        "expiry_present": bool(value.get("expires_at")),
        "digest_format_valid": isinstance(value.get("confirmation_digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["confirmation_digest"]) is not None,
        "no_events": not value.get("events"),
    }
    # Keep field-level booleans and the newly created identifier on failure.
    # Never store body/address/digest values. A later attempt creates a fresh
    # preview and does not load, confirm, retry, or delete a failed attempt's ID.
    core.check(label + " complete immutable preview", all(checks.values()),
               field_checks=checks, preview_id=value.get("name"))
    return value


def unchanged_preview(client, value, label):
    current = core.success(label, rpc(client, "get_message", name=value["name"]))
    core.check(label + " no publication or events", current.get("status") == "preview"
               and current.get("subject") == value["subject"]
               and current.get("body") == value["body"]
               and current.get("recipients") == value["recipients"]
               and current.get("confirmation_digest") == value["confirmation_digest"]
               and not current.get("events"))


def status_summary(label, response):
    value = core.success(label, response)
    rows = value.get("events")
    core.check(label + " bounded safe envelope", set(value) == {"events"}
               and isinstance(rows, list) and len(rows) <= 20)
    for index, row in enumerate(rows):
        core.check(label + " safe fields " + str(index), isinstance(row, dict)
                   and set(row) == SAFE_STATUS_FIELDS
                   and row.get("template") != "manual"
                   and row.get("status") in MAIL_STATUSES
                   and isinstance(row.get("status_label"), str)
                   and isinstance(row.get("attempts"), int)
                   and all(row.get(key) is None or isinstance(row[key], str)
                           for key in ("last_attempt_at", "submitted_at", "created_at")))
        # Authentication links, recipient addresses, bodies and source bindings
        # must be absent even when the row belongs to the logged-in member.
        serialized = json.dumps(row, ensure_ascii=False)
        core.check(label + " no address or secret link " + str(index),
                   not any(fragment in serialized.lower() for fragment in (
                       "@", "#token=", "#key=", "?token=", "?key=", "/rg_join",
                       "body_ciphertext", "related_binding", "recipient_email",
                   )))
        if row["status"] == "captured":
            core.check(label + " capture is not delivery " + str(index),
                       "未向外发送" in row["status_label"])
        if row["status"] == "submitted":
            core.check(label + " acceptance is not receipt " + str(index),
                       "SMTP" in row["status_label"] and "未验证" in row["status_label"])
        if row["status"] == "unknown":
            core.check(label + " unknown does not permit resend " + str(index),
                       "未知" in row["status_label"] and "禁止" in row["status_label"])
    return rows


def run(clients):
    teacher, secretary, one, two = (clients[key] for key in ("teacher", "secretary", "one", "two"))
    options = {key: core.success("recipient options " + key, rpc(clients[key], "recipient_options"))
               for key in ("teacher", "secretary")}
    modes = {value.get("configuration", {}).get("mode") for value in options.values()}
    core.check("mail transport isolated before any write", len(modes) == 1
               and modes.issubset({"capture", "disabled"}))
    sender = options["teacher"].get("configuration", {}).get("from_address")
    core.check("fixed sender shared by both actors", isinstance(sender, str)
               and sender == options["secretary"].get("configuration", {}).get("from_address"))
    recipients = sorted(core.ACCOUNTS[key] for key in ("one", "two"))
    expected = None
    for role, value in options.items():
        members = {row["user"]: row for row in value.get("members", [])}
        core.check("verified group fixture " + role, all(user in members
                   and members[user].get("eligible") and members[user].get("email")
                   and members[user].get("email_version", 0) > 0 for user in recipients))
        selected = [{key: members[user][key] for key in ("user", "display_name", "email")}
                    for user in recipients]
        if expected is None:
            expected = selected
        core.check("both actors see same recipient identity " + role, selected == expected)
    for role, student in (("one", one), ("two", two)):
        core.denied("student cannot enumerate mail recipients " + role, rpc(student, "recipient_options"))
        core.denied("student cannot mass mail " + role, rpc(student, "preview_message",
                    subject="forbidden", body="forbidden", recipients=recipients))
        core.denied("student cannot list manual mail " + role, rpc(student, "list_messages"))

    # A nonempty automatic fixture prevents a vacuous privacy assertion. This
    # read occurs before this script creates any manual preview or confirmation.
    automatic = {role: status_summary("own automatic summary " + role,
                 rpc(client, "automatic_mail_status")) for role, client in clients.items()}
    core.check("automatic privacy fixture is nonempty", any(automatic.values()))
    managed = status_summary("secretary sees ordinary student summary",
                             rpc(secretary, "automatic_mail_status", user=core.ACCOUNTS["one"]))
    core.check("secretary summary has same event identities as owner",
               [(row["name"], row["template"]) for row in managed]
               == [(row["name"], row["template"]) for row in automatic["one"]])
    for role, client, target in (
        ("student peer", one, core.ACCOUNTS["two"]),
        ("teacher other", teacher, core.ACCOUNTS["one"]),
        ("secretary protected teacher", secretary, core.ACCOUNTS["teacher"]),
        ("student secretary", two, core.ACCOUNTS["secretary"]),
    ):
        core.denied("automatic summary forbids " + role, rpc(client, "automatic_mail_status", user=target))
    core.denied("batch summary helper is not public", one.request(
        "/api/method/research_group.notifications.automatic_status_rows", "POST",
        {"user_ids": [core.ACCOUNTS["teacher"]]}))

    marker = "Role mail HTTP fictional " + secrets.token_hex(5)
    body = marker + "\nFirst complete line.\nSecond complete line: <literal> & text.\nFinal complete line."
    drafts = {
        "teacher": preview(teacher, "teacher full preview", marker + " teacher", body,
                           recipients, expected, sender),
        "secretary": preview(secretary, "secretary full preview", marker + " secretary", body,
                             recipients, expected, sender),
    }
    changed = preview(teacher, "edited content requires fresh preview", marker + " edited",
                      body + "\nChanged after the first preview.", recipients, expected, sender)
    core.check("edited preview has a fresh digest", changed["confirmation_digest"]
               != drafts["teacher"]["confirmation_digest"])
    core.denied("old digest cannot confirm edited preview", rpc(teacher, "confirm_message",
                name=changed["name"], confirmation_digest=drafts["teacher"]["confirmation_digest"]))
    unchanged_preview(teacher, changed, "old digest leaves edited preview unchanged")

    for role, other in (("teacher", "secretary"), ("secretary", "teacher")):
        value = drafts[role]
        core.denied("actor cannot read other actor preview " + other,
                    rpc(clients[other], "get_message", name=value["name"]))
        core.denied("actor cannot confirm other actor preview " + other,
                    rpc(clients[other], "confirm_message", name=value["name"],
                        confirmation_digest=value["confirmation_digest"]))
        listing = core.success("actor scoped list " + role, rpc(clients[role], "list_messages", limit=50))
        names = {row["name"] for row in listing.get("messages", [])}
        core.check("list excludes other actor " + role, value["name"] in names
                   and drafts[other]["name"] not in names)
    for role, student in (("one", one), ("two", two)):
        core.denied("student cannot read preview " + role,
                    rpc(student, "get_message", name=drafts["teacher"]["name"]))
        core.denied("student cannot confirm preview " + role, rpc(student, "confirm_message",
                    name=drafts["teacher"]["name"],
                    confirmation_digest=drafts["teacher"]["confirmation_digest"]))

    valid = {"name": drafts["teacher"]["name"],
             "confirmation_digest": drafts["teacher"]["confirmation_digest"]}
    for field, value in (("actor", core.ACCOUNTS["secretary"]), ("from_address", "forged@example.invalid"),
                         ("body", "forged body"), ("recipients", [core.ACCOUNTS["teacher"]])):
        core.denied("confirmation rejects parameter " + field,
                    rpc(teacher, "confirm_message", **valid, **{field: value}))
    for field in ("actor", "from_address"):
        core.denied("preview rejects parameter " + field, rpc(teacher, "preview_message",
                    subject=marker, body=body, recipients=recipients, **{field: "forged@example.invalid"}))
    core.denied("preview forbids external recipient", rpc(teacher, "preview_message",
                subject=marker, body=body, recipients=["outside@example.invalid"]))
    core.denied("preview forbids mixed selectors", rpc(teacher, "preview_message", subject=marker,
                body=body, recipients=recipients, all_members=True))
    core.denied("GET cannot preview", get_rpc(teacher, "preview_message", subject=marker,
                body=body, recipients=json.dumps(recipients)))
    core.denied("GET cannot confirm", get_rpc(teacher, "confirm_message", **valid))
    core.check("foreign Origin request retains valid native CSRF", bool(teacher.csrf))
    denied_write("correct CSRF cannot authorize foreign Origin", teacher, "confirm_message", valid,
                 Origin="https://outside.example.invalid")
    denied_write("correct CSRF cannot authorize other local port", teacher, "confirm_message", valid,
                 Origin="http://localhost:8088")
    denied_write("foreign Referer without Origin is rejected", teacher, "confirm_message", valid,
                 Origin=None, Referer="https://outside.example.invalid/forged")
    denied_write("confirmation requires native CSRF", teacher, "confirm_message", valid,
                 **{"X-Frappe-CSRF-Token": None})
    denied_write("confirmation rejects invalid CSRF", teacher, "confirm_message", valid,
                 **{"X-Frappe-CSRF-Token": "invalid-fictional-token"})
    unchanged_preview(teacher, drafts["teacher"], "all rejected writes leave preview unchanged")

    published = {}
    for role in ("teacher", "secretary"):
        value, client = drafts[role], clients[role]
        args = {"name": value["name"], "confirmation_digest": value["confirmation_digest"]}
        first = core.success("actor confirms own preview " + role, rpc(client, "confirm_message", **args))
        replay = core.success("repeated confirmation " + role, rpc(client, "confirm_message", **args))
        current = core.success("published message reread " + role, rpc(client, "get_message", name=value["name"]))
        events = current.get("events", [])
        event_ids = sorted(row["name"] for row in events)
        core.check("one event per intended recipient " + role, current.get("status") == "published"
                   and len(events) == 2 and len(set(event_ids)) == 2
                   and sorted(row["recipient_user"] for row in events) == recipients
                   and current.get("subject") == value["subject"] and current.get("body") == body
                   and current.get("recipients") == expected)
        core.check("repeat returns original events without duplicates " + role,
                   sorted(row["name"] for row in first.get("events", [])) == event_ids
                   == sorted(row["name"] for row in replay.get("events", [])))
        core.check("isolated events are never SMTP accepted " + role,
                   all(row.get("status") in MAIL_STATUSES - {"submitted", "sending"} for row in events))
        core.denied("GET cannot retry a real event " + role,
                    get_rpc(client, "retry_event", name=event_ids[0]))
        other = secretary if role == "teacher" else teacher
        core.denied("other actor cannot read published mail " + role,
                    rpc(other, "get_message", name=value["name"]))
        core.denied("other actor cannot retry event " + role, rpc(other, "retry_event", name=event_ids[0]))
        core.denied("student cannot retry event " + role, rpc(one, "retry_event", name=event_ids[0]))
        core.denied("own native mail row remains private " + role, client.get("RG Message", value["name"]))
        core.denied("own native event row remains private " + role, client.get("RG Mail Event", event_ids[0]))
        published[role] = {"name": value["name"], "event_ids": event_ids,
                           "observed_statuses": dict(Counter(row["status"] for row in events))}
    unchanged_preview(teacher, changed, "edited preview remains unconfirmed")
    # Re-read automatic views after manual publication: manual events must not
    # enter the otherwise safe automatic-status interface.
    manual_ids = {event for value in published.values() for event in value["event_ids"]}
    for role, client in clients.items():
        rows = status_summary("automatic summary after manual mail " + role, rpc(client, "automatic_mail_status"))
        core.check("automatic summary excludes manual events " + role,
                   not manual_ids.intersection(row["name"] for row in rows))
    return {"mail_mode": next(iter(modes)), "manual_messages": published,
            "unconfirmed_edited_preview": changed["name"],
            "automatic_fixture_counts": {role: len(rows) for role, rows in automatic.items()},
            "worker_invoked": False, "smtp_invoked": False, "delivered_claimed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=core.BASE, help="isolated localhost/127.0.0.1:8089 URL")
    parser.add_argument("--receipt", default="notifications-http-r3.json", help="new file name in task records")
    args = parser.parse_args()
    cfg.base_url(args.base)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json", args.receipt):
        parser.error("Receipt must be a JSON file name within this task's records directory")
    if cfg.current().receipt(args.receipt).exists():
        parser.error("Receipt already exists; use a fresh name to preserve prior evidence")
    core.BASE = args.base.rstrip("/")
    records, error = {}, None
    try:
        passwords = core.credentials()
        clients = {key: core.Client().login(key, passwords) for key in ("teacher", "secretary", "one", "two")}
        del passwords
        records = run(clients)
    except Exception as exc:
        # Never emit credentials, HTTP bodies, CSRF, mail addresses, digests, or
        # token links in a receipt or stdout, including failure paths.
        error = type(exc).__name__
    destination = core.save_receipt(args.receipt, error=error, records=records)
    print(json.dumps({"ok": error is None, "checks": len(core.RESULTS), "receipt": str(destination)},
                     ensure_ascii=False))
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
