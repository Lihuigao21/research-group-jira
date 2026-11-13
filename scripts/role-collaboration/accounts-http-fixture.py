"""Candidate-only, non-whitelisted helper for verify-accounts-http.py.

The caller streams this source and its request to the candidate Python process.
Its stdout is a private pipe: capture links must never be echoed or logged.
This file does not run anything when imported or compiled.
"""
import contextlib
import hashlib
import json
import os
import pickle
import re
import select
import traceback
import time
from pathlib import Path
from urllib.parse import urlsplit


BENCH = Path("/home/frappe/frappe-bench")


def _assert_candidate(request):
    import frappe
    from research_group.notifications import _settings

    prefix = request.get("prefix", "")
    if not re.fullmatch(r"rgah[0-9a-f]{10}", prefix):
        raise AssertionError("test_identity_required")
    config = json.loads((BENCH / "sites" / request["site"] / "site_config.json").read_bytes())
    expected = urlsplit(request["base"])
    actual = urlsplit(config.get("host_name", ""))
    database = str(config.get("db_name", ""))
    named = request.get("expected_db_name")
    database_matches = (bool(re.fullmatch(r"_[0-9a-f]{16}", named)) and database == named
                        if named is not None else database.startswith("rg_candidate_"))
    if (expected.scheme != "http" or expected.hostname not in {"localhost", "127.0.0.1"}
            or expected.port == 8088 or expected.path not in {"", "/"}
            or (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc)
            or not database_matches
            or config.get("rg_mail_mode") != "capture" or not config.get("mute_emails")
            or not config.get("allow_tests") or _settings()["mode"] != "capture"):
        raise AssertionError("isolated_capture_candidate_required")
    return prefix


def _address(value, prefix):
    from research_group.accounts import normalize_email

    address = normalize_email(value)
    if not address.startswith(prefix + "-"):
        raise AssertionError("fixture_outside_run_identity")
    return address


def _registration(email):
    import frappe

    rows = frappe.db.sql("SELECT name,status,token_hash,token_version,created_user,student_id "
                         "FROM `tabRG Registration` WHERE email=%s ORDER BY creation DESC,name DESC LIMIT 1",
                         (email,), as_dict=True)
    if not rows:
        return {"exists": False}
    row = rows[0]
    return {"exists": True, "name": row.name, "status": row.status,
            "token_present": bool(row.token_hash), "token_version": int(row.token_version or 0),
            "user_exists": bool(frappe.db.exists("User", email)),
            "has_created_user": bool(row.created_user)}


def _state(user):
    import frappe

    native = frappe.db.get_value("User", user, ["name", "email", "enabled"], as_dict=True)
    member = frappe.db.get_value("RG Member", {"user": user}, ["display_name", "active"], as_dict=True)
    profile = frappe.db.get_value("RG Account Profile", {"user": user},
                                 ["student_id", "contact_email", "contact_verified", "email_version",
                                  "phone", "bio", "pending_email", "pending_token_hash",
                                  "service_pause_audit"], as_dict=True)
    if not native or not member or not profile:
        raise AssertionError("run_account_not_created")
    return {"login_id": native.name, "native_email": native.email, "enabled": int(native.enabled),
            "active": int(member.active), "display_name": member.display_name,
            "student_id": profile.student_id, "contact_email": profile.contact_email,
            "contact_verified": bool(profile.contact_verified), "email_version": int(profile.email_version),
            "phone": profile.phone, "bio": profile.bio, "pending_email": profile.pending_email,
            "pending_token_present": bool(profile.pending_token_hash),
            "service_pause_present": bool(profile.service_pause_audit)}


def _capture(template, user, prefix):
    import frappe
    from frappe.utils.password import decrypt
    from research_group import notifications

    contracts = {
        "registration_verification": "RG Registration",
        "registration_approved": "User",
        "password_reset": "User",
        "contact_verification": "RG Account Profile",
    }
    if template not in contracts:
        raise AssertionError("capture_template_not_allowed")
    doctype = contracts[template]
    if doctype == "RG Registration":
        source = frappe.db.get_value(doctype, {"email": user, "status": ["!=", "expired"]}, "name")
    elif doctype == "RG Account Profile":
        source = frappe.db.get_value(doctype, {"user": user}, "name")
    else:
        source = user
    if not source:
        raise AssertionError("capture_source_missing")
    rows = frappe.db.sql("SELECT name,status FROM `tabRG Mail Event` WHERE template=%s "
                         "AND related_doctype=%s AND related_name=%s "
                         "ORDER BY creation DESC,name DESC LIMIT 1", (template, doctype, source), as_dict=True)
    if not rows:
        raise AssertionError("capture_event_missing")
    event_name = rows[0].name
    # Process exactly this test event; never drain any other person's queue.
    deadline = time.monotonic() + 5
    while True:
        notifications.process_event(event_name)
        frappe.db.commit()
        row = frappe.db.get_value("RG Mail Event", event_name,
                                  ["status", "body_ciphertext", "recipient_email"], as_dict=True)
        if row.status not in {"queued", "preparing"} or time.monotonic() >= deadline:
            break
        # A worker may already own this same event; wait for its capture commit
        # using a fresh snapshot, without touching another queue entry.
        frappe.db.rollback()
        time.sleep(0.1)
    if row.status != "captured":
        raise AssertionError("capture_not_complete")
    body = decrypt(row.body_ciphertext)
    links = re.findall(r"https?://[^\s]+", body)
    if len(links) != 1:
        raise AssertionError("one_captured_local_link_required")
    link = urlsplit(links[0])
    configured = urlsplit(frappe.conf.host_name)
    if ((link.scheme, link.netloc) != (configured.scheme, configured.netloc)
            or link.path != "/rg_join" or not link.fragment
            or "token=" in link.query or "key=" in link.query):
        raise AssertionError("capture_link_contract")
    _address(row.recipient_email, prefix)
    # Private response, held in parent memory only. The report never receives it.
    return {"event": event_name, "status": row.status, "link": links[0], "recipient": row.recipient_email}


def _connect_candidate(request):
    import frappe

    site = request.get("site", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", site):
        raise AssertionError("site_required")
    os.chdir(BENCH / "sites")
    frappe.init(site=site)
    prefix = _assert_candidate(request)
    frappe.connect()
    frappe.set_user("Administrator")
    return prefix


def _api_token_fixture(action, user, prefix):
    """Issue/revoke only this run's fresh identities, over the private pipe."""
    import frappe
    from research_group.ai.security import global_lock
    from frappe.utils.password import get_decrypted_password

    if user not in {prefix + "-student@m.fudan.edu.cn", prefix + "-native@fudan.edu.cn"}:
        raise AssertionError("api_token_fixture_identity_required")
    global_lock()
    native = frappe.get_doc("User", user, for_update=True)
    members = frappe.db.sql("SELECT name,active FROM `tabRG Member` WHERE user=%s FOR UPDATE",
                            (user,), as_dict=True)
    group = user.endswith("-student@m.fudan.edu.cn")
    if bool(members) != group:
        raise AssertionError("api_token_fixture_membership_required")
    marker_key = "rg-http-api-token:" + user
    marker = frappe.cache.get_value(marker_key, use_local_cache=False)
    if action == "issue_api_token":
        if marker or native.api_key or not native.enabled or (group and not members[0].active):
            raise AssertionError("fresh_active_api_token_fixture_required")
        # Use the pinned native producer, including its Password storage.
        # Test mode keeps native Contact synchronization inside this process.
        frappe.in_test = True
        from frappe.core.doctype.user.user import generate_keys
        credentials = generate_keys(user)
        marker = {"api_key": credentials["api_key"],
                  "secret_sha256": hashlib.sha256(credentials["api_secret"].encode()).hexdigest(),
                  "member_paused": False}
        frappe.cache.set_value(marker_key, marker, expires_in_sec=3600)
        frappe.db.commit()
        # These credentials never enter a receipt or process argument.
        return {**credentials, "enabled": True, "active": bool(group and members[0].active), "group": group}
    if action == "deactivate_token_member":
        if not group or not marker or not native.enabled or not members[0].active:
            raise AssertionError("active_owned_token_member_required")
        secret = get_decrypted_password("User", user, "api_secret", raise_exception=False) or ""
        same_credentials = native.api_key == marker["api_key"] and hashlib.sha256(secret.encode()).hexdigest() == marker["secret_sha256"]
        if not same_credentials:
            raise AssertionError("owned_api_credentials_changed")
        # Deliberately isolate the inbound activation guard: do not save User,
        # revoke its token, or invoke ordinary pause/session cleanup hooks.
        frappe.db.set_value("RG Member", members[0].name, "active", 0, update_modified=False)
        marker["member_paused"] = True
        frappe.cache.set_value(marker_key, marker, expires_in_sec=3600)
        frappe.db.commit()
        return {"active": False, "enabled": bool(native.enabled), "credentials_unchanged": same_credentials}
    if action != "revoke_api_token":
        raise AssertionError("api_token_action_not_allowed")
    if marker:
        if native.api_key != marker["api_key"]:
            raise AssertionError("cannot_revoke_unowned_api_token")
        frappe.db.set_value("User", user, "api_key", None, update_modified=False)
        frappe.db.sql("DELETE FROM `__Auth` WHERE doctype='User' AND name=%s AND fieldname='api_secret'", (user,))
        if marker["member_paused"]:
            frappe.db.set_value("RG Member", members[0].name, "active", 1, update_modified=False)
        frappe.clear_cache(user=user)
        frappe.db.commit()
        frappe.cache.delete_value(marker_key)
    remaining_secret = frappe.db.sql("SELECT 1 FROM `__Auth` WHERE doctype='User' AND name=%s "
                                    "AND fieldname='api_secret'", (user,))
    return {"cleaned": not frappe.db.get_value("User", user, "api_key") and not remaining_secret,
            "member_restored": not group or bool(frappe.db.get_value("RG Member", members[0].name, "active"))}


def dispatch(request):
    import frappe

    prefix = _connect_candidate(request)
    action = request.get("action")
    if action == "preflight":
        return {"isolated_candidate": True, "mail_mode": "capture", "native_mail_muted": True,
                "email_queue_count": frappe.db.count("Email Queue"),
                "native_email_link_enabled": bool(int(frappe.get_system_settings("login_with_email_link") or 0))}
    user = _address(request.get("user", ""), prefix)
    if action in {"issue_api_token", "deactivate_token_member", "revoke_api_token"}:
        return _api_token_fixture(action, user, prefix)
    if action == "prepare_native_account":
        if user != prefix + "-native@fudan.edu.cn" or frappe.db.exists("User", user):
            raise AssertionError("fresh_native_fixture_required")
        if not frappe.db.get_value("Role", "Website Manager", "desk_access"):
            raise AssertionError("native_fixture_requires_non_group_desk_role")
        doc = frappe.get_doc({"doctype": "User", "email": user, "first_name": "虚构非课题组账号",
            "enabled": 1, "user_type": "System User", "send_welcome_email": 0,
            "roles": [{"role": "Website Manager"}]})
        doc.flags.no_welcome_mail = True
        doc.insert(ignore_permissions=True)
        from frappe.utils.password import update_password
        update_password(user, request["password"], logout_all_sessions=False)
        frappe.db.commit()
        return {"created": True, "system_user": doc.user_type == "System User",
                "non_group": not frappe.db.exists("RG Member", {"user": user})
                and not frappe.db.exists("RG Account Profile", {"user": user})}
    if action in {"native_password_state", "issue_native_key"}:
        if (user != prefix + "-native@fudan.edu.cn" or not frappe.db.exists("User", user)
                or frappe.db.exists("RG Member", {"user": user})
                or frappe.db.exists("RG Account Profile", {"user": user})):
            raise AssertionError("native_fixture_boundary")
        link = frappe.get_doc("User", user)._reset_password(send_email=False) if action == "issue_native_key" else None
        if link:
            frappe.db.commit()
        data = frappe.db.get_value("User", user, ["reset_password_key", "last_reset_password_key_generated_on"], as_dict=True)
        result = {"non_group": True, "key_present": bool(data.reset_password_key),
                  "generated": bool(data.last_reset_password_key_generated_on), "key_digest": data.reset_password_key}
        if link:
            from urllib.parse import parse_qs
            result["key"] = parse_qs(urlsplit(link).query)["key"][0]
        # This entire result remains in the private pipe; receipt checks store
        # booleans only, never a key, digest, password or native session.
        return result
    if action == "prepare_legacy_login_keys":
        sid = request.get("sid", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", sid) or not frappe.db.sql(
                "SELECT sid FROM `tabSessions` WHERE sid=%s AND user=%s", (sid, user)):
            raise AssertionError("legacy_fixture_requires_run_account_session")
        key, token = frappe.generate_hash(), frappe.generate_hash()
        binding = {"user": user, "sid": sid, "login_token": token}
        frappe.cache.set_value("one_time_login_key:" + key, user, expires_in_sec=1800)
        frappe.cache.set_value("login_token:" + token, sid, expires_in_sec=1800)
        frappe.cache.set_value("rg-http-legacy:" + key, binding, expires_in_sec=1800)
        # Test-only cache entries, no call to send_login_link or sendmail.
        return {"prepared": True, "key": key, "login_token": token}
    if action in {"legacy_login_keys_state", "cleanup_legacy_login_keys"}:
        key, token, sid = (request.get(field, "") for field in ("key", "login_token", "sid"))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", value) for value in (key, token, sid)):
            raise AssertionError("legacy_fixture_identity_invalid")
        binding = frappe.cache.get_value("rg-http-legacy:" + key, use_local_cache=False)
        if binding != {"user": user, "sid": sid, "login_token": token}:
            raise AssertionError("legacy_fixture_binding_invalid")
        keys = ["one_time_login_key:" + key, "login_token:" + token, "rg-http-legacy:" + key]
        if action == "cleanup_legacy_login_keys":
            frappe.cache.delete_value(keys)
            return {"cleaned": all(frappe.cache.get_value(name, use_local_cache=False) is None for name in keys)}
        return {"email_key_preserved": frappe.cache.get_value(keys[0], use_local_cache=False) == user,
                "sid_token_preserved": frappe.cache.get_value(keys[1], use_local_cache=False) == sid}
    if action == "registration":
        return _registration(user)
    if action == "mark_auth_failure":
        template = request.get("template")
        if template not in {"registration_verification", "password_reset"}:
            raise AssertionError("auth_failure_template_boundary")
        doctype = "RG Registration" if template == "registration_verification" else "User"
        source = frappe.db.get_value("RG Registration", {"email": user}, "name") if doctype == "RG Registration" else user
        rows = frappe.db.sql("SELECT name,status,attempts FROM `tabRG Mail Event` WHERE name=%s AND template=%s "
            "AND related_doctype=%s AND related_name=%s FOR UPDATE",
            (request.get("event"), template, doctype, source), as_dict=True)
        if not rows or rows[0].status != "captured":
            raise AssertionError("captured_run_auth_fixture_required")
        # An explicit isolated fault fixture; never a production failure claim.
        frappe.db.set_value("RG Mail Event", rows[0].name, {"status": "failed", "error_class": "isolated_fixture_failure"})
        frappe.db.commit()
        return {"injected_known_failure": True}
    if action == "state":
        return _state(user)
    if action == "capture":
        return _capture(request.get("template"), user, prefix)
    if action == "external_lock":
        # Positive provenance counterexample through real native lifecycle.
        # Only the fresh account created in this single run can be changed.
        doc = frappe.get_doc("User", user)
        doc.enabled = 0
        doc.save(ignore_permissions=True)
        frappe.db.commit()
        return _state(user)
    if action == "finale":
        # No SMTP status is possible in this run; check all its related events.
        identities = frappe.db.sql("SELECT name FROM `tabUser` WHERE name LIKE %s",
                                  (prefix + "-%",), pluck=True)
        registrations = frappe.db.sql("SELECT name FROM `tabRG Registration` WHERE email LIKE %s",
                                      (prefix + "-%",), pluck=True)
        profiles = frappe.db.sql("SELECT name FROM `tabRG Account Profile` WHERE user LIKE %s",
                                 (prefix + "-%",), pluck=True)
        names = identities + registrations + profiles
        rows = frappe.get_all("RG Mail Event", filters={"related_name": ["in", names]},
                              fields=["name", "status"]) if names else []
        from research_group.notifications import process_event
        for row in rows:
            if row.status == "queued":
                process_event(row.name)
        frappe.db.commit()
        statuses = frappe.get_all("RG Mail Event", filters={"related_name": ["in", names]},
                                  pluck="status") if names else []
        native_user = prefix + "-native@fudan.edu.cn"
        native_queue = frappe.db.sql("SELECT q.name,q.status AS queue_status,r.status AS recipient_status "
            "FROM `tabEmail Queue` q JOIN `tabEmail Queue Recipient` r ON r.parent=q.name "
            "WHERE r.recipient=%s", (native_user,), as_dict=True)
        group_native_queue = frappe.db.sql("SELECT DISTINCT q.name FROM `tabEmail Queue` q "
            "JOIN `tabEmail Queue Recipient` r ON r.parent=q.name "
            "WHERE r.recipient LIKE %s AND r.recipient!=%s", (prefix + "-%", native_user))
        return {"email_queue_count": frappe.db.count("Email Queue"), "event_count": len(statuses),
                "smtp_outcomes": sum(value in {"sending", "submitted", "unknown"} for value in statuses),
                "native_email_queue_count": len({row.name for row in native_queue}),
                "native_email_sent_count": sum(row.queue_status == "Sent" or row.recipient_status == "Sent"
                                              for row in native_queue),
                "group_native_email_queue_count": len(group_native_queue)}
    raise AssertionError("unknown_fixture_action")


def _park_native_session(request):
    """Resume the real original sid; retain its native object across revocation."""
    import frappe
    from frappe.sessions import Session
    from werkzeug.wrappers import Request

    prefix = _connect_candidate(request)
    user = _address(request.get("user", ""), prefix)
    sid = request.get("sid", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", sid):
        raise AssertionError("native_session_sid_required")
    if not frappe.db.sql("SELECT sid FROM `tabSessions` WHERE sid=%s AND user=%s", (sid, user)):
        raise AssertionError("native_session_must_exist_before_hold")
    if not frappe.db.get_value("User", user, "enabled"):
        raise AssertionError("native_session_user_must_be_active")
    # Session.__init__ reads the normal cookie, resume validates the native
    # session and IP rules, and update later reads request_ip/lang. No public
    # HTTP endpoint, patched Session method, or handcrafted Session data is used.
    frappe.local.request = Request.from_values(
        path="/api/method/frappe.auth.get_logged_user", base_url=request["base"],
        headers={"Cookie": "sid=" + sid}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    frappe.local.request_ip = "127.0.0.1"
    frappe.local.form_dict = frappe._dict()
    session = Session(user=user, resume=True)
    frappe.local.session_obj = session
    if (type(session) is not Session or Session.update.__module__ != "frappe.sessions"
            or session.sid != sid or session.user != user or frappe.session.user != user):
        raise AssertionError("real_native_session_resume_required")
    # The live object survives this commit, just as native after-response
    # updates run after the HTTP transaction has committed. Do not hold locks
    # or a repeatable-read snapshot while the other connection changes password.
    frappe.db.commit()
    source_sha256 = hashlib.sha256(Path(frappe.sessions.__file__).read_bytes()).hexdigest()
    return session, user, sid, source_sha256


def _remote_session_cache(sid):
    """Read actual Redis, bypassing RedisWrapper's request-local hget cache."""
    import frappe
    import redis

    # Resume intentionally retains its original in-process cache and Session.
    # Using the base Redis reader with the native site-prefixed key measures
    # the other connection's deletion without altering that retained object.
    raw = redis.Redis.hget(frappe.cache, frappe.cache.make_key("session"), sid)
    return pickle.loads(raw) if raw is not None else None


def _native_refill(request, session, user, sid, phase):
    import frappe

    phase["name"] = "candidate_guard"
    _assert_candidate(request)
    phase["name"] = "sql_before"
    frappe.db.rollback()
    missing_before = not frappe.db.sql("SELECT sid FROM `tabSessions` WHERE sid=%s AND user=%s", (sid, user))
    if not missing_before:
        raise AssertionError("http_revocation_must_precede_native_update")
    phase["name"] = "redis_before"
    cache_absent_before = _remote_session_cache(sid) is None
    if not cache_absent_before:
        raise AssertionError("http_revocation_must_precede_native_update")
    # This is intentionally the fixed framework's real updater. It performs
    # its own UPDATE, metadata update, commit, and Redis write, including when
    # the SQL UPDATE affects zero rows. Never emulate it with cache.hset here.
    phase["name"] = "native_update"
    updated = session.update(force=True)
    phase["name"] = "native_commit"
    frappe.db.commit()
    phase["name"] = "redis_after"
    cached = _remote_session_cache(sid)
    cache_restored = bool(cached and cached.get("sid") == sid and cached.get("user") == user)
    phase["name"] = "sql_after"
    missing_after = not frappe.db.sql("SELECT sid FROM `tabSessions` WHERE sid=%s AND user=%s", (sid, user))
    frappe.db.commit()
    phase["name"] = "verify_result"
    if updated is not True or not cache_restored or not missing_after:
        raise AssertionError("native_update_must_reproduce_late_cache_write")
    return {"native_session_update_called": True, "native_update_returned_true": True,
            "sql_session_missing_before": missing_before, "cache_absent_before": cache_absent_before,
            "late_cache_restored": cache_restored, "sql_session_missing_after": missing_after}


def persistent_native_main(request, incoming, outgoing):
    """Two-way private pipe; ready/update/stop frames contain only booleans.

    Native Session and sid stay inside this one connected process. A bounded
    stdin wait terminates it even if the host verifier exits unexpectedly.
    """
    def emit(value):
        outgoing.write(json.dumps(value) + "\n")
        outgoing.flush()

    phase = {"name": "resume"}
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            session, user, sid, source_sha256 = _park_native_session(request)
            emit({"ok": True, "stage": "ready", "real_native_session_resumed": True,
                  "same_sid_retained": True, "database_snapshot_released": True,
                  "native_source_sha256": source_sha256})
            updates = 0
            while True:
                phase["name"] = "command_wait"
                readable, _, _ = select.select([incoming], [], [], 90)
                if not readable:
                    raise AssertionError("native_session_hold_timed_out")
                phase["name"] = "command_read"
                line = incoming.readline(4097)
                if not line:
                    return
                if len(line) > 4096:
                    raise AssertionError("native_session_command_too_long")
                command = json.loads(line)
                if command == {"command": "stop"}:
                    phase["name"] = "stop"
                    emit({"ok": True, "stage": "stopped", "updates": updates})
                    return
                if command != {"command": "update"} or updates >= 2:
                    raise AssertionError("native_session_command_not_allowed")
                proof = _native_refill(request, session, user, sid, phase)
                updates += 1
                emit({"ok": True, "stage": "updated", "iteration": updates, **proof})
    except BaseException as error:
        # No native exception text or frame locals can expose session data.
        emit({"ok": False, "stage": "failed", "phase": phase["name"], "error_type": type(error).__name__})
    finally:
        try:
            import frappe
            frappe.db.rollback()
            frappe.destroy()
        except Exception:
            pass


def private_main(request):
    """Return safe failure metadata; suppress all Frappe/logging console output."""
    result = None
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = {"ok": True, "result": dispatch(request)}
    except BaseException as error:
        result = {"ok": False, "error_type": type(error).__name__,
                  "frames": [{"file": Path(frame.filename).name, "line": frame.lineno,
                              "function": frame.name} for frame in traceback.extract_tb(error.__traceback__)]}
        if isinstance(error, AssertionError):
            # Every assertion in this helper is a constant label, never data.
            result["check"] = str(error)
    finally:
        try:
            import frappe
            frappe.db.rollback()
            frappe.destroy()
        except Exception:
            pass
    return result
