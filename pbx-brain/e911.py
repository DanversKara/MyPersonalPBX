# SPDX-License-Identifier: GPL-2.0-or-later
"""911 / E911 call handling for pbx-brain.

Rules (US 911 rules - Kari's Law / RAY BAUM's Act - take precedence over
any panel setting):
  * A 911 call is NEVER blocked: not by plans/quotas, DND, forwarding, the
    safety lock, the kill switch, or where the phone is connected from.
  * 911 works without any prefix; 9-911 (habit from old systems) is also
    treated as 911 unless the admin turns that off. 933 (address test line
    at many providers) is routed the same way.
  * Caller ID on the trunk decides the address the provider gives 911:
      - user has E911 on  -> their E911 number (address on file for them)
      - otherwise         -> the office fallback E911 number
      - neither set       -> the trunk's default caller ID (still connects)
  * Every 911 call is logged and the admin is alerted at once (Kari's Law
    notification), including whether the phone was connected from outside
    the office (its address on file may be wrong).

The panel side (codes, per-user E911 numbers/addresses) is pbx-api/e911_ui.py.
"""
import ipaddress
import logging
import threading
import time

log = logging.getLogger("pbx-brain.e911")

DEFAULT_NUMBERS = ("911", "933")


def _kv(q1, key, default=""):
    try:
        r = q1("SELECT value FROM kv_settings WHERE key=?", (key,))
        return r["value"] if r and r["value"] is not None else default
    except Exception:
        return default


def emergency_number(q1, dialed):
    """'911' / '933' if `dialed` is an emergency call, else None."""
    d = (dialed or "").strip()
    if d in DEFAULT_NUMBERS:
        return d
    if _kv(q1, "e911_allow_9prefix", "1") == "1" and d.startswith("9") and d[1:] in DEFAULT_NUMBERS:
        return d[1:]
    # With an outside-line prefix in use (Routes page), prefix + 911 is 911 too.
    pfx = _kv(q1, "outside_prefix", "9") or "9"
    if _kv(q1, "outside_prefix_mode", "off") in ("optional", "required") and d.startswith(pfx) \
            and d[len(pfx):] in DEFAULT_NUMBERS:
        return d[len(pfx):]
    return None


def is_external(channel):
    """True when the phone reached the PBX through the internet edge
    (Kamailio adds a Record-Route) or from a public address."""
    try:
        if channel.get_var("PJSIP_HEADER(read,Record-Route)"):
            return True
        addr = channel.get_var("CHANNEL(pjsip,remote_addr)")
        host = addr.rsplit(":", 1)[0].strip("[]") if addr else ""
        if host:
            return not ipaddress.ip_address(host).is_private
    except Exception:
        pass
    return False


def pick_trunk(q1):
    tid = _kv(q1, "e911_trunk_id")
    if tid.isdigit():
        t = q1("SELECT * FROM trunks WHERE id=? AND enabled=1", (int(tid),))
        if t:
            return t
    return q1("SELECT * FROM trunks WHERE enabled=1 ORDER BY id LIMIT 1")


def caller_identity(q1, me):
    """(caller_id_number, address, source) for a 911 call by login `me`."""
    if me is not None:
        r = q1("SELECT did, address FROM e911_users WHERE login_id=? AND enabled=1", (me["id"],))
        if r and r["did"]:
            return r["did"], r["address"], "user"
    did, addr = _kv(q1, "e911_fallback_did"), _kv(q1, "e911_fallback_address")
    if did:
        return did, addr, "office"
    if _kv(q1, "e911_use_provider", "1") == "1":
        return "", addr, "provider"
    return "", "", "none"


def alert(db, number, who, ext, external, source, did, address, note=""):
    """Log a security event and email the admin immediately (no throttle)."""
    where = "OUTSIDE the office network (via the edge)" if external else "the office network"
    src = {"user": "the user's E911 address", "office": "the office fallback address",
           "provider": "the provider's E911 address (trunk default caller ID)",
           "none": "NO E911 address set (trunk default caller ID)"}[source]
    detail = (f"{who} (ext {ext}) dialed {number} from {where}; sent with caller ID "
              f"{did or 'trunk default'} = {src}" + (f": {address}" if address else "") + (f". {note}" if note else ""))
    try:
        with db() as c:
            c.execute("INSERT INTO security_events (source, kind, ip, jail, detail, actor) VALUES (?,?,?,?,?,?)",
                      ("pbx", "emergency", "", "", detail[:400], str(ext)[:80]))
            c.commit()
    except Exception:
        log.exception("e911: event not stored")
    threading.Thread(target=_email, args=(db, number, detail), daemon=True).start()


def _email(db, number, detail):
    try:
        import mailer
        with db() as c:
            cfg = mailer.settings(c)
            r = c.execute("SELECT value FROM kv_settings WHERE key='sec_alert_to'").fetchone()
        to = (r[0] if r else "").strip()
        if not to or not mailer.configured(cfg):
            return
        mailer.send(cfg, to, f"EMERGENCY: {number} dialed on your phone system",
                    f"{detail}\n\nTime: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
                    "This alert is sent for every 911 call. Check on the caller if you can.\n")
    except Exception as e:  # noqa: BLE001
        log.warning("e911 alert email failed: %s", e)


def handle_emergency(channel, me, number, *, client, app, q1, db, calls, pending):
    """Connect a 911/933 call. Never refuses."""
    ext = me["exten"] if me is not None else (channel.json.get("caller", {}) or {}).get("number", "?")
    who = (me["display_name"] or me["username"]) if me is not None else "Unknown phone"
    external = is_external(channel)
    did, address, source = caller_identity(q1, me)
    trunk = pick_trunk(q1)
    log.warning("EMERGENCY %s from ext %s (%s, external=%s, cid=%s)", number, ext, source, external, did)
    if not trunk:
        alert(db, number, who, ext, external, source, did, address,
              note="CALL FAILED: no enabled trunk to send it to - add a trunk now")
        try:
            channel.answer()
            channel.play("sound:ss-noservice")
        except Exception:
            pass
        return
    alert(db, number, who, ext, external, source, did, address)
    caller_id = f"{who} <{did}>" if did else ""
    try:
        leg_b = client.channels.originate(
            endpoint="PJSIP/%s@ep-trunk-%s" % (number, trunk["name"]),
            app=app, app_args="legb,%s" % channel.id,
            caller_id=caller_id, timeout=120)
    except Exception:
        log.exception("EMERGENCY originate failed for %s", ext)
        alert(db, number, who, ext, external, source, did, address,
              note="CALL FAILED: the trunk refused the call")
        return
    pending[leg_b.id] = channel.id
    calls[channel.id] = {
        "caller": ext, "callee": number, "start": time.time(), "b_id": leg_b.id,
        "direction": "emergency", "login_id": me["id"] if me is not None else None,
        "trunk": trunk["name"], "hops": 99,
    }
    try:
        channel.ring()
    except Exception:
        pass
