#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""FastAGI server for pbx-brain: handles out-of-dialog SIP MESSAGE routing.

Listens on 127.0.0.1:4573. The dialplan [pbx-msg] context calls
AGI(agi://127.0.0.1/msg-route,${EXTEN}) for each incoming MESSAGE.

AGI variables available for MESSAGE:
  agi_arg_1 = destination extension (from ${EXTEN})
  MESSAGE(from), MESSAGE(to), MESSAGE(body) are in the SIP packet;
  we get them via the dialplan passing them as args.
"""
import base64
import binascii
import re
import logging
import os
import socket
import sqlite3
import threading
import voipms_sms

DB = os.environ.get("PBX_DB", "/var/lib/pbx/pbx.db")
log = logging.getLogger("pbx-agi")

# Monthly message quota per extension (default 500, from logins.max_messages)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def text_quota(c, login_id):
    """Texts per month from the user's plan (mirrors pbx-api/entitlements.py).
    -1 = unlimited, 0 = none. Admin logins are unlimited."""
    role = c.execute("SELECT role FROM logins WHERE id=?", (login_id,)).fetchone()
    if role and role[0] == "admin":
        return -1
    try:
        r = c.execute("SELECT quota FROM user_entitlements WHERE login_id=? AND feature='messages'",
                      (login_id,)).fetchone()
        if r is not None:
            return int(r[0])
        d = c.execute("SELECT value FROM kv_settings WHERE key='default_quota_messages'").fetchone()
        return int(d[0]) if d and str(d[0]).strip() else 0
    except (sqlite3.OperationalError, ValueError):
        return 0


def _push_to_phone(to_exten, from_exten, body):
    """Ask pbx-brain (localhost) to deliver the text to the recipient's phone."""
    import json as _json
    import urllib.request
    try:
        req = urllib.request.Request(
            os.environ.get("PBX_BRAIN_STATUS", "http://127.0.0.1:8099") + "/messages/send",
            data=_json.dumps({"to": to_exten, "from": from_exten, "body": body}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # noqa: BLE001
        log.info("msg push to phone failed: %s", e)


def _arg(agi_vars, key):
    """AGI argument; "b64:"-prefixed values (from the dialplan) are decoded.
    Raw values are only accepted if they hold no control characters."""
    v = agi_vars.get(key, "")
    if v.startswith("b64:"):
        try:
            return base64.b64decode(v[4:], validate=True).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return ""
    return "" if any(ord(ch) < 32 for ch in v) else v


def handle_msg_route(agi_vars):
    """Route a SIP MESSAGE: check quota, store, deliver via MessageSend."""
    dest_exten = agi_vars.get("agi_arg_1", "")
    # The dialplan should pass: ${EXTEN}, ${MESSAGE(from)}, ${MESSAGE(body)}
    # For now, agi_arg_2 = from, agi_arg_3 = body (base64 or raw)
    from_uri = _arg(agi_vars, "agi_arg_2")
    body = _arg(agi_vars, "agi_arg_3")
    if not re.fullmatch(r"[0-9*#+]{1,32}", dest_exten or ""):
        log.warning("msg-route: bad destination %r", dest_exten[:40])
        return
    # Extract extension from from_uri (sip:8800@... -> 8800)
    from_exten = ""
    if "sip:" in from_uri:
        try:
            user = from_uri.split("sip:")[1].split("@")[0]
            # user is the SIP auth username (logins.sip_username, defaults to exten)
            with db() as c:
                row = c.execute("SELECT exten FROM logins WHERE sip_username=? OR exten=?",
                                (user, user)).fetchone()
                if row:
                    from_exten = row["exten"]
        except Exception:
            pass
    if not from_exten or not dest_exten or not body:
        log.warning("msg-route: missing from=%s dest=%s body_len=%d",
                    from_exten, dest_exten, len(body))
        return
    # External PSTN number? Route out via voip.ms instead of internal delivery.
    if voipms_sms.is_external_number(dest_exten):
        _handle_external_outbound(from_exten, dest_exten, body)
        return
    # Check quota: count messages from this exten this month
    with db() as c:
        sender = c.execute("SELECT * FROM logins WHERE exten=?",
                           (from_exten,)).fetchone()
        if not sender:
            log.warning("msg-route: unknown sender %s", from_exten)
            return
        # Check destination exists
        dest = c.execute("SELECT * FROM logins WHERE exten=? AND enabled=1",
                         (dest_exten,)).fetchone()
        if not dest:
            log.warning("msg-route: unknown dest %s", dest_exten)
            return
        # Sender turned sending off, recipient turned receiving off, or the
        # recipient blocked the sender: drop it (not stored, not charged).
        def _pref(login_id, col):
            try:
                r = c.execute(f"SELECT {col} FROM user_prefs WHERE login_id=?", (login_id,)).fetchone()
            except sqlite3.OperationalError:
                return 1
            return 1 if r is None else int(r[0])
        if not _pref(sender["id"], "msg_out"):
            log.info("msg-route: %s has sending turned off", from_exten)
            return
        if not _pref(dest["id"], "msg_in"):
            log.info("msg-route: %s isn't accepting texts", dest_exten)
            return
        try:
            if c.execute("SELECT 1 FROM blocked_numbers WHERE login_id=? AND number=?",
                         (dest["id"], from_exten)).fetchone():
                log.info("msg-route: %s blocked %s", dest_exten, from_exten)
                return
        except sqlite3.OperationalError:
            pass
        # Monthly limit: the plan's text allowance, and the per-login cap.
        count = c.execute(
            "SELECT COUNT(*) FROM messages WHERE login_id=?"
            " AND sent_at >= datetime('now', 'localtime', 'start of month', 'utc')",
            (sender["id"],)).fetchone()[0]
        plan = text_quota(c, sender["id"])
        if plan == 0 or (plan > 0 and count >= plan):
            log.warning("msg-route: %s has no texts left in plan (%d/%s)", from_exten, count, plan)
            return
        if count >= sender["max_messages"]:
            log.warning("msg-route: %s over quota (%d/%d)",
                        from_exten, count, sender["max_messages"])
            return
        # Store the message
        c.execute(
            "INSERT INTO messages (from_ext, to_ext, body, login_id)"
            " VALUES (?,?,?,?)",
            (from_exten, dest_exten, body[:1600], sender["id"]))
    log.info("msg: %s -> %s (%d chars)", from_exten, dest_exten, len(body))
    _push_to_phone(dest_exten, from_exten, body[:1600])
    # Deliver via MessageSend: we need to send a SIP MESSAGE to the dest.
    # Use asterisk CLI: pjsip send message? Actually, use MessageSend via
    # a Local channel or via ARI? Simplest: use `asterisk -rx`.
    # For now, log it; the recipient polls via API.
    # TODO: actually deliver via PJSIP MESSAGE using AMI or CLI.


def _send_external_background(api_username: str, api_password: str, sender_did: str,
                              dest_e164: str, body: str, from_exten: str):
    """Run the blocking voip.ms sendSMS in a daemon thread.

    Called by _handle_external_outbound so the AGI answers immediately (the
    message is already stored in the DB before this runs). A slow or failing
    voip.ms API must never hang the SIP MESSAGE response back to Zoiper.
    """
    try:
        resp = voipms_sms.send_sms_via_voipms(api_username, api_password,
                                             sender_did, dest_e164, body)
        if str(resp.get("status")) != "success":
            log.warning("voip.ms sendSMS failed (%s -> %s via %s): %s",
                        from_exten, dest_e164, sender_did, resp)
        else:
            log.info("voip.ms sendSMS ok (%s -> %s via %s)", from_exten, dest_e164, sender_did)
    except Exception as e:  # noqa: BLE001
        log.warning("voip.ms sendSMS error (%s -> %s via %s): %s",
                    from_exten, dest_e164, sender_did, e)


def _handle_external_outbound(from_exten: str, dest_number: str, body: str):
    """Send an SMS from an internal exten to an external PSTN number via voip.ms."""
    body = (body or "")[:1600]
    if not body.strip():
        return
    with db() as c:
        sender = c.execute("SELECT * FROM logins WHERE exten=? AND enabled=1", (from_exten,)).fetchone()
        if not sender:
            log.warning("msg-route external: unknown sender %s", from_exten)
            return
        try:
            r = c.execute("SELECT msg_out FROM user_prefs WHERE login_id=?", (sender["id"],)).fetchone()
            if r is not None and not int(r[0]):
                log.info("msg-route external: %s has sending turned off", from_exten)
                return
        except sqlite3.OperationalError:
            pass
        count = c.execute(
            "SELECT COUNT(*) FROM messages WHERE login_id=?"
            " AND sent_at >= datetime('now','localtime','start of month','utc')",
            (sender["id"],)).fetchone()[0]
        plan = text_quota(c, sender["id"])
        if plan == 0 or (plan > 0 and count >= plan):
            log.warning("msg-route external: %s out of plan quota", from_exten)
            return
        if count >= (sender["max_messages"] or 0):
            log.warning("msg-route external: %s over max_messages", from_exten)
            return
        cfg = voipms_sms.get_voipms_config(c)
        dest_e164 = voipms_sms.e164(dest_number)
        # Prefer the DID this conversation is already on (so replies go out
        # from the DID the contact texted), else the exten's mapped DID.
        sender_did = voipms_sms.lookup_conversation_did(c, from_exten, dest_e164)
        if not sender_did:
            sender_did = voipms_sms.lookup_sender_did(c, from_exten)
        if not sender_did:
            log.warning("msg-route external: no DID route for %s", from_exten)
            return
        if not cfg.get("voipms_api_username") or not cfg.get("voipms_api_password"):
            log.warning("msg-route external: voip.ms API not configured")
            return
        # Store first (so My Phone/ESP show it even if API fails)
        c.execute("INSERT INTO messages (from_ext, to_ext, body, login_id, via_did)"
                  " VALUES (?,?,?,?,?)",
                  (from_exten, dest_e164, body, sender["id"], sender_did))
        c.commit()
    log.info("msg external: %s -> %s via DID %s (%d chars)", from_exten, dest_e164, sender_did, len(body))
    # Send in the background so the AGI answers immediately. The message is
    # already stored above, so My Phone/ESP show it right away even if the
    # voip.ms API is slow (up to its 20s timeout) or fails.
    threading.Thread(target=_send_external_background,
                     args=(cfg["voipms_api_username"], cfg["voipms_api_password"],
                           sender_did, dest_e164, body, from_exten),
                     daemon=True, name="voipms-sendsms").start()
    log.info("msg external queued: %s -> %s via DID %s", from_exten, dest_e164, sender_did)


def agi_session(conn):
    """Handle one AGI connection."""
    try:
        # Read AGI environment (ends with blank line)
        agi_vars = {}
        f = conn.makefile("r")
        for line in f:
            line = line.strip()
            if not line:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                agi_vars.setdefault(k.strip(), v.strip())  # first wins: no overrides
        script = agi_vars.get("agi_network_script", "")
        log.info("AGI script: %s args=%s", script,
                 {k: v for k, v in agi_vars.items() if k.startswith("agi_arg")})
        if script == "msg-route":
            handle_msg_route(agi_vars)
        # Always answer OK
        conn.sendall(b"200 result=0\n")
    except Exception:
        log.exception("AGI session failed")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 4573))
    srv.listen(10)
    log.info("pbx-agi listening on 127.0.0.1:4573")
    while True:
        conn, _ = srv.accept()
        t = threading.Thread(target=agi_session, args=(conn,), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
