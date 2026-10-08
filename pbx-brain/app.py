#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""pbx-brain: the ARI Stasis application that owns every call.

The Asterisk dialplan does nothing except hand calls to
Stasis(pbx-brain,...). All routing, voicemail, recording, spy,
conferences, DISA and CDR live here — this is the "API for extensions".

Phase 1: skeleton + ext-to-ext routing. Phases 2-4 fill in the TODOs.
"""
import logging
import os
import re
import sqlite3
import urllib.parse
import threading
import time
from http.server import BaseHTTPRequestHandler

from ari_client import ARIClient
import e911
import voipms_sms

ARI_URL = os.environ.get("ARI_URL", "http://127.0.0.1:8088/ari")
ARI_USER = os.environ.get("ARI_USER", "pbxbrain")
ARI_PASS = os.environ.get("ARI_PASS", "")
APP = "pbx-brain"
DB = os.environ.get("PBX_DB", "/var/lib/pbx/pbx.db")
REC_ADMIN_DIR = os.environ.get("PBX_REC_ADMIN_DIR", "/var/spool/pbx/monitor/admin")
REC_USER_DIR = os.environ.get("PBX_REC_USER_DIR", "/var/spool/pbx/monitor/user")
VM_DIR = os.environ.get("PBX_VM_DIR", "/var/spool/pbx/voicemail")

log = logging.getLogger("pbx-brain")
CLIENT = None


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def q1(sql, args=()):
    with db() as c:
        return c.execute(sql, args).fetchone()


def qall(sql, args=()):
    with db() as c:
        return c.execute(sql, args).fetchall()


def feature_allowed(code, login):
    """Can this login use a feature code (e.g. '*97', '*555')?

    Checks the global kill switch, per-user overrides, then the default
    access level. Falls back to historical behavior if the tables are missing.
    """
    try:
        f = q1("SELECT enabled, default_access FROM feature_codes WHERE code=?", (code,))
        if not f or not f["enabled"]:
            return False
        o = q1("SELECT allowed FROM login_feature_access WHERE login_id=? AND code=?",
               (login["id"], code))
        if o is not None:
            return bool(o["allowed"])
        da = (f["default_access"] or "all")
        if da == "admin":
            return (login.get("role") or "") == "admin"
        if da == "none":
            return False
        return True
    except Exception:
        # tables missing (migration not run yet): historical behavior
        if code == "*555":
            return (login.get("role") or "") == "admin"
        return True


# ---------------------------------------------------------------- routing
#
# Phase 1: extension -> extension via originate + mixing bridge.
# A calls: A's channel is already in Stasis. We originate a B leg into the
# same Stasis app; when B answers we bridge them. CDR is written on hangup.

PENDING = {}   # b_channel_id -> a_channel_id (B ringing, not yet bridged)
B_LOGIN = {}   # b_channel_id -> login id being rung (who answered -> minutes)
BRIDGED = {}   # channel_id -> bridge_id
CALLS = {}     # a_channel_id -> {caller, callee, start, b_id, ...}


def match_pattern(pattern, number):
    """Match a dial pattern against a number.
    Pattern chars: X=0-9, Z=1-9, N=2-9, .=one or more any, *=literal *.
    """
    import re
    # Convert pattern to regex
    rx = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "X":
            rx += "[0-9]"
        elif c == "Z":
            rx += "[1-9]"
        elif c == "N":
            rx += "[2-9]"
        elif c == ".":
            rx += ".+"
        elif c == "*":
            rx += r"\*"
        else:
            rx += re.escape(c)
        i += 1
    return re.fullmatch(rx, number) is not None


def handle_outbound(channel, me, dialed):
    """Route an outbound call via trunk per outbound_routes.
    Returns True if a route was found and the call was originated.
    """
    import json
    routes = qall("SELECT * FROM outbound_routes WHERE enabled=1"
                  " ORDER BY priority ASC")
    for route in routes:
        try:
            patterns = json.loads(route["patterns"] or "[]")
        except Exception:
            continue
        for pat in patterns:
            if match_pattern(pat, dialed):
                trunk = q1("SELECT * FROM trunks WHERE id=? AND enabled=1",
                           (route["trunk_id"],))
                if not trunk:
                    continue
                log.info("outbound %s -> %s via trunk %s (route %s)",
                         me["exten"], dialed, trunk["name"], route["name"])
                try:
                    # Originate via the trunk endpoint
                    leg_b = CLIENT.channels.originate(
                        endpoint="PJSIP/%s@ep-trunk-%s" % (dialed, trunk["name"]),
                        app=APP,
                        app_args="legb,%s" % channel.id,
                        caller_id="%s <%s>" % (me["display_name"] or me["exten"],
                                               me["exten"]),
                        timeout=30)
                except Exception:
                    log.exception("outbound originate failed")
                    return False
                PENDING[leg_b.id] = channel.id
                CALLS[channel.id] = {
                    "caller": me["exten"], "callee": dialed,
                    "start": time.time(), "b_id": leg_b.id,
                    "direction": "outbound", "login_id": me["id"],
                    "trunk": trunk["name"],
                }
                try:
                    channel.ring()
                except Exception:
                    pass
                return True
    return False


def handle_internal(channel, dialed):
    """Extension dialing: local extension, feature codes, etc."""
    caller_num = channel.json["caller"]["number"]
    me = q1("SELECT * FROM logins WHERE exten=? AND enabled=1",
            (caller_num,))
    # 911 first, before anything that could refuse a call (unknown phone,
    # plans, DND...). See e911.py: emergency calls are never blocked.
    number = e911.emergency_number(q1, dialed)
    if number:
        e911.handle_emergency(channel, me, number, client=CLIENT, app=APP, q1=q1, db=db,
                              calls=CALLS, pending=PENDING)
        return
    if not me:
        log.warning("call from unknown endpoint %s", caller_num)
        channel.hangup()
        return
    # Feature codes (gated by the Features admin page)
    if dialed == "*97":
        if not feature_allowed("*97", me):
            deny_and_hangup(channel, DENIED_PROMPT, "*97 not allowed")
            return
        handle_voicemail_check(channel)
        return
    if dialed == "*98":
        if not feature_allowed("*98", me):
            deny_and_hangup(channel, DENIED_PROMPT, "*98 not allowed")
            return
        handle_record_greeting(channel)
        return
    if dialed.startswith("*555"):
        # *555<exten>: monitor that exten; bare *555: scan any active call.
        # Stays on the line, auto-follows new calls, * / # hops between them.
        if not feature_allowed("*555", me):
            deny_and_hangup(channel, DENIED_PROMPT, "*555 not allowed")
            return
        handle_spy(channel, me, dialed[4:] or None)
        return
    if dialed.startswith("*60"):
        # *60: join conference room 60 (Phase 2e)
        if not feature_allowed("*60", me):
            deny_and_hangup(channel, DENIED_PROMPT, "*60 not allowed")
            return
        handle_conference(channel, me, "60")
        return
    if dialed == "*70":
        # *70: DISA - enter PIN, then dial (Phase 2f)
        if not feature_allowed("*70", me):
            deny_and_hangup(channel, DENIED_PROMPT, "*70 not allowed")
            return
        handle_disa(channel, me)
        return
    # IVR menus with an internal number (e.g. 7000) can be dialed directly.
    try:
        ivr = q1("SELECT * FROM ivr_menus WHERE exten=? AND exten!='' AND enabled=1",
                 (dialed,))
    except sqlite3.OperationalError:
        ivr = None
    if ivr and start_ivr(channel, ivr, {"caller": me["exten"], "me": me, "did": "",
                                        "caller_id": "%s <%s>" % (me["display_name"] or me["exten"], me["exten"])}):
        return
    # A ring group's internal number
    try:
        grp = q1("SELECT * FROM ring_groups WHERE exten=? AND enabled=1", (dialed,))
    except sqlite3.OperationalError:
        grp = None
    if grp:
        import json as _json
        members = [str(x) for x in _json.loads(grp["members"] or "[]") if str(x) != me["exten"]]
        if members:
            log.info("%s dialed ring group %s (%s)", me["exten"], grp["name"], dialed)
            ring_group(channel, members, grp["ring_seconds"] or 30, "", me["exten"],
                       "%s <%s>" % (me["display_name"] or me["exten"], me["exten"]),
                       group=dict(grp), internal=True)
            return
    # Check if it's a local extension first
    callee = q1("SELECT * FROM logins WHERE exten=? AND enabled=1",
                (dialed,))
    if callee and callee["username"] != me["username"]:
        # Local ext-to-ext: fall through to Phase 1 originate logic below
        pass
    elif callee:
        log.info("%s dialed self %s", caller_num, dialed)
        channel.hangup()
        return
    else:
        # Not an extension or feature code: try outbound routes (Phase 3a)
        # Outside-line prefix (admin setting on the Routes page):
        #   off      - numbers are dialed as-is (default)
        #   optional - "9" + number works, so does the number alone
        #   required - outside numbers must start with the prefix
        # The prefix is removed before the call goes to the trunk, so call
        # history and caller ID show the real number. 911 is handled earlier
        # and never needs a prefix.
        mode = (q1("SELECT value FROM kv_settings WHERE key='outside_prefix_mode'") or {"value": "off"})["value"] or "off"
        pfx = (q1("SELECT value FROM kv_settings WHERE key='outside_prefix'") or {"value": "9"})["value"] or "9"
        if mode in ("optional", "required"):
            if dialed.startswith(pfx) and len(dialed) > len(pfx) + 2:
                dialed = dialed[len(pfx):]
            elif mode == "required":
                log.info("%s dialed %s without the outside prefix %s", caller_num, dialed, pfx)
                deny_and_hangup(channel, why="outside prefix %s required" % pfx)
                return
        if not can_call_outside(me["id"]):
            deny_and_hangup(channel, why="%s has no outside minutes left" % me["exten"])
            return
        if handle_outbound(channel, me, dialed):
            return
        log.info("%s dialed unknown %s (no outbound route)", caller_num, dialed)
        channel.hangup()
        return
    ring_local(channel, me, callee)


# ---------------------------------------------------------------- user prefs
# DND / call forwarding, set by each user in the control panel (/ucp).

MAX_FORWARD_HOPS = 2   # A->B->C at most; stops forwarding loops


def get_prefs(login_id):
    """Call-handling prefs for a login. {} if none (or old DB w/o table)."""
    if not login_id:
        return {}
    try:
        r = q1("SELECT * FROM user_prefs WHERE login_id=?", (login_id,))
    except sqlite3.OperationalError:
        return {}
    return dict(r) if r else {}


RING_CHOICES = (10, 15, 20, 25, 30, 45, 60, 90, 120)
DEFAULT_RING = 30


def ring_seconds_for(login_id, default=DEFAULT_RING):
    """How long to ring this user before no-answer handling."""
    try:
        v = int(get_prefs(login_id).get("ring_seconds") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if 5 <= v <= 300 else default


def end_unanswered_leg(b_id):
    """Ring time is up (or the phone is gone): hang up the ringing B leg and
    run no-answer handling (forward / voicemail).

    An originated leg that was never answered never enters Stasis, so no
    StasisEnd arrives for it. Previously the timeout only hung B up and
    waited for a StasisEnd that never came: callers heard silence instead
    of voicemail. ChannelDestroyed (on_channel_destroyed) covers legs that
    end on their own (declined, unreachable); this covers our timeout."""
    try:
        CLIENT.channels.get(b_id).hangup()
    except Exception:
        pass
    with LEG_LOCK:
        if b_id in PENDING:
            hangup_other_leg(b_id)


LEG_LOCK = threading.RLock()


def forward_to(channel, me, callee, target, hops, **ring_kw):
    """Forward A's call (meant for `callee`) to `target`: a local extension
    or an outside number via the outbound routes. Returns True if handled.
    ring_kw (caller_label, caller_id, direction, extra) pass through to
    ring_local so a forwarded outside caller keeps their caller ID."""
    target = (target or "").strip()
    if not target or hops >= MAX_FORWARD_HOPS:
        return False
    tgt = q1("SELECT * FROM logins WHERE exten=? AND enabled=1", (target,))
    if tgt:
        if tgt["id"] in (me["id"], callee["id"]):
            log.info("forward %s -> %s ignored (loop)", callee["exten"], target)
            return False
        log.info("forward %s -> ext %s", callee["exten"], target)
        extra = dict(ring_kw.pop("extra", None) or {})
        if ring_kw.get("direction") == "inbound":
            extra.update(ring_ids=[tgt["id"]], vm_login_id=tgt["id"])
        ring_local(channel, me, tgt, hops + 1, extra=extra, **ring_kw)
        return True
    log.info("forward %s -> outside %s", callee["exten"], target)
    if not can_call_outside(callee["id"]):
        log.info("forward %s -> %s skipped: no outside minutes", callee["exten"], target)
        return False
    if handle_outbound(channel, me, target):
        CALLS[channel.id]["hops"] = MAX_FORWARD_HOPS  # no further forwarding
        CALLS[channel.id]["forwarded_from"] = callee["exten"]
        # The forwarding user pays for the outside leg.
        CALLS[channel.id]["login_id"] = callee["id"]
        CALLS[channel.id]["direction"] = "forwarded"
        if ring_kw.get("caller_label"):
            CALLS[channel.id]["caller"] = ring_kw["caller_label"]
        return True
    log.warning("forward %s -> %s failed: no outbound route",
                callee["exten"], target)
    return False


def num_norm(v):
    """Digits only; US/Canada numbers as 10 digits (for block-list matching)."""
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def blocked_by(login_id, number):
    """True if user `login_id` blocked calls/texts from `number`."""
    n = num_norm(number)
    if not login_id or not n:
        return False
    try:
        return q1("SELECT 1 FROM blocked_numbers WHERE login_id=? AND number=?", (login_id, n)) is not None
    except sqlite3.OperationalError:
        return False


def ring_local(channel, me, callee, hops=0, caller_label=None,
               caller_id=None, direction="internal", extra=None,
               from_ivr=False):
    """Ring a local extension for caller `me`, honouring the callee's DND
    and forward-always settings. No-answer handling (forward or voicemail)
    happens in hangup_other_leg once the ring times out.

    For a forwarded inbound call, `me` is the forwarding user and
    caller_label / caller_id carry the outside caller's number."""
    if blocked_by(callee["id"], caller_label or me["exten"]):
        log.info("%s blocked calls from %s", callee["exten"], caller_label or me["exten"])
        deny_and_hangup(channel, why="caller blocked by %s" % callee["exten"])
        return
    prev = CALLS.get(channel.id, {})
    info = {"caller": caller_label or me["exten"], "callee": callee["exten"],
            "start": prev.get("start", time.time()),
            "direction": direction,
            "login_id": None if direction == "inbound" else me["id"],
            "callee_id": callee["id"], "hops": hops}
    info.update(extra or {})
    # "Answer my calls with my IVR menu" (not when the call came from a menu,
    # e.g. the caller pressed the key that rings this user).
    if not from_ivr and callee["id"] != (me["id"] if direction != "inbound" else None):
        menu = answer_ivr_for(callee["id"])
        if menu:
            CALLS[channel.id] = info
            ctx = {"caller": info["caller"], "did": info.get("did", ""),
                   "me": me if direction != "inbound" else None,
                   "caller_id": caller_id or "%s <%s>" % (me["display_name"] or me["exten"], me["exten"])}
            if start_ivr(channel, menu, ctx):
                return
    if direction == "inbound" and not can_call_outside(callee["id"]):
        # Outside callers can only reach users whose plan includes outside calls.
        log.info("%s has no outside minutes: inbound call -> voicemail/unavailable", callee["exten"])
        CALLS[channel.id] = info
        route_to_voicemail(channel.id, callee["exten"])
        return
    prefs = get_prefs(callee["id"])
    if prefs.get("dnd"):
        log.info("%s is on DND: %s -> voicemail", callee["exten"], info["caller"])
        CALLS[channel.id] = info
        route_to_voicemail(channel.id, callee["exten"])
        return
    if forward_to(channel, me, callee, prefs.get("forward_always"), hops,
                  caller_label=caller_label, caller_id=caller_id,
                  direction=direction, extra=extra):
        return
    ring_secs = ring_seconds_for(callee["id"])
    try:
        leg_b = CLIENT.channels.originate(
            endpoint="PJSIP/%s" % callee["sip_username"],
            app=APP,
            app_args="legb,%s" % channel.id,
            caller_id=caller_id or "%s <%s>" % (me["display_name"] or me["exten"],
                                                me["exten"]),
            timeout=ring_secs + 5)  # our timer fires first -> voicemail
    except Exception:
        log.exception("originate to %s failed", callee["username"])
        channel.hangup()
        return
    PENDING[leg_b.id] = channel.id
    B_LOGIN[leg_b.id] = callee["id"]
    info["b_id"] = leg_b.id
    CALLS[channel.id] = info
    # Ring timeout: if B doesn't answer in time, end B -> forward/voicemail
    def _ring_timeout(b_id=leg_b.id):
        if b_id in PENDING:
            log.info("ring timeout for b leg %s", b_id)
            end_unanswered_leg(b_id)
    t = threading.Timer(float(ring_secs), _ring_timeout)
    t.daemon = True
    t.start()
    # Store timer so we can cancel it on answer
    CALLS[channel.id]["ring_timer"] = t
    try:
        channel.ring()  # 180 Ringing to A while B rings
    except Exception:
        log.exception("ring failed")
    log.info("ringing %s -> %s for %ss (b leg %s)",
             info["caller"], callee["exten"], ring_secs, leg_b.id)


def try_forward_noanswer(a_id, info):
    """Internal call not answered: forward if the callee set it up."""
    callee = q1("SELECT * FROM logins WHERE id=? AND enabled=1",
                (info.get("callee_id"),)) if info.get("callee_id") else None
    if not callee:
        return False
    target = get_prefs(callee["id"]).get("forward_noanswer")
    if not target:
        return False
    me = q1("SELECT * FROM logins WHERE id=?", (info.get("login_id"),))
    if not me:
        return False
    try:
        chan = CLIENT.channels.get(a_id)
    except Exception:
        return False
    return forward_to(chan, me, callee, target, info.get("hops", 0))


def try_forward_noanswer_inbound(a_id, info):
    """Inbound call not answered. Forward-on-no-answer only applies when the
    number rang a single user (a ring group falls through to voicemail)."""
    ring_ids = info.get("ring_ids") or []
    if len(ring_ids) != 1:
        return False
    callee = q1("SELECT * FROM logins WHERE id=? AND enabled=1", (ring_ids[0],))
    if not callee:
        return False
    target = get_prefs(callee["id"]).get("forward_noanswer")
    if not target:
        return False
    try:
        chan = CLIENT.channels.get(a_id)
    except Exception:
        return False
    caller = info.get("caller") or "PSTN"
    return forward_to(chan, callee, callee, target, info.get("hops", 0),
                      caller_label=caller,
                      caller_id="PSTN <%s>" % caller,
                      direction="inbound",
                      extra={"did": info.get("did", "")})


def on_legb_answer(b_channel):
    """B answered: hang up other B legs, bridge the winner with A."""
    a_id = PENDING.pop(b_channel.id, None)
    if not a_id or a_id not in CALLS:
        b_channel.hangup()
        return
    info = CALLS[a_id]
    # Cancel the ring timeout timer
    timer = info.pop("ring_timer", None)
    if timer:
        timer.cancel()
    # If this was a parallel ring (multiple B legs), hang up the losers
    for other_b_id in list(PENDING.keys()):
        if PENDING.get(other_b_id) == a_id and other_b_id != b_channel.id:
            del PENDING[other_b_id]
            try:
                CLIENT.channels.get(other_b_id).hangup()
            except Exception:
                pass
    try:
        CLIENT.channels.get(a_id).stop_ring()
    except Exception:
        pass
    try:
        # Answer A's leg first so Asterisk sends 200 OK to the caller.
        # (A may already be up, e.g. after an IVR; that's fine.)
        try:
            CLIENT.channels.get(a_id).answer()
        except Exception:
            log.info("answer A %s: already up", a_id)
        bridge = CLIENT.bridges.create(type="mixing")
        # Add one channel at a time: ARI only honors the last id
        # when several are passed in a single addChannel call.
        bridge.add_channel(a_id)
        bridge.add_channel(b_channel.id)
        # Start call recordings per admin/user flags (Phase 2b)
        start_call_recordings(bridge, a_id)
    except Exception:
        log.exception("bridge failed")
        b_channel.hangup()
        try:
            CLIENT.channels.get(a_id).hangup()
        except Exception:
            pass
        return
    BRIDGED[a_id] = bridge.id
    BRIDGED[b_channel.id] = bridge.id
    CALLS[a_id]["bridged_at"] = time.time()
    CALLS[a_id]["bridge_id"] = bridge.id
    # Record which B leg won (for CDR)
    CALLS[a_id]["b_id"] = b_channel.id
    CALLS[a_id]["answered_id"] = B_LOGIN.get(b_channel.id)
    log.info("call bridged: %s", bridge.id)


REC_RECORDINGS = {}  # rec_name -> {system, call_id, exten, login_id, ...}


def start_call_recordings(bridge, a_id):
    """Start MixMonitor-equivalent recordings per admin/user flags."""
    info = CALLS.get(a_id)
    if not info:
        return
    # Look up both extensions' recording flags
    caller = q1("SELECT * FROM logins WHERE exten=?", (info["caller"],))
    callee = q1("SELECT * FROM logins WHERE exten=?", (info["callee"],))
    systems = set()
    login_id = info.get("login_id")
    exten = info["caller"]
    # Admin recording needs the system-wide switch (Recordings page, turned on
    # only after the admin acknowledged recording laws) AND the extension's box.
    admin_on = (q1("SELECT value FROM kv_settings WHERE key='admin_rec_enabled'") or {"value": "0"})["value"] == "1"
    if admin_on and caller and caller["record_admin"]:
        systems.add("admin")
    if admin_on and callee and callee["record_admin"]:
        systems.add("admin")
    # "Record my calls" only with the feature in their plan AND the user's
    # acknowledgement of recording laws (two-party consent states etc.).
    for party in (caller, callee):
        if (party and party["user_record"] and has_feature(party["id"], "recording")
                and recording_consented(party["id"])):
            systems.add("user")
    if systems and (q1("SELECT value FROM kv_settings WHERE key='rec_announce'") or {"value": "0"})["value"] == "1":
        # "This call may be recorded": both parties hear it as the call starts.
        p = (q1("SELECT value FROM kv_settings WHERE key='rec_announce_path'") or {"value": ""})["value"]
        media = _media_for(p) if p and os.path.isfile(p) else "sound:beep"
        try:
            bridge.play(media)
        except Exception:
            log.exception("recording announcement failed")
    for system in systems:
        rec_dir = REC_ADMIN_DIR if system == "admin" else REC_USER_DIR
        import os
        os.makedirs(rec_dir, exist_ok=True)
        name = "rec-%s-%s-%d" % (system, a_id.replace(".", "-"),
                                 int(time.time()))
        try:
            rec = bridge.record(name=name, format="wav", max_duration=0)
            REC_RECORDINGS[name] = {
                "system": system,
                "call_id": a_id,
                "exten": exten,
                "login_id": login_id,
                "direction": info.get("direction", "internal"),
                "peer": info["callee"],
                "started": time.time(),
                "bridge_id": bridge.id,
            }
            log.info("started %s recording: %s", system, name)
        except Exception:
            log.exception("failed to start %s recording", system)


def get_voicemail_box(login_id):
    """Return the mailbox name for a login, creating it if needed."""
    if not login_id:
        return None
    row = q1("SELECT mailbox FROM voicemail_boxes WHERE login_id=?",
             (login_id,))
    if row:
        return row["mailbox"]
    # Create a sanitized mailbox name from the login username
    login = q1("SELECT username FROM logins WHERE id=?", (login_id,))
    if not login:
        return None
    mailbox = "".join(c for c in login["username"] if c.isalnum())[:32]
    if not mailbox:
        mailbox = "box%d" % login_id
    # Ensure uniqueness
    base = mailbox
    i = 1
    while q1("SELECT id FROM voicemail_boxes WHERE mailbox=?", (mailbox,)):
        i += 1
        mailbox = "%s%d" % (base, i)
    with db() as c:
        c.execute("INSERT INTO voicemail_boxes (login_id, mailbox)"
                  " VALUES (?,?)", (login_id, mailbox))
    return mailbox


def route_to_voicemail(a_channel_id, callee_exten):
    """B didn't answer: send A to the callee's voicemail box."""
    callee = q1("SELECT * FROM logins WHERE exten=? AND enabled=1",
                (callee_exten,))
    if not callee:
        try:
            CLIENT.channels.get(a_channel_id).hangup()
        except Exception:
            pass
        return
    mailbox = get_voicemail_box(callee["id"])
    if not mailbox:
        # No login/box: use fallback or hangup
        fb = q1("SELECT value FROM kv_settings WHERE key='fallback_mailbox'")
        if fb and fb["value"]:
            mailbox = fb["value"]
        else:
            try:
                CLIENT.channels.get(a_channel_id).hangup()
            except Exception:
                pass
            return
    try:
        a_chan = CLIENT.channels.get(a_channel_id)
        start_voicemail(a_chan, mailbox, CALLS.get(a_channel_id, {}).get("caller", ""))
    except Exception:
        log.exception("voicemail failed")
        try:
            CLIENT.channels.get(a_channel_id).hangup()
        except Exception:
            pass


VM_RECORDINGS = {}  # rec_name -> {mailbox, caller, channel_id, started}
SPY_SESSIONS = {}  # snoop_channel_id -> spy_channel_id
SPY_MONITORS = {}  # spy_channel_id -> persistent monitor session dict


def _spy_eligible_calls(target_exten):
    """Currently-bridged calls available for spying.

    Returns [(call_key, snoop_chan_id, label)] oldest-first. Target mode
    (target_exten set) lists only that extension's calls; scan mode (None)
    lists every bridged call on the system.
    """
    out = []
    for chan_id, info in list(CALLS.items()):
        if not info.get("bridged_at"):
            continue
        caller = info.get("caller") or ""
        callee = info.get("callee") or ""
        if target_exten:
            if callee == target_exten:
                snoop_id = info.get("b_id") or chan_id
            elif caller == target_exten:
                snoop_id = chan_id
            else:
                continue
        else:
            snoop_id = chan_id
        out.append((chan_id, snoop_id, "%s->%s" % (caller, callee),
                    info.get("bridged_at") or 0))
    out.sort(key=lambda t: t[3])
    return [(k, s, label) for k, s, label, _ in out]


def _spy_beep(spy_channel_id):
    try:
        CLIENT.channels.get(spy_channel_id).play("sound:beep")
    except Exception:
        pass


def _spy_detach(sess):
    """Tear down the current snoop/bridge; the session stays alive."""
    with sess["lock"]:
        snoop_id = sess.pop("snoop_id", None)
        bridge_id = sess.pop("bridge_id", None)
        sess["call_key"] = None
        sess["label"] = None
        sess["bridged_at"] = None
    if snoop_id:
        SPY_SESSIONS.pop(snoop_id, None)
        try:
            CLIENT.channels.get(snoop_id).hangup()
        except Exception:
            pass
    if bridge_id:
        try:
            CLIENT.bridges.get(bridge_id).destroy()
        except Exception:
            pass


def _spy_attach(sess, call_key, snoop_chan_id, label):
    """Attach the spy to a call, detaching any current snoop first."""
    _spy_detach(sess)
    spy_id = sess["spy_id"]
    try:
        snoop = CLIENT.channels.get(snoop_chan_id).snoop(
            app=APP, spy="both", whisper="none",
            app_args="spy,%s" % spy_id)
    except Exception:
        log.warning("spy: snoop failed for %s", label)
        return False
    with sess["lock"]:
        sess["snoop_id"] = snoop["id"]
        sess["call_key"] = call_key
        sess["label"] = label
        sess["bridged_at"] = time.time()
    SPY_SESSIONS[snoop["id"]] = spy_id
    log.info("spy: %s now listening to %s (snoop %s)",
             sess["spy_exten"], label, snoop["id"])
    _spy_beep(spy_id)
    return True


def _spy_monitor_loop(sess):
    """Background thread: keep the spy attached to a live call.

    When the current call ends, auto-attaches to the target's next call
    (target mode) or the next active call (scan mode); otherwise waits.
    """
    target = sess["target"]
    while not sess["stop"].is_set():
        try:
            with sess["lock"]:
                call_key = sess.get("call_key")
            alive = False
            if call_key:
                info = CALLS.get(call_key)
                alive = bool(info and info.get("bridged_at"))
            if not alive:
                if call_key:
                    _spy_detach(sess)
                eligible = _spy_eligible_calls(target)
                if eligible:
                    k, s, label = eligible[-1]  # most recent call
                    _spy_attach(sess, k, s, label)
        except Exception:
            log.exception("spy monitor error")
        sess["stop"].wait(2.0)


def _spy_stop_monitor(sess):
    sess["stop"].set()
    _spy_detach(sess)
    log.info("spy: monitor ended for %s", sess["spy_exten"])


def spy_dtmf(spy_channel_id, digit):
    """Hop between eligible calls: * / 1 = previous, # / 2 = next."""
    sess = SPY_MONITORS.get(spy_channel_id)
    if not sess or digit not in ("*", "#", "1", "2"):
        return
    direction = -1 if digit in ("*", "1") else 1
    eligible = _spy_eligible_calls(sess["target"])
    if not eligible:
        return
    with sess["lock"]:
        cur = sess.get("call_key")
    keys = [k for k, _, _ in eligible]
    try:
        idx = keys.index(cur)
    except ValueError:
        idx = -1 if direction > 0 else 0
    nxt = (idx + direction) % len(keys)
    k, s, label = eligible[nxt]
    if k == cur:
        return  # single call: already there
    log.info("spy: %s hopping (%s) to %s", sess["spy_exten"], digit, label)
    _spy_attach(sess, k, s, label)


def handle_spy(channel, spy_me, target_exten):
    """*555<exten> or *555: persistent ChanSpy monitor.

    Stays on the line instead of hanging up: attaches to the target's active
    call (or any active call when dialed bare), auto-follows new calls the
    moment they bridge, and hops between calls with * / # (or 1 / 2).
    Access is gated by feature_allowed() at the dispatch level (Features
    admin page); this function assumes the caller was already authorized.
    """
    target = (target_exten or "").strip().rstrip("#") or None
    try:
        channel.answer()
    except Exception:
        pass
    sess = {
        "spy_id": channel.id,
        "spy_exten": spy_me["exten"],
        "target": target,
        "lock": threading.Lock(),
        "stop": threading.Event(),
        "snoop_id": None,
        "bridge_id": None,
        "call_key": None,
        "label": None,
        "started_at": time.time(),
        "bridged_at": None,
    }
    SPY_MONITORS[channel.id] = sess
    eligible = _spy_eligible_calls(target)
    if eligible:
        k, s, label = eligible[-1]
        _spy_attach(sess, k, s, label)
    else:
        log.info("spy: %s waiting for %s", sess["spy_exten"],
                 "call on %s" % target if target else "any call")
    threading.Thread(target=_spy_monitor_loop, args=(sess,),
                     daemon=True, name="spy-monitor-%s" % channel.id).start()


def on_spy_stasis(snoop_channel, spy_channel_id):
    """Snoop channel entered Stasis: bridge it with the spy."""
    sess = SPY_MONITORS.get(spy_channel_id)
    current = False
    if sess is not None:
        with sess["lock"]:
            current = (sess.get("snoop_id") == snoop_channel.id)
    if not current:
        # Stale snoop (monitor hopped or session ended before Stasis): drop it.
        try:
            snoop_channel.hangup()
        except Exception:
            pass
        return
    try:
        bridge = CLIENT.bridges.create(type="mixing")
        bridge.add_channel(spy_channel_id)
        bridge.add_channel(snoop_channel.id)
        with sess["lock"]:
            if sess.get("snoop_id") == snoop_channel.id:
                sess["bridge_id"] = bridge.id
        log.info("spy bridged: %s", bridge.id)
    except Exception:
        log.exception("spy bridge failed")
        try:
            snoop_channel.hangup()
        except Exception:
            pass


CONFERENCES = {}  # room -> bridge_id
DISA_SESSIONS = {}  # channel_id -> {stage, digits, me}


def handle_disa(channel, me):
    """*70: DISA - PIN entry then dial tone for outbound/internal."""
    import bcrypt
    pin_hash_row = q1("SELECT value FROM kv_settings WHERE key='disa_pin_hash'")
    if not pin_hash_row or not pin_hash_row["value"]:
        log.info("DISA: no PIN configured")
        try:
            channel.hangup()
        except Exception:
            pass
        return
    try:
        channel.answer()
        # Play beep to prompt for PIN
        channel.play("tone:beep")
        DISA_SESSIONS[channel.id] = {
            "stage": "pin", "digits": "", "me": me,
            "pin_hash": pin_hash_row["value"],
        }
        log.info("DISA: %s prompted for PIN", me["exten"])
    except Exception:
        log.exception("DISA failed")
        try:
            channel.hangup()
        except Exception:
            pass


def on_dtmf(channel, event):
    """Handle DTMF for IVR menus, DISA PIN entry and dialing."""
    if channel.id in SPY_MONITORS:
        spy_dtmf(channel.id, event.get("digit", ""))
        return
    if channel.id in IVR_SESSIONS:
        ivr_dtmf(channel.id, event.get("digit", ""))
        return
    if channel.id in VM_WAIT:
        vm_skip_greeting(channel.id)
        return
    if channel.id in VMS:
        vms_dtmf(channel.id, event.get("digit", ""))
        return
    import bcrypt
    chan_id = channel.id
    sess = DISA_SESSIONS.get(chan_id)
    if not sess:
        return
    digit = event.get("digit", "")
    if sess["stage"] == "pin":
        if digit == "#":
            # Validate PIN
            pin = sess["digits"]
            if bcrypt.checkpw(pin.encode(), sess["pin_hash"].encode()):
                sess["stage"] = "dial"
                sess["digits"] = ""
                try:
                    channel.play("tone:dial")
                    log.info("DISA: PIN accepted for %s", sess["me"]["exten"])
                except Exception:
                    pass
            else:
                log.info("DISA: wrong PIN from %s", sess["me"]["exten"])
                try:
                    channel.hangup()
                except Exception:
                    pass
                DISA_SESSIONS.pop(chan_id, None)
        elif digit.isdigit():
            sess["digits"] += digit
            if len(sess["digits"]) > 12:
                try:
                    channel.hangup()
                except Exception:
                    pass
                DISA_SESSIONS.pop(chan_id, None)
    elif sess["stage"] == "dial":
        if digit == "#":
            # Dial the collected digits as an internal call
            dest = sess["digits"]
            me = sess["me"]
            DISA_SESSIONS.pop(chan_id, None)
            log.info("DISA: %s dialing %s", me["exten"], dest)
            # Reuse handle_internal logic by faking the dialed number
            # We need to route this channel as if they dialed dest
            handle_internal(channel, dest)
        elif digit.isdigit() or digit == "*":
            sess["digits"] += digit
            if len(sess["digits"]) > 15:
                try:
                    channel.hangup()
                except Exception:
                    pass
                DISA_SESSIONS.pop(chan_id, None)


def handle_conference(channel, me, room):
    """*60: join (or create) a conference room mixing bridge."""
    try:
        channel.answer()
        bridge_id = CONFERENCES.get(room)
        if bridge_id:
            # Verify the bridge still exists
            try:
                CLIENT.get("bridges/%s" % bridge_id)
            except Exception:
                bridge_id = None
        if not bridge_id:
            bridge = CLIENT.bridges.create(type="mixing")
            bridge_id = bridge.id
            CONFERENCES[room] = bridge_id
            log.info("conference room %s created: %s", room, bridge_id)
        else:
            bridge = CLIENT.bridges.get(bridge_id)
        bridge.add_channel(channel.id)
        log.info("%s joined conference %s", me["exten"], room)
        # Track for CDR
        CALLS[channel.id] = {"caller": me["exten"], "callee": "conf-%s" % room,
                             "start": time.time(), "direction": "conference",
                             "login_id": me["id"],
                             "bridged_at": time.time()}
        BRIDGED[channel.id] = bridge_id
    except Exception:
        log.exception("conference join failed")
        try:
            channel.hangup()
        except Exception:
            pass


def on_recording_finished(event):
    """Save a finished recording (voicemail or call) to the DB."""
    rec = event.get("recording", {})
    name = rec.get("name", "")
    # Check if it's a voicemail recording
    vm_info = VM_RECORDINGS.pop(name, None)
    if vm_info:
        save_voicemail_recording(name, vm_info)
        # Caller pressed # (or hit the limit) and is still on: confirm + hang up.
        try:
            ch = CLIENT.channels.get(vm_info["channel_id"])
            play_then(ch, "sound:vm-msgsaved,sound:vm-goodbye", lambda: _hangup(ch))
        except Exception:
            pass
        return
    g = GREET_RECORDINGS.pop(name, None)
    if g:
        save_greeting_recording(name, g)
        return
    # Check if it's a call recording
    rec_info = REC_RECORDINGS.pop(name, None)
    if rec_info:
        save_call_recording(name, rec_info)
        return


def save_voicemail_recording(name, info):
    """Move voicemail file and insert DB row."""
    path = "/var/spool/asterisk/recording/%s.wav" % name
    duration = int(time.time() - info["started"])
    import os, shutil
    os.makedirs(VM_DIR, exist_ok=True)
    dest = os.path.join(VM_DIR, "%s.wav" % name)
    try:
        shutil.move(path, dest)
    except Exception:
        log.exception("voicemail move failed")
        dest = path
    with db() as c:
        c.execute(
            "INSERT INTO voicemail_messages (mailbox, caller, duration_sec,"
            " folder, path) VALUES (?,?,?,?,?)",
            (info["mailbox"], info["caller"], duration, "INBOX", dest))
    log.info("voicemail saved: %s from %s (%ds)",
             info["mailbox"], info["caller"], duration)
    # Ring group "every member gets a copy": one file per box, so each member
    # can delete their own copy.
    for mb in VM_COPY.pop(info.get("channel_id"), []) or []:
        if mb == info["mailbox"]:
            continue
        try:
            cp = os.path.join(VM_DIR, "%s-%s.wav" % (name, mb))
            shutil.copy2(dest, cp)
            with db() as c:
                c.execute("INSERT INTO voicemail_messages (mailbox, caller, duration_sec, folder, path)"
                          " VALUES (?,?,?,?,?)", (mb, info["caller"], duration, "INBOX", cp))
            threading.Thread(target=email_voicemail, args=(mb, info["caller"], duration, cp),
                             daemon=True, name="vm-email").start()
            log.info("voicemail copy for %s", mb)
        except Exception:
            log.exception("voicemail copy to %s failed", mb)
    # Email the mailbox owner in the background (never block the event thread).
    threading.Thread(target=email_voicemail, args=(info["mailbox"], info["caller"], duration, dest),
                     daemon=True, name="vm-email").start()
    # TODO: MWI NOTIFY to the extension


def email_voicemail(mailbox, caller, duration, path):
    """Send 'new voicemail' to the owner's vm_email if SMTP is set up."""
    import mailer
    try:
        with db() as c:
            owner = c.execute("SELECT l.* FROM voicemail_boxes b JOIN logins l ON l.id=b.login_id"
                              " WHERE b.mailbox=?", (mailbox,)).fetchone()
            cfg = mailer.settings(c)
    except Exception:
        log.exception("voicemail email: lookup failed")
        return
    if not owner or not owner["vm_email"] or not mailer.configured(cfg):
        return
    when = time.strftime("%a %b %d, %Y at %I:%M %p")
    secs = int(_wav_seconds(path)) if os.path.isfile(path) else duration
    who = mailer.clean(caller) or "Unknown caller"
    link = (cfg.get("panel_url") or "").rstrip("/")
    text = (f"Hi {owner['display_name'] or owner['username']},\n\n"
            f"You have a new voicemail on extension {owner['exten']}.\n\n"
            f"From:     {who}\n"
            f"Received: {when}\n"
            f"Length:   {secs // 60}:{secs % 60:02d}\n\n"
            + (f"Listen online: {link}/ucp/voicemail\n" if link else "")
            + "Or dial *97 from your phone.\n")
    attach = None
    if cfg.get("vm_email_attach") == "1" and os.path.isfile(path):
        try:
            if os.path.getsize(path) <= mailer.MAX_ATTACH:
                with open(path, "rb") as f:
                    attach = ("voicemail-%s.wav" % time.strftime("%Y%m%d-%H%M"), f.read(), "audio/wav")
        except OSError:
            pass
    stamp = time.strftime("%Y-%m-%d %H:%M")
    try:
        mailer.send(cfg, owner["vm_email"], f"New voicemail from {who} ({secs // 60}:{secs % 60:02d})",
                    text, attachment=attach)
        result = f"{stamp}: voicemail for {owner['exten']} sent to {owner['vm_email']}"
        log.info("voicemail email sent to %s", owner["vm_email"])
    except mailer.MailError as e:
        result = f"{stamp}: voicemail for {owner['exten']} to {owner['vm_email']} FAILED: {e}"
        log.warning("voicemail email failed: %s", e)
    try:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('smtp_last_result', ?)", (result,))
    except Exception:
        pass


def save_call_recording(name, info):
    """Move call recording file and insert DB row."""
    # ARI bridge recordings go to /var/spool/asterisk/recording/
    src = "/var/spool/asterisk/recording/%s.wav" % name
    duration = int(time.time() - info["started"])
    import os, shutil
    rec_dir = REC_ADMIN_DIR if info["system"] == "admin" else REC_USER_DIR
    os.makedirs(rec_dir, exist_ok=True)
    dest = os.path.join(rec_dir, "%s.wav" % name)
    try:
        shutil.move(src, dest)
        size = os.path.getsize(dest)
    except Exception:
        log.exception("recording move failed")
        # Fall back to the spool path; the file is still readable there.
        dest = src
        try:
            size = os.path.getsize(src)
        except OSError:
            size = 0
    with db() as c:
        cur = c.execute(
            "INSERT INTO recordings (call_id, system, exten, login_id,"
            " direction, peer, duration_sec, bytes, path)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (info["call_id"], info["system"], info["exten"],
             info["login_id"], info["direction"], info["peer"],
             duration, size, dest))
        rid = cur.lastrowid
        # Link to CDR if it exists
        c.execute("UPDATE cdr SET recording_id=? WHERE call_id=?",
                  (rid, info["call_id"]))
    log.info("%s recording saved: %s (%ds, %d bytes)",
             info["system"], dest, duration, size)


# ---------------------------------------------------------------- voicemail
# Leaving a message: greeting (the box's own, else Asterisk's "please leave
# your message after the tone"), beep, record until # / hang up / 120 s.
# Any key during the greeting skips it.
# *97: hear your messages (7 delete, # next, 0 record your greeting).
# *98: record your voicemail greeting straight away.
# Everything is event driven (PlaybackFinished / RecordingFinished): nothing
# blocks the event thread, so other calls are never held up.

VMGREET_DIR = os.environ.get("PBX_VMGREET_DIR", "/var/lib/pbx/vmgreet")
VM_DEFAULT_GREETING = "sound:vm-intro"   # "Please leave your message after the tone..."
VM_MAX_SEC = 120
GREETING_MAX_SEC = 60
PB_CALLBACKS = {}     # playback_id -> fn() when it finishes
VM_WAIT = {}          # channel_id -> {"pb", "begin", "timer"} greeting before a message
VM_COPY = {}          # channel_id -> [extra mailboxes] (ring group "every member gets a copy")
VMS = {}              # channel_id -> *97 session
GREET_RECORDINGS = {} # recording name -> {"mailbox", "channel_id"}
VM_LOCK = threading.RLock()


def play_then(chan, media, fn):
    """Play media on chan; call fn() when it finishes. Returns playback id."""
    try:
        pb = chan.play(media)
        pid = (pb or {}).get("id")
    except Exception:
        log.info("play %s on %s failed", media, getattr(chan, "id", "?"))
        pid = None
    if pid:
        PB_CALLBACKS[pid] = fn
        return pid
    fn()
    return None


def stop_playback(pid):
    """Stop a playback without firing its callback."""
    if not pid:
        return
    PB_CALLBACKS.pop(pid, None)
    try:
        CLIENT.delete("playbacks/%s" % pid)
    except Exception:
        pass


def _media_for(path):
    return "sound:" + os.path.splitext(path)[0]


def greeting_media(mailbox):
    r = q1("SELECT greeting_path FROM voicemail_boxes WHERE mailbox=?", (mailbox,))
    p = r["greeting_path"] if r else ""
    if p and os.path.isfile(p):
        return _media_for(p), _wav_seconds(p)
    return VM_DEFAULT_GREETING, 6


def _wav_seconds(path):
    import wave
    try:
        with wave.open(path) as w:
            return w.getnframes() / float(w.getframerate() or 8000)
    except Exception:
        return 30


def _hangup(chan_or_id):
    try:
        (CLIENT.channels.get(chan_or_id) if isinstance(chan_or_id, str) else chan_or_id).hangup()
    except Exception:
        pass


def start_voicemail(chan, mailbox, caller):
    """Greeting, then record a message into mailbox."""
    owner = q1("SELECT login_id FROM voicemail_boxes WHERE mailbox=?", (mailbox,))
    if owner and not has_feature(owner["login_id"], "voicemail"):
        # Voicemail isn't in this user's plan: tell the caller, hang up.
        deny_and_hangup(chan, UNAVAILABLE_PROMPT, "voicemail not in plan for %s" % mailbox)
        return
    try:
        chan.answer()
    except Exception:
        pass
    media, secs = greeting_media(mailbox)

    def begin():
        with VM_LOCK:
            w = VM_WAIT.pop(chan.id, None)
            if w and w.get("timer"):
                w["timer"].cancel()
        try:
            rec = chan.record(name="vm-%s-%d" % (mailbox, int(time.time() * 1000)),
                              format="wav", max_duration=VM_MAX_SEC,
                              beep=True, terminate_on="#")
        except Exception:
            log.exception("voicemail record failed")
            _hangup(chan)
            return
        VM_RECORDINGS[rec["name"]] = {"mailbox": mailbox, "caller": caller,
                                      "channel_id": chan.id, "started": time.time()}
        log.info("voicemail recording started for %s from %s", mailbox, caller)

    with VM_LOCK:
        pid = play_then(chan, media, begin)
        if pid:
            # Safety net if PlaybackFinished never arrives.
            t = threading.Timer(secs + 4, lambda: _vm_wait_timeout(chan.id, pid))
            t.daemon = True
            VM_WAIT[chan.id] = {"pb": pid, "begin": begin, "timer": t}
            t.start()


def _vm_wait_timeout(chan_id, pid):
    with VM_LOCK:
        w = VM_WAIT.get(chan_id)
        if not w or w["pb"] != pid:
            return
    stop_playback(pid)
    w["begin"]()


def vm_skip_greeting(chan_id):
    with VM_LOCK:
        w = VM_WAIT.get(chan_id)
        if not w:
            return False
    stop_playback(w["pb"])
    w["begin"]()
    return True


def handle_voicemail_check(channel):
    """*97: hear your messages."""
    me = q1("SELECT * FROM logins WHERE exten=? AND enabled=1",
            (channel.json["caller"]["number"],))
    mailbox = get_voicemail_box(me["id"]) if me else None
    if not mailbox:
        channel.hangup()
        return
    msgs = [dict(m) for m in qall("SELECT * FROM voicemail_messages WHERE mailbox=?"
                                  " AND folder='INBOX' ORDER BY id", (mailbox,))]
    try:
        channel.answer()
    except Exception:
        pass
    sess = {"chan": channel, "me": dict(me), "mailbox": mailbox,
            "queue": msgs, "cur": None, "pb": None}
    VMS[channel.id] = sess
    n = len(msgs)
    if n:
        intro = "sound:vm-youhave,number:%d,sound:vm-INBOX,sound:%s" % (
            n, "vm-message" if n == 1 else "vm-messages")
    else:
        intro = "sound:vm-youhave,sound:vm-no,sound:vm-messages"
    log.info("*97: %s has %d new message(s)", mailbox, n)
    sess["pb"] = play_then(channel, intro, lambda: _vms_next(sess))


def _vms_next(sess):
    if VMS.get(sess["chan"].id) is not sess:
        return
    if sess["queue"]:
        m = sess["queue"].pop(0)
        sess["cur"] = m
        sess["pb"] = play_then(sess["chan"], _media_for(m["path"]), lambda: _vms_heard(sess))
    else:
        sess["cur"] = None
        sess["pb"] = play_then(sess["chan"], "sound:vm-goodbye", lambda: _vms_end(sess))


def _vms_heard(sess):
    m = sess.get("cur")
    if m:
        with db() as c:
            c.execute("UPDATE voicemail_messages SET folder='Old', is_read=1 WHERE id=?", (m["id"],))
    _vms_next(sess)


def _vms_end(sess):
    VMS.pop(sess["chan"].id, None)
    _hangup(sess["chan"])


def vms_dtmf(chan_id, digit):
    sess = VMS.get(chan_id)
    if not sess:
        return
    if sess.get("recording"):
        return            # '#' ends the greeting recording (terminate_on)
    m = sess.get("cur")
    if digit == "0":
        if not has_feature(sess["me"]["id"], "voicemail"):
            return
        stop_playback(sess["pb"])
        record_greeting(sess["chan"], sess["mailbox"], sess)
    elif digit == "7" and m:
        stop_playback(sess["pb"])
        with db() as c:
            c.execute("DELETE FROM voicemail_messages WHERE id=?", (m["id"],))
        try:
            if os.path.commonpath([os.path.abspath(m["path"]), VM_DIR]) == VM_DIR:
                os.remove(m["path"])
        except Exception:
            pass
        sess["cur"] = None
        log.info("*97: %s deleted message %s", sess["mailbox"], m["id"])
        sess["pb"] = play_then(sess["chan"], "sound:vm-deleted", lambda: _vms_next(sess))
    elif digit in ("#", "9") and m:
        stop_playback(sess["pb"])
        _vms_heard(sess)


def handle_record_greeting(channel):
    """*98: record your voicemail greeting."""
    me = q1("SELECT * FROM logins WHERE exten=? AND enabled=1",
            (channel.json["caller"]["number"],))
    if me and not has_feature(me["id"], "voicemail"):
        deny_and_hangup(channel, why="voicemail not in plan for %s" % me["exten"])
        return
    mailbox = get_voicemail_box(me["id"]) if me else None
    if not mailbox:
        channel.hangup()
        return
    try:
        channel.answer()
    except Exception:
        pass
    sess = {"chan": channel, "me": dict(me), "mailbox": mailbox, "queue": [], "cur": None, "pb": None}
    VMS[channel.id] = sess
    record_greeting(channel, mailbox, sess)


def record_greeting(chan, mailbox, sess):
    """'After the tone, say your message, then press #' -> beep -> record."""
    sess["queue"], sess["cur"] = [], None

    def go():
        name = "vmgreet-%s-%d" % (mailbox, int(time.time() * 1000))
        try:
            chan.record(name=name, format="wav", max_duration=GREETING_MAX_SEC,
                        beep=True, terminate_on="#")
        except Exception:
            log.exception("greeting record failed")
            _vms_end(sess)
            return
        sess["recording"] = name
        GREET_RECORDINGS[name] = {"mailbox": mailbox, "channel_id": chan.id, "started": time.time()}
        log.info("recording voicemail greeting for %s", mailbox)
    sess["pb"] = play_then(chan, "sound:vm-rec-unv", go)


def save_greeting_recording(name, info):
    """Recorded greeting finished: keep it as the box's greeting."""
    import shutil
    src = "/var/spool/asterisk/recording/%s.wav" % name
    chan_id = info["channel_id"]
    sess = VMS.get(chan_id)
    if not os.path.isfile(src) or _wav_seconds(src) < 1.0:
        log.info("greeting for %s too short/missing, not saved", info["mailbox"])
        try:
            os.remove(src)
        except OSError:
            pass
        if sess:
            sess.pop("recording", None)
            sess["pb"] = play_then(sess["chan"], "sound:vm-goodbye", lambda: _vms_end(sess))
        return
    os.makedirs(VMGREET_DIR, exist_ok=True)
    dest = os.path.join(VMGREET_DIR, "%s.wav" % name)
    try:
        shutil.move(src, dest)
        os.chmod(dest, 0o644)
    except Exception:
        log.exception("greeting move failed")
        dest = src
    old = q1("SELECT greeting_path FROM voicemail_boxes WHERE mailbox=?", (info["mailbox"],))
    with db() as c:
        c.execute("UPDATE voicemail_boxes SET greeting_path=? WHERE mailbox=?", (dest, info["mailbox"]))
    if old and old["greeting_path"] and old["greeting_path"] != dest:
        try:
            if os.path.commonpath([os.path.abspath(old["greeting_path"]), VMGREET_DIR]) == VMGREET_DIR:
                os.remove(old["greeting_path"])
        except Exception:
            pass
    log.info("voicemail greeting saved for %s: %s", info["mailbox"], dest)
    if sess:
        sess.pop("recording", None)
        # Play it back, confirm, hang up.
        sess["pb"] = play_then(sess["chan"], _media_for(dest) + ",sound:vm-msgsaved",
                               lambda: _vms_end(sess))


def vm_cleanup(chan_id):
    with VM_LOCK:
        w = VM_WAIT.pop(chan_id, None)
    if w:
        if w.get("timer"):
            w["timer"].cancel()
        PB_CALLBACKS.pop(w["pb"], None)
    sess = VMS.pop(chan_id, None)
    if sess:
        PB_CALLBACKS.pop(sess.get("pb"), None)


def hangup_other_leg(ended_id):
    """One leg ended: clean up the other side."""
    # Bridged (active) call: hang up the other leg(s) in the same bridge
    bridge_id = BRIDGED.pop(ended_id, None)
    if bridge_id:
        for chan_id, bid in list(BRIDGED.items()):
            if bid == bridge_id:
                del BRIDGED[chan_id]
                try:
                    CLIENT.channels.get(chan_id).hangup()
                except Exception:
                    pass
        try:
            CLIENT.bridges.get(bridge_id).destroy()
        except Exception:
            pass
        return
    # A hung up while B(s) were ringing: hang up all B legs
    for b_id, a_id in list(PENDING.items()):
        if a_id == ended_id:
            del PENDING[b_id]
            try:
                CLIENT.channels.get(b_id).hangup()
            except Exception:
                pass
    if ended_id in PENDING:
        # A B leg ended while ringing
        a_id = PENDING.pop(ended_id)
        info = CALLS.get(a_id)
        if not info:
            return
        # Check if there are other B legs still ringing for this A
        remaining = [b for b, a in PENDING.items() if a == a_id]
        if remaining:
            # Others still ringing, wait for them
            log.info("B leg %s ended, %d still ringing for %s",
                     ended_id, len(remaining), a_id)
            return
        # All B legs done (no answer/timeout)
        if info.get("direction") in ("inbound", "group"):
            # Inbound no-answer: the single rung user's forward-on-no-answer,
            # else the first ring extension's voicemail box. (Previously this
            # checked a "fallback_mailbox" key that was never set, so inbound
            # callers were hung up instead of reaching voicemail.)
            if not info.get("group") and try_forward_noanswer_inbound(a_id, info):
                return
            if info.get("group_row"):
                try:
                    chan = CLIENT.channels.get(a_id)
                    first = q1("SELECT * FROM logins WHERE id=?", (info.get("first_login_id"),)) \
                        if info.get("first_login_id") else None
                    vml = q1("SELECT * FROM logins WHERE id=?", (info.get("vm_login_id"),)) \
                        if info.get("vm_login_id") else None
                    _group_fallback(chan, info["group_row"], first, vml, info.get("vm_copies") or [],
                                    info.get("did", ""), info.get("caller", "PSTN"),
                                    info.get("caller_id"), info.get("internal"), info.get("group_hops", 0))
                except Exception:
                    log.exception("ring group no-answer handling failed")
                return
            if info.get("vm_copies"):
                VM_COPY[a_id] = list(info["vm_copies"])
            mailbox = info.get("fallback_mailbox") or \
                get_voicemail_box(info.get("vm_login_id"))
            if mailbox:
                log.info("inbound no-answer, routing to voicemail %s", mailbox)
                route_inbound_to_voicemail_by_id(a_id, mailbox,
                                                 info.get("did", ""),
                                                 info.get("caller", "PSTN"))
            else:
                try:
                    CLIENT.channels.get(a_id).hangup()
                except Exception:
                    pass
        elif info.get("direction") == "internal":
            # Internal no-answer -> callee's forward-on-no-answer, else voicemail
            if try_forward_noanswer(a_id, info):
                return
            log.info("no answer from %s, routing %s to voicemail",
                     info["callee"], info["caller"])
            route_to_voicemail(a_id, info["callee"])
        else:
            try:
                CLIENT.channels.get(a_id).hangup()
            except Exception:
                pass


def route_inbound_to_voicemail_by_id(a_id, mailbox, did, caller="PSTN"):
    """Route an inbound A leg to voicemail by channel ID."""
    # Clear the CALLS entry so write_cdr doesn't double-count;
    # route_inbound_to_voicemail will create a new one
    CALLS.pop(a_id, None)
    try:
        chan = CLIENT.channels.get(a_id)
        # We need the channel object; use the low-level API
        # For now, hang up and let the caller redial? No - do it properly:
        # Actually, we already have the channel ID, just proceed
        pass
    except Exception:
        pass
    # Get the channel and route it
    # This is a bit hacky; in practice the channel is still in Stasis
    try:
        # Re-create the voicemail flow
        ch = CLIENT.channels.get(a_id)
        CALLS[a_id] = {
            "caller": caller, "callee": "voicemail-%s" % mailbox,
            "start": time.time(), "direction": "inbound",
            "login_id": None, "did": did,
        }
        start_voicemail(ch, mailbox, caller)
    except Exception:
        log.exception("inbound voicemail fallback failed")
        try:
            CLIENT.channels.get(a_id).hangup()
        except Exception:
            pass


def did_norm(v):
    """Phone number in one form: digits only, US/Canada numbers as 10 digits."""
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def handle_inbound(channel, did):
    """PSTN inbound: DID -> explicit ring list -> voicemail fallback."""
    import json
    log.info("inbound DID %s", did)
    route = q1("SELECT * FROM inbound_routes WHERE did=? AND enabled=1",
               (did,))
    if not route:
        # Same number written differently (+1 / 1 / 10 digits): match on the
        # normalised form, so one route covers every way the provider sends it.
        want = did_norm(did)
        for r in qall("SELECT * FROM inbound_routes WHERE enabled=1 ORDER BY id"):
            if did_norm(r["did"]) == want:
                route = r
                break
    if not route:
        log.warning("no inbound route for DID %s", did)
        channel.hangup()
        return
    try:
        ring_extens = json.loads(route["ring_extens"] or "[]")
    except Exception:
        ring_extens = []
    caller_num = channel.json["caller"]["number"] or "PSTN"
    ivr_id = dict(route).get("ivr_id")
    if ivr_id:
        ivr = q1("SELECT * FROM ivr_menus WHERE id=? AND enabled=1", (ivr_id,))
        if ivr and start_ivr(channel, ivr, {"caller": caller_num,
                                            "caller_id": "PSTN <%s>" % caller_num,
                                            "did": did, "me": None}):
            return
        log.warning("DID %s points at missing/disabled IVR %s; ringing list",
                    did, ivr_id)
    gid = dict(route).get("group_id")
    if gid:
        g = q1("SELECT * FROM ring_groups WHERE id=? AND enabled=1", (gid,))
        if g:
            try:
                members = [str(x) for x in json.loads(g["members"] or "[]")]
            except Exception:
                members = []
            if members:
                log.info("DID %s -> ring group %s", did, g["name"])
                ring_group(channel, members, g["ring_seconds"] or 30, did, caller_num, group=dict(g))
                return
        log.warning("DID %s points at missing/disabled/empty ring group %s; ringing list", did, gid)
    if not ring_extens:
        log.warning("DID %s has empty ring list", did)
        channel.hangup()
        return
    ring_group(channel, ring_extens, route["timeout_sec"] or 30, did,
               caller_num)


def _group_vm(group, first_login):
    """(login whose box gets the voicemail or None, [extra mailboxes for copies])."""
    if not group:
        return first_login, []
    mode = group.get("vm_mode") or "member"
    if mode == "none":
        return None, []
    primary = first_login
    if mode == "member" and group.get("vm_exten"):
        primary = q1("SELECT * FROM logins WHERE exten=? AND enabled=1", (group["vm_exten"],)) or first_login
    copies = []
    if mode == "all":
        import json as _json
        try:
            members = _json.loads(group.get("members") or "[]")
        except Exception:
            members = []
        for ex in members:
            u = q1("SELECT * FROM logins WHERE exten=? AND enabled=1", (str(ex),))
            if u and primary and u["id"] != primary["id"] and has_feature(u["id"], "voicemail"):
                mb = get_voicemail_box(u["id"])
                if mb:
                    copies.append(mb)
    return primary, copies


def _group_to_voicemail(channel, vm_login, copies, did):
    mailbox = get_voicemail_box(vm_login["id"]) if vm_login else None
    if mailbox:
        if copies:
            VM_COPY[channel.id] = copies
        route_inbound_to_voicemail(channel, mailbox, did)
    else:
        channel.hangup()


GROUP_FORWARD_MODES = ("ext", "group", "ivr", "number")
MAX_GROUP_HOPS = 3


def _group_fallback(channel, group, first_login, vm_login, copies, did, caller_num,
                    caller_id, internal, hops):
    """Nobody in `group` answered (or nobody could ring): the group's choice.
    Voicemail modes go to voicemail; forward modes send the caller on to an
    extension, another group, an IVR menu or an outside number; 'none' hangs
    up. Loops between groups stop after MAX_GROUP_HOPS (-> voicemail/hang up)."""
    mode = (group or {}).get("vm_mode") or "member"
    target = str((group or {}).get("noanswer_target") or "").strip()
    if group and mode in GROUP_FORWARD_MODES and target and hops < MAX_GROUP_HOPS:
        log.info("ring group %s: no answer -> %s %s", group.get("name"), mode, target)
        CALLS.pop(channel.id, None)
        try:
            if mode == "ext":
                ring_group(channel, [target], 30, did, caller_num, caller_id,
                           internal=internal, hops=hops + 1)
                return
            if mode == "group" and target.isdigit():
                g2 = q1("SELECT * FROM ring_groups WHERE id=? AND enabled=1", (int(target),))
                if g2:
                    import json as _json
                    members = [str(x) for x in _json.loads(g2["members"] or "[]")]
                    if members:
                        ring_group(channel, members, g2["ring_seconds"] or 30, did, caller_num, caller_id,
                                   group=dict(g2), internal=internal, hops=hops + 1)
                        return
            if mode == "ivr" and target.isdigit():
                ivr = q1("SELECT * FROM ivr_menus WHERE id=? AND enabled=1", (int(target),))
                if ivr and start_ivr(channel, ivr, {"caller": caller_num, "caller_id": caller_id,
                                                    "did": did, "me": None}):
                    return
            if mode == "number" and first_login is not None:
                if forward_to(channel, first_login, first_login, target, hops,
                              caller_label=caller_num, caller_id=caller_id,
                              direction="inbound"):
                    return
        except Exception:
            log.exception("ring group %s fallback failed", group.get("name"))
        log.warning("ring group %s: fallback %s %s unavailable; voicemail/hang up",
                    group.get("name"), mode, target)
        vm_login = first_login if mode in GROUP_FORWARD_MODES else vm_login
    if mode == "none":
        try:
            channel.hangup()
        except Exception:
            pass
        return
    _group_to_voicemail(channel, vm_login, copies, did)


def ring_group(channel, ring_extens, timeout, did, caller_num, caller_id=None,
               from_ivr=False, group=None, internal=False, hops=0):
    """Ring a list of extensions in parallel (first answer wins), honouring
    each user's DND / forward-always. No answer -> the single rung user's
    forward-on-no-answer, else the first extension's voicemail.

    Used for inbound DIDs and for IVR "ring group" options."""
    caller_id = caller_id or "PSTN <%s>" % caller_num
    # A number that rings just one user follows their "answer with IVR".
    if len(ring_extens) == 1 and not from_ivr and not group:
        solo = q1("SELECT * FROM logins WHERE exten=? AND enabled=1", (ring_extens[0],))
        menu = answer_ivr_for(solo["id"]) if solo else None
        if menu and start_ivr(channel, menu, {"caller": caller_num, "caller_id": caller_id,
                                               "did": did, "me": None}):
            return
    # Who actually rings: skip users on DND, follow forward-always.
    targets, first_login = [], None
    blocked_n = 0
    for exten in ring_extens:
        callee = q1("SELECT * FROM logins WHERE exten=? AND enabled=1",
                    (exten,))
        if not callee:
            continue
        if blocked_by(callee["id"], caller_num):
            log.info("%s blocked %s: not rung", exten, caller_num)
            blocked_n += 1
            continue
        if first_login is None:
            first_login = callee
        if not internal and not can_call_outside(callee["id"]):
            log.info("inbound %s: %s has no outside minutes, skipped", did, exten)
            continue
        prefs = get_prefs(callee["id"])
        if prefs.get("dnd"):
            log.info("inbound %s: %s on DND, skipped", did, exten)
            continue
        fwd = (prefs.get("forward_always") or "").strip()
        if fwd:
            tgt = q1("SELECT * FROM logins WHERE exten=? AND enabled=1", (fwd,))
            if tgt and tgt["id"] != callee["id"]:
                if get_prefs(tgt["id"]).get("dnd") or not can_call_outside(tgt["id"]):
                    log.info("inbound %s: %s forwards to %s (on DND), skipped",
                             did, exten, fwd)
                    continue
                log.info("inbound %s: %s forwarded to %s", did, exten, fwd)
                callee = tgt
            elif not tgt and len(ring_extens) == 1:
                # Number rings only this user: send the call outside.
                if can_call_outside(callee["id"]) and handle_outbound(channel, callee, fwd):
                    CALLS[channel.id].update(caller=caller_num,
                                             direction="forwarded",
                                             forwarded_from=exten, did=did)
                    log.info("inbound %s: %s forwarded outside to %s",
                             did, exten, fwd)
                    return
                log.warning("inbound %s: forward %s -> %s failed (no route)",
                            did, exten, fwd)
            elif not tgt:
                log.info("inbound %s: outside forward for %s ignored "
                         "(ring group)", did, exten)
        if all(t["id"] != callee["id"] for t in targets):
            targets.append(callee)
    vm_login, vm_copies = _group_vm(group, first_login)
    if not targets and blocked_n:
        # Everyone this call would ring has blocked the caller.
        deny_and_hangup(channel, why="caller %s blocked by everyone rung" % caller_num)
        return
    if not targets:
        # Everyone on DND (or no valid users): the group's no-answer choice,
        # or the first extension's box.
        _group_fallback(channel, group, first_login, vm_login, vm_copies, did, caller_num,
                        caller_id, internal, hops)
        return
    # A number that rings one person uses that person's ring time, if set.
    if len(targets) == 1 and not group:
        timeout = ring_seconds_for(targets[0]["id"], default=timeout)
    # Originate to all targets in parallel; first to answer wins
    b_ids = []
    for callee in targets:
        try:
            leg_b = CLIENT.channels.originate(
                endpoint="PJSIP/%s" % callee["sip_username"],
                app=APP,
                app_args="legb,%s" % channel.id,
                caller_id=caller_id,
                timeout=timeout + 5)  # our timer fires first -> voicemail
            b_ids.append(leg_b.id)
            PENDING[leg_b.id] = channel.id
            B_LOGIN[leg_b.id] = callee["id"]
        except Exception:
            log.exception("inbound originate to %s failed", callee["exten"])
    if not b_ids:
        # All originates failed: the group's no-answer choice / voicemail
        _group_fallback(channel, group, first_login, vm_login, vm_copies, did, caller_num,
                        caller_id, internal, hops)
        return
    # Track this as an inbound call with multiple B legs
    CALLS[channel.id] = {
        "caller": caller_num,
        "callee": ",".join(t["exten"] for t in targets),
        "start": CALLS.get(channel.id, {}).get("start", time.time()),
        "b_ids": b_ids,  # multiple B legs (parallel ring)
        # Internal calls to a group's number aren't billed as outside minutes.
        "direction": "group" if internal else "inbound",
        "login_id": None,
        "did": did,
        # No-answer -> forward (single user, not groups) or voicemail
        "vm_login_id": vm_login["id"] if vm_login else None,
        "vm_copies": vm_copies,
        "group": group["name"] if group else "",
        "group_row": dict(group) if group else None,
        "first_login_id": first_login["id"] if first_login else None,
        "caller_id": caller_id, "internal": internal, "group_hops": hops,
        "ring_ids": [t["id"] for t in targets],
        "hops": 0,
    }
    # Ring timeout: hang up all B legs after timeout to trigger voicemail
    a_id = channel.id
    def _inbound_timeout(b_ids=list(b_ids), a_id=a_id):
        for b_id in b_ids:
            log.info("inbound ring timeout for b leg %s", b_id)
            end_unanswered_leg(b_id)
    t = threading.Timer(float(timeout), _inbound_timeout)
    t.daemon = True
    t.start()
    CALLS[channel.id]["ring_timer"] = t
    try:
        channel.ring()
    except Exception:
        pass
    log.info("inbound %s ringing %s for %ss (did %s)",
             caller_num, [t["exten"] for t in targets], timeout, did)


def route_inbound_to_voicemail(channel, mailbox, did):
    """Send an inbound caller straight to a voicemail box."""
    try:
        start_voicemail(channel, mailbox, channel.json["caller"]["number"] or "PSTN")
        CALLS[channel.id] = {
            "caller": channel.json["caller"]["number"] or "PSTN",
            "callee": "voicemail-%s" % mailbox,
            "start": time.time(), "direction": "inbound",
            "login_id": None, "did": did,
        }
    except Exception:
        log.exception("inbound voicemail failed")
        try:
            channel.hangup()
        except Exception:
            pass


# ---------------------------------------------------------------- IVR
# Auto-attendant menus (ivr_menus). A caller hears the greeting, presses a
# key (or dials an extension number if direct dial is on) and is sent to the
# option's destination. No key / wrong key replays the menu up to
# max_retries, then the fallback destination applies.

def quota(login_id, feature):
    """Per-user feature quota (billing hook). Row in user_entitlements wins,
    else kv_settings default_quota_<feature>, else 0. Mirrors pbx-api."""
    try:
        r = q1("SELECT quota FROM user_entitlements WHERE login_id=? AND feature=?",
               (login_id, feature))
        if r:
            return int(r["quota"])
        d = q1("SELECT value FROM kv_settings WHERE key=?", ("default_quota_" + feature,))
        return int(d["value"]) if d and str(d["value"]).strip() else 0
    except (sqlite3.OperationalError, ValueError, TypeError):
        return 0


# ---- plan limits (mirror pbx-api/entitlements.py)
UNLIMITED = -1
RECORDING_CONSENT_VERSION = 1
DENIED_PROMPT = "sound:cannot-complete-as-dialed"
UNAVAILABLE_PROMPT = "sound:vm-nobodyavail"


def _is_admin(login_id):
    r = q1("SELECT role FROM logins WHERE id=?", (login_id,)) if login_id else None
    return bool(r) and r["role"] == "admin"


def has_feature(login_id, feature):
    """On/off features (voicemail, recording). Admins always have them."""
    if not login_id:
        return False
    return _is_admin(login_id) or quota(login_id, feature) != 0


def minutes_used(login_id):
    try:
        r = q1("SELECT COALESCE(SUM((bill_sec + 59) / 60), 0) AS m FROM cdr"
               " WHERE started_at >= datetime('now', 'localtime', 'start of month')"
               " AND ((login_id=? AND direction IN ('outbound', 'forwarded'))"
               "   OR (answered_login_id=? AND direction='inbound'))", (login_id, login_id))
        return int(r["m"] or 0)
    except sqlite3.OperationalError:     # old DB without answered_login_id
        return 0


def can_call_outside(login_id):
    """Outside (trunk) calls need minutes left in the user's plan."""
    if not login_id:
        return False
    if _is_admin(login_id):
        return True
    q = quota(login_id, "call_minutes")
    if q == UNLIMITED:
        return True
    return q > 0 and minutes_used(login_id) < q


def recording_consented(login_id):
    try:
        r = q1("SELECT recording_consent_version FROM user_prefs WHERE login_id=?", (login_id,))
        return bool(r) and int(r["recording_consent_version"] or 0) >= RECORDING_CONSENT_VERSION
    except sqlite3.OperationalError:
        return False


def deny_and_hangup(chan, prompt=DENIED_PROMPT, why=""):
    log.info("denied %s: %s", getattr(chan, "id", "?"), why)
    try:
        chan.answer()
    except Exception:
        pass
    play_then(chan, prompt, lambda: _hangup(chan))


def ivr_active(ivr):
    """System menus are always usable. A user's menu is usable while their
    quota covers it (oldest menus first), so a lapsed/downgraded plan turns
    menus off without deleting them."""
    ivr = dict(ivr)
    if not ivr.get("enabled"):
        return False
    owner = ivr.get("owner_login_id")
    if not owner:
        return True
    n = quota(owner, "ivr_menus")
    if n <= 0:
        return False
    rows = qall("SELECT id FROM ivr_menus WHERE owner_login_id=? ORDER BY id LIMIT ?",
                (owner, n))
    return any(r["id"] == ivr["id"] for r in rows)


def answer_ivr_for(login_id):
    """The user's 'answer my calls with' menu, if set, owned by them and active."""
    mid = get_prefs(login_id).get("answer_ivr_id")
    if not mid:
        return None
    try:
        ivr = q1("SELECT * FROM ivr_menus WHERE id=? AND owner_login_id=?", (mid, login_id))
    except sqlite3.OperationalError:
        return None
    return ivr if ivr and ivr_active(ivr) else None


IVR_SESSIONS = {}    # channel_id -> session dict
PLAYBACKS = {}       # playback_id -> channel_id
IVR_LOCK = threading.RLock()
IVR_INTERDIGIT = 2.5     # seconds to wait for the next digit of a number
IVR_MAX_VISITS = 6       # menu-to-menu hops per call (loop guard)
IVR_DEFAULT_GREETING = "sound:vm-enter-num-to-call"   # Asterisk core sound
IVR_INVALID = "sound:option-is-invalid"


def _ivr_opts(ivr):
    import json
    try:
        o = json.loads(ivr.get("options") or "{}")
        return o if isinstance(o, dict) else {}
    except Exception:
        return {}


def _ivr_fallback(ivr):
    import json
    try:
        f = json.loads(ivr.get("fallback") or "{}")
        return f if isinstance(f, dict) and f.get("type") else {"type": "hangup"}
    except Exception:
        return {"type": "hangup"}


def _ivr_cancel_timer(sess):
    t = sess.pop("timer", None)
    if t:
        t.cancel()


def _ivr_set_timer(sess, secs, fn):
    _ivr_cancel_timer(sess)
    cid = sess["chan"].id

    def _fire():
        with IVR_LOCK:
            cur = IVR_SESSIONS.get(cid)
            if cur is sess and sess.get("timer") is t:
                sess.pop("timer", None)
                fn(sess)
    t = threading.Timer(float(secs), _fire)
    t.daemon = True
    sess["timer"] = t
    t.start()


def _ivr_stop_playback(sess):
    pb = sess.pop("playback", None)
    if pb:
        PLAYBACKS.pop(pb, None)
        try:
            CLIENT.delete("playbacks/%s" % pb)
        except Exception:
            pass


def _ivr_play(sess, media, after):
    """Play media; call after(sess) when it finishes (or fails to start)."""
    _ivr_stop_playback(sess)
    sess["after_play"] = after
    try:
        pb = sess["chan"].play(media)
        pb_id = (pb or {}).get("id")
    except Exception:
        log.exception("IVR %s: playback of %s failed", sess["ivr"]["name"], media)
        pb_id = None
    if pb_id:
        sess["playback"] = pb_id
        PLAYBACKS[pb_id] = sess["chan"].id
    else:
        sess.pop("after_play", None)
        after(sess)


def _ivr_greeting_media(ivr):
    import os
    path = ivr.get("greeting_path") or ""
    if path and os.path.isfile(path):
        return "sound:" + os.path.splitext(path)[0]
    return IVR_DEFAULT_GREETING


def start_ivr(channel, ivr, ctx):
    """Put a caller into an IVR menu. ctx: caller (number/label), caller_id,
    did, me (login row for internal callers, else None), visits."""
    ivr = dict(ivr)
    if not ivr_active(ivr):
        log.info("IVR %s inactive (disabled or owner over quota)", ivr.get("name"))
        return False
    with IVR_LOCK:
        visits = ctx.get("visits", 0) + 1
        if visits > IVR_MAX_VISITS:
            log.warning("IVR loop guard: hanging up %s", channel.id)
            ivr_end(channel.id)
            try:
                channel.hangup()
            except Exception:
                pass
            return True
        ivr_end(channel.id)
        ctx = dict(ctx, visits=visits)
        try:
            channel.answer()
        except Exception:
            pass
        prev = CALLS.get(channel.id, {})
        CALLS[channel.id] = {
            "caller": ctx.get("caller", ""), "callee": "ivr-%s" % ivr["name"],
            "start": prev.get("start", time.time()),
            "direction": "internal" if ctx.get("me") else "inbound",
            "login_id": ctx["me"]["id"] if ctx.get("me") else None,
            "did": ctx.get("did", ""),
        }
        sess = {"ivr": ivr, "chan": channel, "ctx": ctx, "digits": "",
                "tries": 0}
        IVR_SESSIONS[channel.id] = sess
        log.info("IVR %s: caller %s entered", ivr["name"], ctx.get("caller"))
        _ivr_menu(sess)
        return True


def _ivr_menu(sess):
    """(Re)play the greeting, then wait timeout_sec for a key."""
    sess["digits"] = ""
    _ivr_cancel_timer(sess)
    _ivr_play(sess, _ivr_greeting_media(sess["ivr"]), _ivr_wait_for_key)


def _ivr_wait_for_key(sess):
    if sess["digits"]:
        return
    _ivr_set_timer(sess, max(1, int(sess["ivr"].get("timeout_sec") or 5)),
                   lambda s: _ivr_retry(s, "timeout"))


def _ivr_retry(sess, why):
    sess["tries"] += 1
    ivr = sess["ivr"]
    if sess["tries"] > int(ivr.get("max_retries") or 0):
        log.info("IVR %s: %s, retries used up -> fallback", ivr["name"], why)
        ivr_go(sess, _ivr_fallback(ivr))
        return
    log.info("IVR %s: %s (try %d)", ivr["name"], why, sess["tries"])
    if why == "invalid":
        _ivr_play(sess, IVR_INVALID, _ivr_menu)
    else:
        _ivr_menu(sess)


def _ext_exists(exten):
    return q1("SELECT 1 FROM logins WHERE exten=? AND enabled=1", (exten,)) is not None


def _ext_prefix(digits):
    """True if some extension is longer than `digits` and starts with it."""
    return q1("SELECT 1 FROM logins WHERE enabled=1 AND exten LIKE ? AND length(exten) > ?",
              (digits + "%", len(digits))) is not None


def ivr_dtmf(channel_id, digit):
    with IVR_LOCK:
        sess = IVR_SESSIONS.get(channel_id)
        if not sess or not digit:
            return
        _ivr_stop_playback(sess)
        sess.pop("after_play", None)
        _ivr_cancel_timer(sess)
        sess["digits"] += digit
        d = sess["digits"]
        opts = _ivr_opts(sess["ivr"])
        direct = bool(sess["ivr"].get("direct_dial"))
        could_be_ext = direct and d.isdigit() and (_ext_prefix(d) or _ext_exists(d))
        if d in opts and not (direct and _ext_prefix(d)):
            _ivr_resolve(sess)           # unambiguous menu key
        elif direct and _ext_exists(d) and not _ext_prefix(d):
            _ivr_resolve(sess)           # complete extension number
        elif could_be_ext or any(k.startswith(d) and k != d for k in opts):
            _ivr_set_timer(sess, IVR_INTERDIGIT, _ivr_resolve)   # wait for more
        else:
            _ivr_resolve(sess)           # nothing can match -> invalid


def _ivr_resolve(sess):
    d, sess["digits"] = sess["digits"], ""
    opts = _ivr_opts(sess["ivr"])
    if d in opts:
        log.info("IVR %s: key %s", sess["ivr"]["name"], d)
        ivr_go(sess, opts[d])
    elif sess["ivr"].get("direct_dial") and d.isdigit() and _ext_exists(d):
        log.info("IVR %s: direct dial %s", sess["ivr"]["name"], d)
        ivr_go(sess, {"type": "ext", "target": d})
    else:
        _ivr_retry(sess, "invalid")


def ivr_end(channel_id):
    """Forget a channel's IVR session (it left the menu or hung up)."""
    with IVR_LOCK:
        sess = IVR_SESSIONS.pop(channel_id, None)
        if sess:
            _ivr_cancel_timer(sess)
            _ivr_stop_playback(sess)


def ivr_go(sess, dest):
    """Send the caller to an IVR destination."""
    chan, ctx = sess["chan"], sess["ctx"]
    t = (dest or {}).get("type", "hangup")
    target = str((dest or {}).get("target", "")).strip()
    if t == "repeat":
        _ivr_menu(sess)
        return
    ivr_end(chan.id)
    if t == "ivr":
        nxt = q1("SELECT * FROM ivr_menus WHERE id=? AND enabled=1", (target,))
        if nxt and start_ivr(chan, nxt, ctx):
            return
        log.warning("IVR %s: target menu %s missing", sess["ivr"]["name"], target)
    elif t in ("ext", "vm"):
        callee = q1("SELECT * FROM logins WHERE exten=? AND enabled=1", (target,))
        if callee and t == "vm":
            CALLS.setdefault(chan.id, {})["caller"] = ctx.get("caller", "")
            route_to_voicemail(chan.id, callee["exten"])
            return
        if callee:
            if ctx.get("me"):
                ring_local(chan, ctx["me"], callee, from_ivr=True)
            else:
                ring_local(chan, callee, callee, caller_label=ctx.get("caller"),
                           caller_id=ctx.get("caller_id"), direction="inbound",
                           extra={"ring_ids": [callee["id"]],
                                  "vm_login_id": callee["id"],
                                  "did": ctx.get("did", "")},
                           from_ivr=True)
            return
        log.warning("IVR %s: extension %s missing", sess["ivr"]["name"], target)
    elif t == "group":
        extens = [e.strip() for e in target.split(",") if e.strip()]
        if extens:
            ring_group(chan, extens, DEFAULT_RING, ctx.get("did", ""),
                       ctx.get("caller", ""), ctx.get("caller_id"),
                       from_ivr=True)
            return
    log.info("IVR %s: hanging up %s", sess["ivr"]["name"], chan.id)
    try:
        chan.hangup()
    except Exception:
        pass


def on_playback_finished(event):
    pb_id = (event.get("playback") or {}).get("id")
    cb = PB_CALLBACKS.pop(pb_id, None)
    if cb:
        try:
            cb()
        except Exception:
            log.exception("playback callback failed")
        return
    with IVR_LOCK:
        cid = PLAYBACKS.pop(pb_id, None)
        sess = IVR_SESSIONS.get(cid) if cid else None
        if not sess or sess.get("playback") != pb_id:
            return
        sess.pop("playback", None)
        after = sess.pop("after_play", None)
        if after:
            after(sess)


def start_recording(channel, exten_row, direction):
    """Start MixMonitor-equivalent recording per admin/user flags."""
    # TODO Phase 2: POST /channels/{id}/record for each active system,
    # files under REC_ADMIN_DIR / REC_USER_DIR, row in recordings table.
    pass


def write_cdr(channel, event):
    """Persist one CDR row when the A leg ends."""
    info = CALLS.pop(channel.id, None)
    if not info:
        return
    end = time.time()
    duration = int(end - info["start"])
    bill = duration if info.get("bridged_at") else 0
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info["start"]))
    for b in [k for k, v in PENDING.items() if v == channel.id] + [info.get("b_id")]:
        B_LOGIN.pop(b, None)
    vals = (channel.id, ts, info["caller"], info["callee"],
            info["direction"], info["login_id"], duration, bill,
            "ANSWERED" if info.get("bridged_at") else "NO ANSWER")
    with db() as c:
        try:
            c.execute(
                "INSERT INTO cdr (call_id, started_at, src, dst, direction,"
                " login_id, duration_sec, bill_sec, disposition, answered_login_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)", vals + (info.get("answered_id"),))
        except sqlite3.OperationalError:
            c.execute(
                "INSERT INTO cdr (call_id, started_at, src, dst, direction,"
                " login_id, duration_sec, bill_sec, disposition)"
                " VALUES (?,?,?,?,?,?,?,?,?)", vals)
    BRIDGED.pop(channel.id, None)
    log.info("cdr: %s -> %s dur=%ds bill=%ds",
             info["caller"], info["callee"], duration, bill)


# ---------------------------------------------------------------- events

def _kill_switch_on():
    try:
        r = q1("SELECT value FROM kv_settings WHERE key='kill_switch'")
        return bool(r) and r["value"] == "1"
    except Exception:
        return False


def on_stasis_start(channel, event):
    _args = event.get("args", [])
    _emergency = (len(_args) > 1 and _args[0] == "internal"
                  and e911.emergency_number(q1, _args[1]) is not None)
    if _kill_switch_on() and not _emergency:
        log.warning("kill switch engaged: hanging up new channel %s", channel.id)
        try:
            channel.hangup()
        except Exception:
            pass
        return
    args = event.get("args", [])
    kind = args[0] if args else "internal"
    target = args[1] if len(args) > 1 else ""
    try:
        if kind == "internal":
            handle_internal(channel, target)
        elif kind == "inbound":
            handle_inbound(channel, target)
        elif kind == "legb":
            # Originated B leg: wait for answer (ChannelStateChange -> Up).
            log.info("b leg in Stasis: %s", channel.id)
        elif kind == "spy":
            # Snoop channel for *555: bridge with the spy.
            spy_id = target
            if spy_id:
                on_spy_stasis(channel, spy_id)
            else:
                channel.hangup()
        else:
            log.warning("unknown Stasis kind %r", kind)
            channel.hangup()
    except Exception:
        log.exception("StasisStart failed")
        try:
            channel.hangup()
        except Exception:
            pass


def on_channel_state_change(channel, event):
    # Bridge A+B the moment the B leg answers.
    if (channel.id in PENDING
            and event.get("channel", {}).get("state") == "Up"):
        try:
            on_legb_answer(channel)
        except Exception:
            log.exception("legb answer handling failed")


def on_channel_destroyed(channel, event):
    """A ringing B leg went away without answering (declined, busy,
    unreachable, or our timeout hung it up): run no-answer handling."""
    with LEG_LOCK:
        if channel.id in PENDING:
            log.info("b leg %s ended unanswered (%s)", channel.id,
                     event.get("cause_txt", ""))
            hangup_other_leg(channel.id)


def on_stasis_end(channel, event):
    try:
        ivr_end(channel.id)
        vm_cleanup(channel.id)
        with LEG_LOCK:
            hangup_other_leg(channel.id)
        DISA_SESSIONS.pop(channel.id, None)
        mon = SPY_MONITORS.pop(channel.id, None)
        if mon is not None:
            _spy_stop_monitor(mon)
        write_cdr(channel, event)
    except Exception:
        log.exception("StasisEnd handling failed")


# ---------------------------------------------------------------- status server
# Localhost-only JSON status. The brain is the source of truth for live calls
# (it owns every call via ARI); the API/dashboard proxy this instead of
# scraping Asterisk. By default nothing here is exposed past 127.0.0.1;
# PBX_STATUS_HOST can open it to the LAN (use PBX_MSG_TOKEN then).
STATUS_PORT = int(os.environ.get("PBX_STATUS_PORT", "8099"))
# Bind address for the status server. Default stays 127.0.0.1 (portal-only);
# set PBX_STATUS_HOST=0.0.0.0 to let LAN clients (Home Assistant) reach the
# message endpoints below.
STATUS_HOST = os.environ.get("PBX_STATUS_HOST", "127.0.0.1")
# Shared token for LAN callers of /messages/send and /messages/inbox. When
# set, the token must arrive as an X-Msg-Token header or a ?token= query
# parameter. The portal talks to 127.0.0.1 without a token and keeps working
# when this is unset.
MSG_TOKEN = os.environ.get("PBX_MSG_TOKEN", "")


def _live_calls():
    out = []
    for call_id, info in list(CALLS.items()):
        try:
            out.append({
                "call_id": call_id,
                "caller": info.get("caller", ""),
                "callee": info.get("callee", ""),
                "direction": info.get("direction", ""),
                "state": "bridged" if info.get("bridged_at") else "ringing",
                "started_at": info.get("start"),
                "bridged_at": info.get("bridged_at"),
                "login_id": info.get("login_id"),
                "trunk": info.get("trunk", ""),
                "did": info.get("did", ""),
                "forwarded_from": info.get("forwarded_from", ""),
                "answered_id": info.get("answered_id"),
            })
        except Exception:
            continue
    # *555 spy monitor sessions: show who's listening, so the dashboard
    # reflects them. Not real calls (no CDR/billing impact).
    for spy_id, sess in list(SPY_MONITORS.items()):
        try:
            out.append({
                "call_id": spy_id,
                "caller": sess.get("spy_exten", ""),
                "callee": sess.get("target") or "",
                "direction": "spy",
                "state": "bridged" if sess.get("call_key") else "waiting",
                "started_at": sess.get("started_at"),
                "bridged_at": sess.get("bridged_at"),
                "login_id": None,
                "trunk": "",
                "did": "",
                "forwarded_from": "",
                "answered_id": None,
                "spy_label": sess.get("label") or "",
            })
        except Exception:
            continue
    return out


_TEXT_MAIL_LAST = {}   # (to, from) -> time of last email (one per conversation per 10 min)


def email_text(to_exten, from_exten, body):
    """Email the recipient about a new text, if they have an address and the
    switch on (My Phone -> Settings). Bursts in one conversation send one
    email per 10 minutes."""
    import mailer
    try:
        with db() as c:
            to = c.execute("SELECT id, username, display_name, exten, vm_email FROM logins WHERE exten=? AND enabled=1",
                           (to_exten,)).fetchone()
            if not to or not (to["vm_email"] or "").strip():
                return
            try:
                p = c.execute("SELECT text_email FROM user_prefs WHERE login_id=?", (to["id"],)).fetchone()
            except sqlite3.OperationalError:
                p = None
            if p is not None and not p["text_email"]:
                return
            frm = c.execute("SELECT display_name, exten FROM logins WHERE exten=?", (from_exten,)).fetchone()
            cfg = mailer.settings(c)
        if not mailer.configured(cfg):
            return
        key, now = (to_exten, from_exten), time.time()
        if now - _TEXT_MAIL_LAST.get(key, 0) < 600:
            return
        _TEXT_MAIL_LAST[key] = now
        who = mailer.clean((frm["display_name"] if frm else "") or "") or "Extension"
        link = (cfg.get("panel_url") or "").rstrip("/")
        preview = body if len(body) <= 300 else body[:300] + "…"
        mailer.send(cfg, to["vm_email"], f"New text from {who} ({from_exten})",
                    f"Hi {to['display_name'] or to['username']},\n\n"
                    f"{who} (ext {from_exten}) sent you a text:\n\n{preview}\n\n"
                    + (f"Reply online: {link}/ucp/messages?with={from_exten}\n" if link else "")
                    + "\nTurn these emails off in My Phone -> Settings.\n")
        log.info("text email sent to %s", to["vm_email"])
    except Exception as e:  # noqa: BLE001
        log.warning("text email failed: %s", e)


def deliver_message(to_exten, from_exten, body):
    """Push a text to the recipient's phone as a SIP MESSAGE (best effort:
    it's already stored, so My Phone shows it even if the phone is offline)."""
    threading.Thread(target=email_text, args=(to_exten, from_exten, body), daemon=True,
                     name="text-email").start()
    to = q1("SELECT sip_username, exten FROM logins WHERE exten=? AND enabled=1", (to_exten,))
    frm = q1("SELECT display_name, exten FROM logins WHERE exten=?", (from_exten,))
    if not to or not to["sip_username"] or CLIENT is None:
        return False
    name = ((frm["display_name"] if frm else "") or from_exten).replace('"', "")
    try:
        CLIENT.put("endpoints/PJSIP/%s/sendMessage" % to["sip_username"],
                   params={"from": '"%s" <sip:%s@pbx>' % (name, from_exten), "body": body})
        log.info("msg delivered %s -> %s", from_exten, to_exten)
        return True
    except Exception as e:  # noqa: BLE001  (phone offline / not registered)
        log.info("msg %s -> %s not delivered to phone: %s", from_exten, to_exten, e)
        return False



def _msg_token_ok(handler):
    """True when the request carries the LAN token (or none is configured)."""
    if not MSG_TOKEN:
        return True
    # Localhost callers (portal, AGI msg-route) run on the box itself and
    # predate the token; the token only needs to guard the LAN listener.
    if handler.client_address[0] in ("127.0.0.1", "::1"):
        return True
    if handler.headers.get("X-Msg-Token") == MSG_TOKEN:
        return True
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    return qs.get("token", [""])[0] == MSG_TOKEN


def _json_response(handler, payload, status=200):
    import json as _json
    body = _json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_quota(c, login_id):
    """Texts/month allowed for a login: -1 = unlimited, 0 = none.
    Mirrors pbx-api/entitlements.py quota() and agi_server.text_quota()."""
    r = c.execute("SELECT role FROM logins WHERE id=?", (login_id,)).fetchone()
    if r and r[0] == "admin":
        return -1
    try:
        q = c.execute("SELECT quota FROM user_entitlements WHERE login_id=? AND feature='messages'",
                      (login_id,)).fetchone()
        if q is not None:
            return int(q[0])
        d = c.execute("SELECT value FROM kv_settings WHERE key='default_quota_messages'").fetchone()
        return int(d[0]) if d and str(d[0]).strip() else 0
    except (sqlite3.OperationalError, ValueError):
        return 0


def send_message_api(to_exten, from_exten, body):
    """Validate, store and deliver a text from an API caller (HA/ESP).

    Same rules as the portal's /ucp/messages/send and the AGI msg-route:
    sender/recipient prefs, blocks, plan quota and per-login max_messages.
    If to_exten is an external PSTN number, sends via voip.ms.
    Returns (ok, info_dict)."""
    body = (body or "")[:1600]
    if not re.fullmatch(r"[0-9*#+]{1,32}", to_exten or ""):
        # allow +E.164 for external
        if not voipms_sms.is_external_number(to_exten or ""):
            return False, {"error": "bad destination"}
    if not re.fullmatch(r"[0-9*#+]{1,32}", from_exten or ""):
        return False, {"error": "bad sender extension"}
    if not body.strip():
        return False, {"error": "empty message"}
    # External outbound via voip.ms
    if voipms_sms.is_external_number(to_exten):
        with db() as c:
            sender = c.execute("SELECT * FROM logins WHERE exten=? AND enabled=1",
                               (from_exten,)).fetchone()
            if not sender:
                return False, {"error": "unknown sender extension"}
            try:
                r = c.execute("SELECT msg_out FROM user_prefs WHERE login_id=?", (sender["id"],)).fetchone()
                if r is not None and not int(r[0]):
                    return False, {"error": "sending is turned off for this extension"}
            except sqlite3.OperationalError:
                pass
            plan = _text_quota(c, sender["id"])
            used = c.execute("SELECT COUNT(*) FROM messages WHERE login_id=?"
                             " AND sent_at >= datetime('now','localtime','start of month','utc')",
                             (sender["id"],)).fetchone()[0]
            if plan == 0 or (plan > 0 and used >= plan):
                return False, {"error": "monthly text allowance used up"}
            if used >= (sender["max_messages"] or 0):
                return False, {"error": "extension text limit reached"}
            cfg = voipms_sms.get_voipms_config(c)
            dest_e164 = voipms_sms.e164(to_exten)
            # Prefer the DID this conversation is already on (so replies go out
            # from the DID the contact texted), else the exten's mapped DID.
            sender_did = voipms_sms.lookup_conversation_did(c, from_exten, dest_e164)
            if not sender_did:
                sender_did = voipms_sms.lookup_sender_did(c, from_exten)
            if not sender_did:
                return False, {"error": "no DID route for this extension"}
            if not cfg.get("voipms_api_username") or not cfg.get("voipms_api_password"):
                return False, {"error": "voip.ms API not configured"}
            msg_id = c.execute("INSERT INTO messages (from_ext, to_ext, body, login_id, via_did)"
                               " VALUES (?,?,?,?,?)",
                               (from_exten, dest_e164, body, sender["id"], sender_did)).lastrowid
        try:
            resp = voipms_sms.send_sms_via_voipms(cfg["voipms_api_username"], cfg["voipms_api_password"],
                                                 sender_did, dest_e164, body)
            ok = str(resp.get("status")) == "success"
            return ok, {"stored": True, "id": msg_id, "voipms": resp}
        except Exception as e:
            return False, {"stored": True, "id": msg_id, "error": str(e)}
    with db() as c:
        sender = c.execute("SELECT * FROM logins WHERE exten=? AND enabled=1",
                           (from_exten,)).fetchone()
        if not sender:
            return False, {"error": "unknown sender extension"}
        dest = c.execute("SELECT * FROM logins WHERE exten=? AND enabled=1",
                         (to_exten,)).fetchone()
        if not dest:
            return False, {"error": "unknown destination extension"}

        def _pref(login_id, col):
            try:
                r = c.execute("SELECT %s FROM user_prefs WHERE login_id=?" % col,
                              (login_id,)).fetchone()
            except sqlite3.OperationalError:
                return 1
            return 1 if r is None else int(r[0])

        if not _pref(sender["id"], "msg_out"):
            return False, {"error": "sending is turned off for this extension"}
        if not _pref(dest["id"], "msg_in"):
            return False, {"error": "recipient isn't accepting texts"}
        try:
            blocked = c.execute("SELECT 1 FROM blocked_numbers WHERE login_id=? AND number=?",
                                (dest["id"], from_exten)).fetchone()
        except sqlite3.OperationalError:
            blocked = None
        if blocked:
            return False, {"error": "recipient blocked this extension"}
        plan = _text_quota(c, sender["id"])
        used = c.execute("SELECT COUNT(*) FROM messages WHERE login_id=?"
                         " AND sent_at >= datetime('now','localtime','start of month','utc')",
                         (sender["id"],)).fetchone()[0]
        if plan == 0 or (plan > 0 and used >= plan):
            return False, {"error": "monthly text allowance used up"}
        if used >= (sender["max_messages"] or 0):
            return False, {"error": "extension text limit reached"}
        msg_id = c.execute("INSERT INTO messages (from_ext, to_ext, body, login_id)"
                           " VALUES (?,?,?,?)",
                           (from_exten, to_exten, body, sender["id"])).lastrowid
    delivered = deliver_message(to_exten, from_exten, body)
    log.info("msg api %s -> %s stored id=%d delivered=%s",
             from_exten, to_exten, msg_id, delivered)
    return True, {"stored": True, "id": msg_id, "delivered": delivered}


def inbox_messages(exten, since_id=0, limit=20):
    """Recent texts involving exten (both directions), oldest first."""
    limit = max(1, min(int(limit or 20), 50))
    rows = qall("SELECT id, from_ext, to_ext, body, sent_at FROM messages"
                " WHERE (from_ext=? OR to_ext=?) AND id>? ORDER BY id DESC LIMIT ?",
                (exten, exten, since_id, limit))
    msgs = [{"id": r["id"], "from": r["from_ext"], "to": r["to_ext"],
             "body": r["body"], "sent_at": r["sent_at"],
             "dir": "out" if r["from_ext"] == exten else "in"}
            for r in rows]
    latest = q1("SELECT MAX(id) AS m FROM messages WHERE from_ext=? OR to_ext=?",
                (exten, exten))
    return {"messages": list(reversed(msgs)),
            "latest_id": (latest["m"] or 0) if latest else 0}


class _StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        import json as _json
        parts = urllib.parse.urlparse(self.path)
        if parts.path == "/calls":
            body = _json.dumps({"calls": _live_calls()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parts.path == "/messages/inbox":
            if not _msg_token_ok(self):
                _json_response(self, {"ok": False, "error": "bad token"}, 403)
                return
            qs = urllib.parse.parse_qs(parts.query)
            ext = qs.get("ext", [""])[0]
            if not re.fullmatch(r"[0-9*#+]{1,32}", ext or ""):
                _json_response(self, {"ok": False, "error": "bad ext"}, 400)
                return
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                since = 0
            data = inbox_messages(ext, since, qs.get("limit", ["20"])[0])
            data["ok"] = True
            _json_response(self, data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        import json as _json
        if self.path == "/messages/send":
            if not _msg_token_ok(self):
                _json_response(self, {"ok": False, "error": "bad token"}, 403)
                return
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 20000)
                req = _json.loads(self.rfile.read(n) or b"{}")
                if req.get("store"):
                    # API caller (HA/ESP): validate + store + deliver.
                    ok, info = send_message_api(str(req.get("to", "")),
                                                str(req.get("from", "")),
                                                str(req.get("body", "")))
                    info["ok"] = ok
                    _json_response(self, info)
                else:
                    # Portal path: already validated + stored by pbx-api.
                    ok = deliver_message(str(req.get("to", "")), str(req.get("from", "")),
                                         str(req.get("body", ""))[:1600])
                    _json_response(self, {"ok": ok, "delivered": ok})
            except Exception:
                log.exception("messages/send failed")
                _json_response(self, {"ok": False, "error": "internal error"}, 500)
            return
        if self.path == "/calls/hangup-all":
            # Emergency: hang up every tracked call. Hanging the A leg
            # triggers StasisEnd -> the other leg is hung up too.
            count = 0
            for call_id in list(CALLS.keys()):
                try:
                    CLIENT.channels.get(call_id).hangup()
                    count += 1
                except Exception:
                    pass
            log.warning("hangup-all: hung up %d calls", count)
            body = _json.dumps({"hung_up": count}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def start_status_server():
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer((STATUS_HOST, STATUS_PORT), _StatusHandler)
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="status-server").start()
    log.info("status server on %s:%d", STATUS_HOST, STATUS_PORT)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    global CLIENT
    start_status_server()
    CLIENT = ARIClient(ARI_URL, ARI_USER, ARI_PASS)
    CLIENT.on_event("StasisStart", on_stasis_start)
    CLIENT.on_event("StasisEnd", on_stasis_end)
    CLIENT.on_event("ChannelStateChange", on_channel_state_change)
    CLIENT.on_event("RecordingFinished", on_recording_finished)
    CLIENT.on_event("ChannelDtmfReceived", on_dtmf)
    CLIENT.on_event("ChannelDestroyed", on_channel_destroyed)
    CLIENT.on_event("PlaybackFinished", on_playback_finished)
    log.info("pbx-brain connected to %s as %s", ARI_URL, ARI_USER)
    CLIENT.run(APP)


if __name__ == "__main__":
    main()
