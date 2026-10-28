# SPDX-License-Identifier: GPL-2.0-or-later
"""JSON API for the My Phone Android app (/api/v1/app/*, /api/v1/me/*).

Sign-in: a long-lived, per-phone device token (`pbxd_...`). Only its sha256
is stored (app_devices). The expiry slides forward on every use, so an app
used at least every DEVICE_TTL_DAYS days never has to sign in again. A
password change, "Sign out" in My Phone > App, or a disabled login ends it.

Every change goes through the same My Phone (ucp / safety_ui) handlers the
web panel uses, so permission checks, the safety lock, plan quotas and rate
limits can't drift apart. Those handlers answer with a redirect carrying a
flash key; here it becomes JSON: {"ok": bool, "code": key, "message": text}.
Phones send changes form-encoded, exactly like the web forms.

Device tokens only work for My Phone. They are not admin API keys and never
reach admin pages (those use the session cookie or api_keys).
"""
import hashlib
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

M = None  # the app module, set by install()

DEVICE_TTL_DAYS = 60
TOKEN_PREFIX = "pbxd_"
MAX_DEVICES = 10                 # per login
PAIR_TTL = 300                   # pairing codes live 5 minutes
_pair_codes: dict[str, tuple[int, float]] = {}   # code -> (login_id, expires)
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


# ---------------------------------------------------------------- tokens

def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def device_login(request: Request):
    """Login row (dict, plus device_id) for a valid device token, else None.

    Slides the expiry forward. Never accepts admin api_keys tokens.
    """
    tok = _bearer(request)
    if not tok.startswith(TOKEN_PREFIX):
        return None
    with M.db() as c:
        r = c.execute(
            "SELECT d.id AS device_id, d.pwsig AS device_pwsig, d.calls_seen_id, l.* FROM app_devices d"
            " JOIN logins l ON l.id = d.login_id"
            " WHERE d.token_hash=? AND d.expires_at > datetime('now') AND l.enabled=1",
            (_hash(tok),)).fetchone()
        if not r:
            return None
        if r["device_pwsig"] != M._pw_sig(r["pwhash"]):
            # Password changed since this phone signed in.
            c.execute("DELETE FROM app_devices WHERE id=?", (r["device_id"],))
            c.commit()
            return None
        c.execute("UPDATE app_devices SET last_used_at=datetime('now'),"
                  " expires_at=datetime('now', ?) WHERE id=?",
                  (f"+{DEVICE_TTL_DAYS} days", r["device_id"]))
        # The app reports its version on every API call; keep the latest
        # per login so the admin can see who is on what (Logins page +
        # Signed in now). No-ops unless the version changed or the last
        # report is over an hour old, so the hot path stays one write.
        ver = (request.headers.get("x-myphone-version") or "").strip()
        ver = "".join(ch for ch in ver if ch.isprintable())[:20]
        if ver:
            c.execute("UPDATE logins SET app_version=?, app_version_at=datetime('now')"
                      " WHERE id=? AND (app_version IS NULL OR app_version<>?"
                      " OR app_version_at IS NULL OR app_version_at=''"
                      " OR app_version_at < datetime('now','-1 hour'))",
                      (ver, r["id"], ver))
        c.commit()
    return dict(r)


def device_session(row: dict) -> dict:
    """Session-shaped dict the ucp handlers expect (no cookie, no CSRF:
    bearer callers are CSRF-exempt because browsers never attach them)."""
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "pwsig": M._pw_sig(row["pwhash"]), "device_id": row["device_id"]}


# ---------------------------------------------------------------- app store mode
# For phones running the app from Google Play (or any store): the app gets no
# plan / billing / usage information and no "not in your plan" wording, so it
# never looks like it sells or upsells anything. Limits still apply the same;
# what's unavailable is just "not available on this account". Per login
# (logins.app_hide_billing) or for everyone (Billing -> App store mode).
NEUTRAL = "This isn't available on your account."


def account_site():
    """Where store-mode users are told to look for account details
    (Billing -> App store mode), e.g. myphone.example.com."""
    try:
        with M.db() as c:
            r = c.execute("SELECT value FROM kv_settings WHERE key='app_account_site'").fetchone()
            return (r[0] if r else "").strip()
    except Exception:
        return ""


def account_note():
    site = account_site()
    return f"For account details, visit {site}" if site else "For account details, contact your administrator."
_PLANNY = ("plan", "upgrade", "billing", "subscri", "purchase", "price", "minutes left", "texts left")


def hide_billing(me) -> bool:
    try:
        with M.db() as c:
            r = c.execute("SELECT value FROM kv_settings WHERE key='app_hide_billing_all'").fetchone()
            if r and r[0] == "1":
                return True
            r = c.execute("SELECT app_hide_billing FROM logins WHERE id=?", (me["id"],)).fetchone()
            return bool(r and r[0])
    except Exception:
        return False


def web_hide_billing(me) -> bool:
    """App store mode, second switch: the WEB My Phone portal also hides the
    Billing tab, the plan/usage box and plan wording, so a store reviewer's
    test account looks like a plain phone / push-to-talk service.
    Per login (logins.web_hide_billing) or for everyone
    (kv web_hide_billing_all). Independent of the app switch above."""
    try:
        with M.db() as c:
            r = c.execute("SELECT value FROM kv_settings WHERE key='web_hide_billing_all'").fetchone()
            if r and r[0] == "1":
                return True
            r = c.execute("SELECT web_hide_billing FROM logins WHERE id=?", (me["id"],)).fetchone()
            return bool(r and r[0])
    except Exception:
        return False


# Plan / money wording in the web portal -> plain account wording. Applied to
# the whole rendered My Phone page (ucp._render) for web-hidden users, so
# every page (Calls, Voicemail, Messages, Settings, IVR, Faxes, ...) is
# covered without threading a flag through each one. Order matters: the
# specific sentences first, the generic "your plan" fallback last.
_WEB_NEUTRAL = [
    (re.compile(r'\s*<a\b[^>]*href="/ucp/billing[^"]*"[^>]*>.*?</a>', re.S), ""),
    (re.compile(r"Outside calls need a plan\."), "Outside calls aren't available on your account."),
    (re.compile(r"You've used all your outside call minutes this month\."), "Outside calls aren't available right now."),
    (re.compile(r"You've used all your texts this month\."), "Sending texts isn't available right now."),
    (re.compile(r"You have no texts left on your plan this month\."), "Sending texts isn't available right now."),
    (re.compile(r"You've sent all the faxes your plan includes this month(?: \(resets on the 1st\))?\."),
     "Sending faxes isn't available right now."),
    (re.compile(r"It is included only when your plan has push-to-talk or your admin turns it on for you\."),
     "Your administrator can turn it on for you."),
    (re.compile(r"\s*\(through your plan or your admin\)"), ""),
    (re.compile(r";\s*both count\s+toward your plan's monthly texts\."), "."),
    (re.compile(r"Uses outside-call minutes from your plan\.\s*"), ""),
    (re.compile(r"\s*Choose a plan with fax to send and receive faxes\."), ""),
    (re.compile(r"\d[\d,]* (?:text|fax)(?:es|s)? left this month\.?\s?"), ""),
    (re.compile(r"\b(is|are)n(?:'|&#x27;|&#39;)t included in your plan"), r"\1n't available on your account"),
    (re.compile(r"on your plan(?: right now)?"), "on your account"),
    (re.compile(r"\byour plan\b"), "your account"),
]


def web_neutral(page_html: str) -> str:
    for rx, rep in _WEB_NEUTRAL:
        page_html = rx.sub(rep, page_html)
    return page_html


def neutral(me, text, hidden=None):
    """Text for the app: plan/billing wording becomes a plain 'not available'."""
    if not text:
        return text
    if hidden is None:
        hidden = hide_billing(me)
    if hidden and any(w in text.lower() for w in _PLANNY):
        return NEUTRAL + " " + account_note() + ("" if account_note().endswith(".") else ".")
    return text


def _need(request: Request):
    row = device_login(request)
    if not row:
        raise HTTPException(401, "sign in again")
    return row


def _issue(login, name: str) -> dict:
    tok = TOKEN_PREFIX + secrets.token_urlsafe(32)
    name = "".join(ch for ch in (name or "") if ch.isprintable())[:60] or "Android phone"
    with M.db() as c:
        # Keep the newest MAX_DEVICES-1, then add this one.
        old = c.execute("SELECT id FROM app_devices WHERE login_id=? ORDER BY id DESC LIMIT -1 OFFSET ?",
                        (login["id"], MAX_DEVICES - 1)).fetchall()
        c.executemany("DELETE FROM app_devices WHERE id=?", [(o["id"],) for o in old])
        top = c.execute("SELECT COALESCE(MAX(id), 0) FROM cdr").fetchone()[0]
        c.execute("INSERT INTO app_devices (login_id, name, token_hash, pwsig, expires_at, calls_seen_id)"
                  " VALUES (?,?,?,?,datetime('now', ?),?)",
                  (login["id"], name, _hash(tok), M._pw_sig(login["pwhash"]), f"+{DEVICE_TTL_DAYS} days", top))
        c.commit()
    M.ucp._audit(dict(login), "app.signin", name)
    return {"token": tok, "ttl_days": DEVICE_TTL_DAYS, "me": _who(login)}


def _who(login) -> dict:
    login = dict(login)
    return {"username": login["username"], "exten": login.get("exten") or "",
            "display_name": login.get("display_name") or "", "role": login["role"]}


# ---------------------------------------------------------------- sign in

class LoginIn(BaseModel):
    username: str
    password: str
    device_name: str = ""


class PairIn(BaseModel):
    code: str
    device_name: str = ""


def app_login(request: Request, body: LoginIn):
    import bcrypt
    ip = M.client_ip(request)
    if not M._login_allowed(ip, body.username):
        raise HTTPException(429, "Too many attempts - try again in 15 minutes.")
    with M.db() as c:
        u = c.execute("SELECT * FROM logins WHERE username=?", (body.username,)).fetchone()
    ok = bcrypt.checkpw(body.password.encode()[:72], (u["pwhash"] if u else M._DUMMY_HASH).encode())
    if not u or not ok or not u["enabled"]:
        M._log_signin(request, body.username, False)
        M._login_failed(ip, body.username)
        raise HTTPException(401, "Wrong username or password.")
    M._log_signin(request, body.username, True)
    return _issue(u, body.device_name)


def app_pair(request: Request, body: PairIn):
    ip = M.client_ip(request)
    if not M._login_allowed(ip, ""):
        raise HTTPException(429, "Too many attempts - try again in 15 minutes.")
    code = "".join(ch for ch in body.code.upper() if ch.isalnum())
    now = time.time()
    for k, (_, exp) in list(_pair_codes.items()):
        if exp < now:
            _pair_codes.pop(k, None)
    hit = _pair_codes.pop(code, None)
    if not hit:
        M._login_failed(ip, "")
        raise HTTPException(401, "That code is wrong or expired. Get a new one in My Phone > App.")
    with M.db() as c:
        u = c.execute("SELECT * FROM logins WHERE id=? AND enabled=1", (hit[0],)).fetchone()
    if not u:
        raise HTTPException(401, "That login is disabled.")
    M._log_signin(request, u["username"], True)
    return _issue(u, body.device_name)


def app_logout(request: Request):
    row = _need(request)
    with M.db() as c:
        c.execute("DELETE FROM app_devices WHERE id=?", (row["device_id"],))
        c.commit()
    return {"ok": True}


# ---------------------------------------------------------------- reads

def _paged(rows, before, limit):
    limit = max(1, min(int(limit or 100), 500))
    if before:
        rows = [r for r in rows if r["id"] < before]
    return rows[:limit]


def me_overview(request: Request):
    me = _need(request)
    U = M.ucp
    p = U._prefs(me["id"])
    box = U._mailbox(me["id"])
    with M.db() as c:
        new_vm = c.execute("SELECT COUNT(*) FROM voicemail_messages WHERE mailbox=? AND folder='INBOX'"
                           " AND is_read=0", (box or "\x00",)).fetchone()[0]
    hidden_calls = _hidden_calls(me["id"])
    hidden = hide_billing(me)
    missed_new = [x for x in U._my_calls(me, limit=200, missed_only=True)
                  if x["id"] > (me.get("calls_seen_id") or 0) and x["id"] not in hidden_calls]
    missed = [x for x in missed_new
              if x.get("started_at", "") >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))]
    devs = U._device_status(me)
    msgs = U._my_messages(me, limit=1)
    return {"me": _who(me), "dnd": bool(p["dnd"]), "new_voicemail": new_vm,
            "missed_24h": len(missed), "missed_new": len(missed_new),
            "texts_left": None if hidden else U._left(me, "messages"),
            # App store mode: no plan / usage / billing anywhere in the app.
            "hide_billing": hidden,
            "account_note": account_note() if hidden else "",
            "registered": None if devs is None else len(devs) > 0,
            "latest_message_id": msgs[0]["id"] if msgs else 0,
            "locked": U._locked(),
            # Faxes tab: shown when a fax number rings them, they have faxes, or they can send.
            "fax_enabled": _fax_enabled(me),
            "new_faxes": M.fax_ui.unseen_count(me)}


def _fax_enabled(me):
    try:
        return M.fax_ui.show_tab(me)
    except Exception:
        return False


def _peer(me, m):
    return m["to_ext"] if m["from_ext"] == me["exten"] else m["from_ext"]


def _msg(me, m):
    return {"id": m["id"], "peer": _peer(me, m), "mine": m["from_ext"] == me["exten"],
            "body": m["body"], "sent_at": m["sent_at"], "via_did": m.get("via_did") or ""}


def me_threads(request: Request):
    me = _need(request)
    threads = {}
    names = M.contacts.names_for(me["id"])
    for m in M.ucp._my_messages(me, limit=5000):
        p = _peer(me, m)
        t = threads.get(p)
        if t is None:
            threads[p] = {"peer": p, "peer_name": names.get(M.contacts.norm_number(p), ""),
                          "last": _msg(me, m), "count": 1,
                          "blocked": M.safety_ui.is_blocked(me["id"], p)}
        else:
            t["count"] += 1
    return {"threads": list(threads.values())}


def me_thread(request: Request, peer: str, before: int = 0, limit: int = 100):
    me = _need(request)
    rows = [m for m in M.ucp._my_messages(me, limit=5000) if _peer(me, m) == peer]
    return {"peer": peer, "messages": [_msg(me, m) for m in _paged(rows, before, limit)]}


def me_messages_since(request: Request, after: int = 0):
    me = _need(request)
    rows = [m for m in M.ucp._my_messages(me, limit=500) if m["id"] > after]
    return {"messages": [_msg(me, m) for m in rows]}


def _hidden_calls(login_id) -> set:
    with M.db() as c:
        return {r[0] for r in c.execute("SELECT cdr_id FROM call_hidden WHERE login_id=?", (login_id,))}


def me_calls(request: Request, missed: int = 0, view: str = "", before: int = 0, limit: int = 100):
    """view: all (default), missed, or hidden. Hidden calls are left out of
    all/missed and only listed under hidden; nothing is deleted (the call
    history is purged for everyone after 90 days)."""
    me = _need(request)
    view = view if view in ("all", "missed", "hidden") else ("missed" if missed else "all")
    hidden = _hidden_calls(me["id"])
    rows = M.ucp._my_calls(me, limit=1000, missed_only=view == "missed")
    rows = [r for r in rows if (r["id"] in hidden) == (view == "hidden")]
    keep = ("id", "kind", "other", "started_at", "duration_sec", "bill_sec", "disposition", "recording_id")
    names = M.contacts.names_for(me["id"])
    calls = []
    for r in _paged(rows, before, limit):
        d = {k: r.get(k) for k in keep if k in r}
        d["hidden"] = r["id"] in hidden
        d["peer_name"] = names.get(M.contacts.norm_number(r.get("other") or ""), "")
        calls.append(d)
    return {"view": view, "seen_id": me.get("calls_seen_id") or 0, "calls": calls}


def _latest(me) -> dict:
    """Newest ids the phone cares about: any visible text, a missed call
    the user hasn't hidden, an Inbox voicemail."""
    U = M.ucp
    msgs = U._my_messages(me, limit=1)
    hidden = _hidden_calls(me["id"])
    missed = [c["id"] for c in U._my_calls(me, limit=50, missed_only=True) if c["id"] not in hidden]
    box = U._mailbox(me["id"])
    vm = 0
    if box:
        with M.db() as c:
            vm = c.execute("SELECT COALESCE(MAX(id), 0) FROM voicemail_messages WHERE mailbox=? AND folder='INBOX'",
                           (box,)).fetchone()[0]
    notes = M.contacts.latest_note_id(me["id"])
    return {"msg": msgs[0]["id"] if msgs else 0, "call": max(missed, default=0), "vm": vm,
            "notes": notes, "fax": M.fax_ui.latest_received_id(me)}


WAIT_MAX = 55          # under Cloudflare's 100 s idle limit
WAIT_STEP = 2.0        # seconds between DB looks while waiting


async def me_wait(request: Request, msg: int = 0, call: int = 0, vm: int = 0, timeout: int = 50,
                  fax: int = -1):
    """Long poll for the app's "Instant" mode: answers as soon as there is a
    text, missed call or Inbox voicemail newer than the ids given, or after
    `timeout` seconds. Cheap: a few indexed queries every 2 s per waiting phone."""
    import asyncio
    me = _need(request)
    timeout = max(0, min(int(timeout or 0), WAIT_MAX))
    deadline = time.monotonic() + timeout
    while True:
        cur = await asyncio.to_thread(_latest, me)
        changed = (cur["msg"] > msg or cur["call"] > call or cur["vm"] > vm
                   or (fax >= 0 and cur["fax"] > fax))    # older apps don't send fax=
        if changed or time.monotonic() >= deadline or await request.is_disconnected():
            return cur | {"changed": changed}
        await asyncio.sleep(WAIT_STEP)


def _own_call_ids(me, ids) -> list:
    """Only calls that are in this user's own history."""
    mine = {r["id"] for r in M.ucp._my_calls(me, limit=5000)}
    return [i for i in ids if i in mine]


async def me_calls_hide(request: Request):
    """Form: ids=..&ids=..  (unhide=1 to show them again)."""
    me = _need(request)
    f = await request.form()
    try:
        ids = [int(x) for x in f.getlist("ids")][:1000]
    except ValueError:
        raise HTTPException(400, "bad ids")
    ids = _own_call_ids(me, ids)
    with M.db() as c:
        if f.get("unhide") == "1":
            c.executemany("DELETE FROM call_hidden WHERE cdr_id=? AND login_id=?", [(i, me["id"]) for i in ids])
        else:
            c.executemany("INSERT OR IGNORE INTO call_hidden (cdr_id, login_id) VALUES (?,?)",
                          [(i, me["id"]) for i in ids])
        c.commit()
    return {"ok": True, "code": "ok", "message": f"{len(ids)} call(s) updated.", "count": len(ids)}


async def me_calls_seen(request: Request):
    """The user looked at Calls: clear this phone's missed-call badge."""
    me = _need(request)
    with M.db() as c:
        top = c.execute("SELECT COALESCE(MAX(id), 0) FROM cdr").fetchone()[0]
        c.execute("UPDATE app_devices SET calls_seen_id=? WHERE id=?", (top, me["device_id"]))
        c.commit()
    return {"ok": True, "seen_id": top}


def me_voicemail(request: Request, folder: str = "INBOX"):
    me = _need(request)
    folder = "Old" if folder == "Old" else "INBOX"
    box = M.ucp._mailbox(me["id"])
    rows = []
    if box:
        with M.db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, caller, received_at, duration_sec, folder, is_read FROM voicemail_messages"
                " WHERE mailbox=? AND folder=? ORDER BY id DESC LIMIT 500", (box, folder)).fetchall()]
    names = M.contacts.names_for(me["id"])
    for r in rows:
        r["caller_name"] = names.get(M.contacts.norm_number(r.get("caller") or ""), "")
    return {"folder": folder, "voicemail": rows}


def me_recordings(request: Request):
    me = _need(request)
    keep = ("id", "direction", "peer", "started_at", "duration_sec", "bytes")
    names = M.contacts.names_for(me["id"])
    recs = []
    for r in M.ucp._recordings_for(me, limit=500):
        d = {k: r[k] for k in keep}
        d["peer_name"] = names.get(M.contacts.norm_number(r.get("peer") or ""), "")
        recs.append(d)
    return {"recordings": recs}


def _ptt_by_exten() -> dict:
    """{exten: effective push-to-talk} for every login.

    Effective = PERMISSION AND the user's own wish. Permission is the
    admin override flag (logins.ptt_allowed) or the plan's push_to_talk
    entitlement; the wish is logins.ptt_enabled. Empty on a
    pre-migration DB (no PTT columns yet)."""
    try:
        with M.db() as c:
            granted = set()
            try:
                granted = {r["login_id"] for r in c.execute(
                    "SELECT login_id FROM user_entitlements"
                    " WHERE feature='push_to_talk' AND quota != 0")}
                d = c.execute("SELECT value FROM kv_settings WHERE key='default_quota_push_to_talk'").fetchone()
                if d and str(d[0]).strip() not in ("", "0"):
                    granted = None  # default grants everyone
            except Exception:
                granted = set()
            return {r["exten"]: (bool(r["ptt_allowed"]) or granted is None or r["id"] in granted)
                    and bool(r["ptt_enabled"])
                    for r in c.execute("SELECT id, exten, ptt_allowed, ptt_enabled FROM logins"
                                       " WHERE exten != ''")}
    except Exception:
        return {}


def _contact_ptt(contact, ptt_map) -> bool:
    """True when this contact's number is an extension whose login can
    receive push-to-talk right now. Outside numbers never match a login."""
    return bool(ptt_map.get(M.contacts.norm_number(contact.get("number") or ""), False))


def me_contacts(request: Request, q: str = ""):
    """This user's contacts, with note counts."""
    me = _need(request)
    rows = M.contacts.contacts_for(me["id"], q=(q or "").strip()[:60])
    counts = M.contacts.note_counts(me["id"])
    ptt_map = _ptt_by_exten()
    for r in rows:
        r["notes_count"] = counts.get(r["id"], 0)
        r["last_at"] = ""
        r["ptt"] = _contact_ptt(r, ptt_map)
    # Most-recently-active first would need a scan; name order is fine and cheap.
    return {"contacts": rows}


def me_contact_detail(request: Request, contact_id: int):
    me = _need(request)
    c = M.contacts.contact_get(me["id"], contact_id)
    if not c:
        raise HTTPException(404, "no such contact")
    c["notes_count"] = M.contacts.note_counts(me["id"]).get(c["id"], 0)
    c["ptt"] = _contact_ptt(c, _ptt_by_exten())
    return {"contact": c,
            "timeline": M.contacts.timeline_for(me["id"], limit=200, contact_id=contact_id),
            "notes": M.contacts.notes_for(me["id"], contact_id=contact_id)}


def me_timeline(request: Request, limit: int = 200):
    """Unified newest-first feed: calls, texts, voicemails, recordings."""
    me = _need(request)
    limit = max(1, min(int(limit or 200), 500))
    return {"items": M.contacts.timeline_for(me["id"], limit=limit)}


def _audio(path, root):
    if not path or os.path.commonpath([os.path.abspath(path), root]) != root or not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


def me_vm_audio(request: Request, msg_id: int):
    me = _need(request)
    m = M.ucp._own_vm(me, msg_id)
    if not m:
        raise HTTPException(404)
    return _audio(m["path"], M.ucp.VM_ROOT)


def me_rec_audio(request: Request, rec_id: int):
    me = _need(request)
    if not M.ucp.can_play_recording(me, rec_id):
        raise HTTPException(404)
    with M.db() as c:
        r = c.execute("SELECT path FROM recordings WHERE id=?", (rec_id,)).fetchone()
    return _audio(r["path"] if r else "", M.ucp.REC_ROOT)


def me_settings(request: Request):
    me = _need(request)
    U = M.ucp
    p = U._prefs(me["id"])
    mp = M.safety_ui.msg_prefs(me["id"])
    _, ver = U._consent(me)
    import entitlements as ent
    with M.db() as c:
        ivrs = [dict(r) for r in c.execute("SELECT id, name FROM ivr_menus WHERE owner_login_id=?",
                                           (me["id"],)).fetchall()]
        trusted = [r[0] for r in c.execute(
            "SELECT exten FROM ptt_trusted WHERE login_id=? ORDER BY exten",
            (me["id"],)).fetchall()]
    return {
        "dnd": bool(p["dnd"]), "forward_always": p["forward_always"] or "",
        "forward_noanswer": p["forward_noanswer"] or "",
        "ring_seconds": p["ring_seconds"] or U.DEFAULT_RING, "ring_choices": list(U.RING_CHOICES),
        "answer_ivr_id": p.get("answer_ivr_id"), "ivr_menus": ivrs,
        "user_record": bool(me.get("user_wants_record")),
        "record_allowed": U._quota(me, "recording") != 0,
        "ptt_allowed": bool(me.get("ptt_allowed")) or U._quota(me, "push_to_talk") != 0,
        "user_ptt": bool(me.get("ptt_enabled")),
        "user_ptt_missed": "retry" if str(me.get("ptt_missed") or "").strip().lower() == "retry" else "voicemail",
        "ptt_trusted": trusted,
        "is_guest": bool(me.get("is_guest")),
        "guest_outbound": bool(me.get("guest_outbound")),
        "guest_inbound": bool(me.get("guest_inbound")),
        "guest_sms": bool(me.get("guest_sms")),
        "guest_sms_mode": (me.get("guest_sms_mode")
                           or ("all" if me.get("guest_sms") else "off")) if me.get("is_guest") else "all",
        "guest_upgrade": bool(me.get("guest_upgrade")),
        "guest_e911": bool(me.get("guest_e911")),
        "record_ack_needed": ver < ent.RECORDING_CONSENT_VERSION,
        "vm_email": me.get("vm_email") or "", "text_email": bool(p["text_email"]),
        "msg_in": bool(mp["msg_in"]), "msg_out": bool(mp["msg_out"]),
        "blocked": M.safety_ui.block_rules(me["id"]),
        "cell_number": p.get("cell_number") or "", "ring_cell": bool(p.get("ring_cell")),
        "cell_delay": p.get("cell_delay") or 15,
    }


def me_usage(request: Request):
    """Plan & usage for the app's "Plan & usage" screen.

    Same numbers the web portal shows: quotas/remaining come from ucp._quota
    and ucp._left (what billing.usage_panel renders on the Overview), so the
    app and the portal can never disagree. Unlimited is JSON null — for the
    quota and for what's left — matching _left returning None there. A quota
    of 0 means the feature isn't in the plan at all.
    """
    me = _need(request)
    if hide_billing(me):
        raise HTTPException(404, "Not available on this account.")
    U = M.ucp
    import entitlements as ent

    def feat(feature):
        q = U._quota(me, feature)
        with M.db() as c:
            used = ent.usage(c, me["id"], feature)
        return {
            "quota": None if q == ent.UNLIMITED else q,
            "used": used,
            "left": U._left(me, feature),
        }

    plan_name = None
    cust = M.billing._cust(me["id"])
    if cust and cust.get("plan_id"):
        plan = M.billing._plan(cust["plan_id"])
        if plan:
            plan_name = plan["name"]
    return {
        "plan": plan_name,
        "features": {
            "call_minutes": feat("call_minutes"),
            "messages": feat("messages"),
            "voicemail": feat("voicemail"),
            "recording": feat("recording"),
            "ivr_menus": feat("ivr_menus"),
            # Only when fax is in the plan; receiving is never limited.
            **({"faxes": feat("faxes")} if U._quota(me, "fax") != 0 else {}),
        },
        "includes": {
            "calls": U._quota(me, "call_minutes") != 0,
            "voicemail": U._quota(me, "voicemail") != 0,
            "messages": U._quota(me, "messages") != 0,
            "recording": U._quota(me, "recording") != 0,
            "ivr_menus": U._quota(me, "ivr_menus") != 0,
            "push_to_talk": bool(me.get("ptt_allowed")) or U._quota(me, "push_to_talk") != 0,
            "fax": U._quota(me, "fax") != 0,
        },
    }



def me_features(request: Request):
    """Features for the app's "Features" screen: just which services are on
    for this account, as on/off. No minutes, counts, limits, plan name or
    prices, so it is safe in App store mode (the Settings row store-mode
    users see instead of "Plan & usage"). Works for every user."""
    me = _need(request)
    U = M.ucp
    on = lambda f: U._quota(me, f) != 0
    items = [
        ("calls", "Calling phone numbers", on("call_minutes")),
        ("messages", "Texting", on("messages")),
        ("voicemail", "Voicemail", on("voicemail")),
        ("push_to_talk", "Push-to-talk", bool(me.get("ptt_allowed")) or on("push_to_talk")),
        ("recording", "Call recording", on("recording")),
        ("ivr_menus", "Call menus (IVR)", on("ivr_menus")),
        ("fax", "Fax (send & receive)", M.fax_send.entitled(me)),
    ]
    return {"features": [{"key": k, "label": label, "on": bool(v)} for k, label, v in items],
            "account_note": account_note() if hide_billing(me) else ""}

def app_line(me) -> str:
    """SIP username of the user's My Phone app line."""
    return "%s-app" % me["sip_username"]


def me_sip(request: Request):
    """What the app's built-in phone needs to place calls as this extension,
    through the edge (TLS + SRTP), exactly like a remote Zoiper. Only the
    signed-in user's own extension; a device token already has full access
    to that user's My Phone, and the phone secret is theirs."""
    me = _need(request)
    rs = M.network_ui.remote_settings()
    ok = bool(me.get("exten") and me.get("sip_username") and me.get("sip_secret") and rs.get("domain"))
    reason = ("" if ok else
              "Your login has no extension." if not me.get("exten") else
              "Remote calling isn't set up on this PBX yet (Admin > Network: domain)." if not rs.get("domain") else
              "Your extension has no phone password yet.")
    # The app signs in on its own line (<sip_username>-app, see pjsip.conf.j2)
    # so it never replaces the desk phone / Zoiper registration on the main line.
    return {"enabled": ok, "reason": reason,
            "exten": me.get("exten") or "", "display_name": me.get("display_name") or "",
            "username": app_line(me) if ok else "", "main_username": me.get("sip_username") or "",
            "register": True,
            "password": (me.get("sip_secret") or "") if ok else "",
            "domain": rs.get("domain") or "", "port": int(rs.get("port") or 5061),
            "transport": "tls", "srtp": "sdes", "codecs": ["PCMU", "PCMA", "G722"]}


def me_devices(request: Request):
    me = _need(request)
    with M.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, name, created_at, last_used_at, expires_at FROM app_devices"
            " WHERE login_id=? ORDER BY id DESC", (me["id"],)).fetchall()]
    for r in rows:
        r["this_device"] = r["id"] == me["device_id"]
    return {"devices": rows}


def me_device_delete(request: Request, dev_id: int):
    me = _need(request)
    with M.db() as c:
        c.execute("DELETE FROM app_devices WHERE id=? AND login_id=?", (dev_id, me["id"]))
        c.commit()
    return {"ok": True}


class PushIn(BaseModel):
    endpoint: str = ""


def me_push(request: Request, body: PushIn):
    me = _need(request)
    ep = body.endpoint.strip()
    if ep and (not ep.startswith("https://") or len(ep) > 500):
        raise HTTPException(400, "push endpoint must be an https:// URL")
    with M.db() as c:
        c.execute("UPDATE app_devices SET push_endpoint=? WHERE id=?", (ep, me["device_id"]))
        c.commit()
    return {"ok": True}


def push_notify(login_id: int, kind: str):
    """Wake this login's phones (UnifiedPush). Best effort, never raises.

    The body is only the kind of event ("message", "voicemail", ...); the
    app then fetches over the API, so no content passes the push server.
    """
    try:
        with M.db() as c:
            eps = [r[0] for r in c.execute(
                "SELECT push_endpoint FROM app_devices WHERE login_id=? AND push_endpoint != ''"
                " AND expires_at > datetime('now')", (login_id,)).fetchall()]
    except Exception:
        return
    import threading

    def send(ep):
        try:
            req = urllib.request.Request(ep, data=kind.encode(), method="POST",
                                         headers={"Content-Type": "text/plain", "TTL": "3600"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
    for ep in eps:
        threading.Thread(target=send, args=(ep,), daemon=True).start()


def push_exten(exten: str, kind: str):
    """push_notify() for whoever owns this extension."""
    try:
        with M.db() as c:
            r = c.execute("SELECT id FROM logins WHERE exten=? AND enabled=1", (exten,)).fetchone()
    except Exception:
        return
    if r:
        push_notify(r["id"], kind)


# ---------------------------------------------------------------- changes

def _flash_json(resp):
    """ucp redirect (?msg=key) -> JSON."""
    loc = resp.headers.get("location", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    key = (q.get("msg") or q.get("bmsg") or [""])[0]
    cls, text = M.ucp.FLASH.get(key, ("ok", "Done."))
    status = 200 if cls == "ok" else (423 if key == "locked" else 400)
    return JSONResponse({"ok": cls == "ok", "code": key or "ok", "message": text}, status_code=status)


def _wrap(handler):
    async def route(request: Request, **kw):
        me = _need(request)
        r = await handler(request, **kw)
        if isinstance(r, RedirectResponse):
            r = _flash_json(r)
            if hide_billing(me):
                import json as _json
                d = _json.loads(r.body)
                d["message"] = neutral(me, d.get("message", ""), True)
                return JSONResponse(d, status_code=r.status_code)
            return r
        if isinstance(r, dict):
            if "message" in r:
                r["message"] = neutral(me, r.get("message") or "")
            return r
        if isinstance(r, HTMLResponse):
            # A ucp handler answered with a page (e.g. safety lock); say so plainly.
            return JSONResponse({"ok": r.status_code < 400, "code": "page",
                                 "message": "Open My Phone in the browser for this."},
                                status_code=r.status_code)
        return r
    import inspect
    route.__name__ = "app_" + handler.__name__
    route.__signature__ = inspect.signature(handler)  # keep path params for FastAPI
    return route


async def me_password(request: Request):
    """Change the panel password. This phone stays signed in; every other
    phone and browser is signed out."""
    me = _need(request)
    r = await M.ucp.settings_password(request)
    out = _flash_json(r)
    if "msg=pw" in r.headers.get("location", "") and "msg=pwbad" not in r.headers.get("location", ""):
        with M.db() as c:
            u = c.execute("SELECT pwhash FROM logins WHERE id=?", (me["id"],)).fetchone()
            c.execute("DELETE FROM app_devices WHERE login_id=? AND id!=?", (me["id"], me["device_id"]))
            c.execute("UPDATE app_devices SET pwsig=? WHERE id=?", (M._pw_sig(u["pwhash"]), me["device_id"]))
            c.commit()
    return out


# ---------------------------------------------------------------- My Phone > App (web)

def _new_pair_code(login_id: int) -> str:
    now = time.time()
    for k, (lid, exp) in list(_pair_codes.items()):
        if exp < now or lid == login_id:
            _pair_codes.pop(k, None)
    code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
    _pair_codes[code] = (login_id, now + PAIR_TTL)
    return code


PAIR_SCHEME = "myphone"


def pair_link(server: str, code: str) -> str:
    """What the QR code holds: myphone://pair?s=<server>&c=<code>. The app
    reads it from its own scanner or when the phone camera opens the link."""
    return f"{PAIR_SCHEME}://pair?" + urllib.parse.urlencode({"s": server, "c": code})


def sip_link(server: str, user: str, password: str) -> str:
    """Phone-login QR: myphone://sip?s=<host:port>&u=<user>&p=<password>."""
    return f"{PAIR_SCHEME}://sip?" + urllib.parse.urlencode({"s": server, "u": user, "p": password})


APP_PACKAGE = "org.ownpbx.myphone"


def android_link(link: str) -> str:
    """myphone://host?q -> intent://host?q#Intent;scheme=myphone;package=...;end

    Chrome on Android ignores many plain custom-scheme links; an intent: URL
    is handed straight to the installed app (Firefox understands it too)."""
    rest = link.split("://", 1)[1]
    return f"intent://{rest}#Intent;scheme={PAIR_SCHEME};package={APP_PACKAGE};end"


def pair_qr_svg(server: str, code: str) -> str:
    """Inline SVG QR code for the pairing link, or "" if segno isn't installed."""
    return qr_svg(pair_link(server, code), "Pairing QR code")


def qr_svg(data: str, title: str = "QR code") -> str:
    """Inline SVG QR code, or "" if segno isn't installed."""
    try:
        import io
        import segno
    except ImportError:
        return ""
    buf = io.BytesIO()
    segno.make(data, error="m").save(
        buf, kind="svg", scale=6, border=2, xmldecl=False, svgns=True, dark="#000", light="#fff",
        title=title)
    return buf.getvalue().decode()


COPY_JS = """<script>
document.querySelectorAll('[data-copy]').forEach(function (b) {
  b.addEventListener('click', function () {
    var t = b.getAttribute('data-copy'), note = b.nextElementSibling;
    function say(ok) { if (note) { note.textContent = ok ? 'Copied' : 'Select and copy it'; setTimeout(function () { note.textContent = ''; }, 2000); } }
    function fallback() {
      var a = document.createElement('textarea'); a.value = t; a.setAttribute('readonly', '');
      a.style.position = 'fixed'; a.style.opacity = '0'; document.body.appendChild(a); a.select();
      var ok = false; try { ok = document.execCommand('copy'); } catch (e) {} a.remove(); say(ok);
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(t).then(function () { say(true); }, fallback);
    else fallback();
  });
});
</script>"""

COPY_BTN = ("background:none;border:1px dashed currentColor;border-radius:6px;color:inherit;"
            "cursor:pointer;padding:2px 8px;font:inherit")


def app_page(request: Request, code: str = "", sip: bool = False):
    s, me = M.ucp._me(request)
    if not s:
        return RedirectResponse("/login")
    esc = M.esc
    with M.db() as c:
        devs = c.execute("SELECT id, name, created_at, last_used_at FROM app_devices WHERE login_id=?"
                         " ORDER BY id DESC", (me["id"],)).fetchall()
    csrf = M._csrf_field(s)
    rows = "".join(
        f"<tr><td>{esc(d['name'])}</td><td>{esc(d['created_at'])}</td><td>{esc(d['last_used_at'] or '-')}</td>"
        f"<td><form method='post' action='/ucp/app/devices/{int(d['id'])}/delete'>{csrf}"
        f"<button class='btn ghost danger'>Sign out</button></form></td></tr>" for d in devs
    ) or "<tr><td colspan='4' class='muted'>No phones signed in.</td></tr>"
    shown = ""
    if code:
        server = f"{'https' if M.is_https(request) else 'http'}://{request.headers.get('host', '')}"
        qr = pair_qr_svg(server, code)
        qr_html = (f"<div style='background:#fff;padding:10px;border-radius:8px;display:inline-block;"
                   f"line-height:0'>{qr}</div>" if qr else "")
        copy_btn = COPY_BTN
        shown = (f"<div class='flash ok' style='display:flex;gap:20px;align-items:center;flex-wrap:wrap'>"
                 f"{qr_html}<div style='flex:1;min-width:240px'>"
                 + ("<b>Scan this with the My Phone app</b> (Scan QR code on the sign-in screen) "
                    "or with your phone's camera.<br><br>" if qr else "")
                 # Same phone as this page? The link opens the app with both filled in.
                 + f"<a class='btn' href='{esc(android_link(pair_link(server, code)))}' style='display:inline-block;"
                 f"text-decoration:none;text-align:center'>Open in the My Phone app</a>"
                 f"<div class='muted' style='font-size:13px;margin-top:4px'>(when this page is open on the phone itself)</div>"
                 f"<br><br>Or type it in (tap to copy):<br>"
                 f"Pairing code: <button type='button' data-copy='{esc(code)}' title='Copy'"
                 f" style='{copy_btn};font-size:22px;font-weight:700;letter-spacing:3px'>{esc(code)}</button>"
                 f" <span class='copied muted' aria-live='polite'></span><br>"
                 f"Server: <button type='button' data-copy='{esc(server)}' title='Copy'"
                 f" style='{copy_btn};font-family:monospace'>{esc(server)}</button>"
                 f" <span class='copied muted' aria-live='polite'></span><br>"
                 f"Works once, within 5 minutes.</div></div>")
    sip_box = ""
    rs = M.network_ui.remote_settings()
    can_sip = bool(me.get("exten") and me.get("sip_username") and me.get("sip_secret") and rs.get("domain"))
    if sip and can_sip:
        server = f"{rs['domain']}:{rs.get('port') or 5061}"
        link = sip_link(server, app_line(me), me["sip_secret"])
        qr = qr_svg(link)
        qr_html = (f"<div style='background:#fff;padding:10px;border-radius:8px;display:inline-block;"
                   f"line-height:0'>{qr}</div>" if qr else "")
        sip_box = (f"<div class='flash ok' style='display:flex;gap:20px;align-items:center;flex-wrap:wrap'>"
                   f"{qr_html}<div style='flex:1;min-width:240px'>"
                   "<b>Phone login for the My Phone app.</b> In the app: Settings &gt; Calling &gt; Phone login &gt; Scan QR code"
                   " (or scan it with the phone's camera). This is the app's own line on your extension"
                   " (<code>-app</code>), so your desk phone and Zoiper stay signed in. For Zoiper use your"
                   " normal username from Settings.<br><br>"
                   f"<a class='btn' href='{esc(android_link(link))}' style='display:inline-block;text-decoration:none'>"
                   "Open in the My Phone app</a><br><br>"
                   f"Server: <button type='button' data-copy='{esc(server)}' style='{COPY_BTN};font-family:monospace'>{esc(server)}</button>"
                   " <span class='copied muted'></span><br>"
                   f"Username: <button type='button' data-copy='{esc(app_line(me))}' style='{COPY_BTN};font-family:monospace'>{esc(app_line(me))}</button>"
                   " <span class='copied muted'></span><br>"
                   f"Password: <button type='button' data-copy='{esc(me['sip_secret'])}' style='{COPY_BTN};font-family:monospace'>Copy password</button>"
                   " <span class='copied muted'></span><br>"
                   "<span class='muted' style='font-size:13px'>This is your extension's phone password: anyone with it can"
                   " make calls as you. Close this page when you're done.</span></div></div>")
    sip_btn = (f'<form method="post" action="/ucp/app/sip-qr" style="display:inline">{csrf}'
               '<button class="btn ghost">Show phone login QR</button></form>' if can_sip and not sip else "")
    body = f"""{shown}{sip_box}{COPY_JS if (code or sip_box) else ""}
<p>The My Phone app for Android keeps you signed in for {DEVICE_TTL_DAYS} days after you last used it.
Changing your password signs out every phone.</p>
<form method="post" action="/ucp/app/pair-code" style="display:inline">{csrf}<button class="btn">Get a pairing code</button></form>
{sip_btn}
<h3>Signed-in phones</h3>
<div class="scrollbox"><table><tr><th>Phone</th><th>Signed in</th><th>Last used</th><th></th></tr>{rows}</table></div>"""
    resp = M.ucp._render(request, s, me, "ucp-app", "App", body)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def app_pair_code(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    code = _new_pair_code(me["id"])
    M.ucp._audit(me, "app.pair_code")
    return app_page(request, code)


async def app_sip_qr(request: Request):
    """My Phone > App > Show phone login QR (only when asked; the password is
    otherwise never shown on the web, matching Settings)."""
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    M.ucp._audit(me, "app.sip_qr")
    return app_page(request, sip=True)


async def app_device_delete_web(request: Request, dev_id: int):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    with M.db() as c:
        c.execute("DELETE FROM app_devices WHERE id=? AND login_id=?", (dev_id, me["id"]))
        c.commit()
    M.ucp._audit(me, "app.signout", f"device={dev_id}")
    return RedirectResponse("/ucp/app", status_code=303)


# ---------------------------------------------------------------- sign in with the browser
# The app opens /ucp/app/connect?st=<random> in an in-app browser tab. If
# needed the person signs in first (password or single sign-on; /login
# brings them back here), then this page makes a pairing code and hands it
# to the app: myphone://pair?s=..&c=..&st=.. . The app connects by itself only
# when st matches the sign-in it started, so a link elsewhere can't pair it.
CONNECT_PATH = "/ucp/app/connect"
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def connect_next_ok(nxt: str) -> bool:
    """Where /login may send someone back to after signing in (no open redirect)."""
    if not nxt or not nxt.startswith(CONNECT_PATH):
        return False
    rest = nxt[len(CONNECT_PATH):]
    if not rest:
        return True
    if not rest.startswith("?st="):
        return False
    return bool(_STATE_RE.match(rest[4:]))


def app_connect(request: Request):
    st = request.query_params.get("st") or ""
    if st and not _STATE_RE.match(st):
        st = ""
    here = CONNECT_PATH + (f"?st={st}" if st else "")
    s, me = M.ucp._me(request)
    if not s:
        return RedirectResponse("/login?" + urllib.parse.urlencode({"next": here}), status_code=302)
    esc = M.esc
    code = _new_pair_code(me["id"])
    M.ucp._audit(me, "app.pair_code", "browser sign-in")
    server = f"{'https' if M.is_https(request) else 'http'}://{request.headers.get('host', '')}"
    link = pair_link(server, code) + (f"&st={st}" if st else "")
    intent = android_link(link)
    body = f"""<h2>Signed in</h2>
<p>Taking you back to the My Phone app&hellip;</p>
<p><a class="btn" id="go" href="{esc(intent)}" style="display:inline-block;font-size:1.1em;padding:12px 20px">Open the My Phone app</a></p>
<p class="muted">If nothing happens, tap the button. Or type this code in the app: <b style="font-family:monospace">{esc(code)}</b>
(works once, for 5 minutes).</p>
<script>setTimeout(function () {{ location.href = document.getElementById('go').href; }}, 300);</script>"""
    return HTMLResponse(M.page("Connect the app", body, s["username"], s["role"], "ucp-app"))


# ---------------------------------------------------------------- install

def install(app_module):
    global M
    M = app_module
    app = M.app
    U, S = M.ucp, M.safety_ui
    app.post("/api/v1/app/login")(app_login)
    app.post("/api/v1/app/pair")(app_pair)
    app.post("/api/v1/app/logout")(app_logout)

    app.get("/api/v1/me/overview")(me_overview)
    app.get("/api/v1/me/threads")(me_threads)
    app.get("/api/v1/me/threads/{peer}")(me_thread)
    app.get("/api/v1/me/messages/since")(me_messages_since)
    app.get("/api/v1/me/calls")(me_calls)
    app.post("/api/v1/me/calls/hide")(me_calls_hide)
    app.post("/api/v1/me/calls/seen")(me_calls_seen)
    app.get("/api/v1/me/wait")(me_wait)
    app.get("/api/v1/me/sip")(me_sip)
    app.get("/api/v1/me/voicemail")(me_voicemail)
    app.get("/api/v1/me/voicemail/{msg_id}/audio")(me_vm_audio)
    app.get("/api/v1/me/recordings")(me_recordings)
    app.get("/api/v1/me/recordings/{rec_id}/audio")(me_rec_audio)
    app.get("/api/v1/me/contacts")(me_contacts)
    app.get("/api/v1/me/contacts/{contact_id}")(me_contact_detail)
    app.get("/api/v1/me/timeline")(me_timeline)
    app.get("/api/v1/me/settings")(me_settings)
    app.get("/api/v1/me/usage")(me_usage)
    app.get("/api/v1/me/features")(me_features)
    app.get("/api/v1/me/devices")(me_devices)
    app.delete("/api/v1/me/devices/{dev_id}")(me_device_delete)
    app.put("/api/v1/me/push")(me_push)

    # Changes: form-encoded, same fields as the web forms.
    app.post("/api/v1/me/messages/send")(_wrap(U.msg_send))
    app.post("/api/v1/me/messages/delete")(_wrap(U.msg_delete))     # ids=..&convs=..|mode=all
    app.post("/api/v1/me/voicemail/delete")(_wrap(U.vm_bulk_delete))
    app.post("/api/v1/me/voicemail/{msg_id}/heard")(_wrap(U.vm_heard))
    app.post("/api/v1/me/voicemail/{msg_id}/move")(_wrap(U.vm_move))
    app.post("/api/v1/me/recordings/delete")(_wrap(U.rec_bulk_delete))
    app.post("/api/v1/me/settings/calls")(_wrap(U.settings_calls))
    app.post("/api/v1/me/settings/prefs")(_wrap(U.settings_prefs))
    app.post("/api/v1/me/settings/messaging")(_wrap(S.save_messaging))
    app.post("/api/v1/me/settings/password")(me_password)
    app.post("/api/v1/me/dnd")(_wrap(U.dnd_toggle))
    app.post("/api/v1/me/block")(_wrap(S.block))
    app.post("/api/v1/me/report")(_wrap(S.report))
    app.post("/api/v1/me/contacts/save")(_wrap(M.contacts.contact_save))
    app.post("/api/v1/me/contacts/delete")(_wrap(M.contacts.contact_delete))
    app.post("/api/v1/me/notes/save")(_wrap(M.contacts.note_save))
    app.post("/api/v1/me/notes/delete")(_wrap(M.contacts.note_delete))

    app.get("/ucp/app", response_class=HTMLResponse)(app_page)
    app.get(CONNECT_PATH, response_class=HTMLResponse)(app_connect)
    app.post("/ucp/app/pair-code", response_class=HTMLResponse)(app_pair_code)
    app.post("/ucp/app/sip-qr", response_class=HTMLResponse)(app_sip_qr)
    app.post("/ucp/app/devices/{dev_id}/delete")(app_device_delete_web)
