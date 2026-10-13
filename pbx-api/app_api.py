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
            "SELECT d.id AS device_id, d.pwsig AS device_pwsig, l.* FROM app_devices d"
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
        c.commit()
    return dict(r)


def device_session(row: dict) -> dict:
    """Session-shaped dict the ucp handlers expect (no cookie, no CSRF:
    bearer callers are CSRF-exempt because browsers never attach them)."""
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "pwsig": M._pw_sig(row["pwhash"]), "device_id": row["device_id"]}


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
        c.execute("INSERT INTO app_devices (login_id, name, token_hash, pwsig, expires_at)"
                  " VALUES (?,?,?,?,datetime('now', ?))",
                  (login["id"], name, _hash(tok), M._pw_sig(login["pwhash"]), f"+{DEVICE_TTL_DAYS} days"))
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
    missed = [x for x in U._my_calls(me, limit=200, missed_only=True)
              if x.get("started_at", "") >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))]
    devs = U._device_status(me)
    msgs = U._my_messages(me, limit=1)
    return {"me": _who(me), "dnd": bool(p["dnd"]), "new_voicemail": new_vm,
            "missed_24h": len(missed), "texts_left": U._left(me, "messages"),
            "registered": None if devs is None else len(devs) > 0,
            "latest_message_id": msgs[0]["id"] if msgs else 0,
            "locked": U._locked()}


def _peer(me, m):
    return m["to_ext"] if m["from_ext"] == me["exten"] else m["from_ext"]


def _msg(me, m):
    return {"id": m["id"], "peer": _peer(me, m), "mine": m["from_ext"] == me["exten"],
            "body": m["body"], "sent_at": m["sent_at"], "via_did": m.get("via_did") or ""}


def me_threads(request: Request):
    me = _need(request)
    threads = {}
    for m in M.ucp._my_messages(me, limit=5000):
        p = _peer(me, m)
        t = threads.get(p)
        if t is None:
            threads[p] = {"peer": p, "last": _msg(me, m), "count": 1,
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


def me_calls(request: Request, missed: int = 0, before: int = 0, limit: int = 100):
    me = _need(request)
    rows = M.ucp._my_calls(me, limit=1000, missed_only=bool(missed))
    keep = ("id", "kind", "other", "started_at", "duration_sec", "bill_sec", "disposition", "recording_id")
    return {"calls": [{k: r.get(k) for k in keep if k in r} for r in _paged(rows, before, limit)]}


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
    return {"folder": folder, "voicemail": rows}


def me_recordings(request: Request):
    me = _need(request)
    keep = ("id", "direction", "peer", "started_at", "duration_sec", "bytes")
    return {"recordings": [{k: r[k] for k in keep} for r in M.ucp._recordings_for(me, limit=500)]}


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
    return {
        "dnd": bool(p["dnd"]), "forward_always": p["forward_always"] or "",
        "forward_noanswer": p["forward_noanswer"] or "",
        "ring_seconds": p["ring_seconds"] or U.DEFAULT_RING, "ring_choices": list(U.RING_CHOICES),
        "answer_ivr_id": p.get("answer_ivr_id"), "ivr_menus": ivrs,
        "user_record": bool(me.get("user_wants_record")),
        "record_allowed": U._quota(me, "recording") != 0,
        "record_ack_needed": ver < ent.RECORDING_CONSENT_VERSION,
        "vm_email": me.get("vm_email") or "", "text_email": bool(p["text_email"]),
        "msg_in": bool(mp["msg_in"]), "msg_out": bool(mp["msg_out"]),
        "blocked": M.safety_ui.block_rules(me["id"]),
    }


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
        _need(request)
        r = await handler(request, **kw)
        if isinstance(r, RedirectResponse):
            return _flash_json(r)
        if isinstance(r, dict):
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


def app_page(request: Request, code: str = ""):
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
        host = request.headers.get("host", "")
        shown = (f"<div class='flash ok'>Pairing code: <b style='font-size:22px;letter-spacing:3px'>"
                 f"{esc(code)}</b><br>Server: <code>{'https' if M.is_https(request) else 'http'}://{esc(host)}</code><br>"
                 f"Enter both in the app within 5 minutes. The code works once.</div>")
    body = f"""{shown}
<p>The My Phone app for Android keeps you signed in for {DEVICE_TTL_DAYS} days after you last used it.
Changing your password signs out every phone.</p>
<form method="post" action="/ucp/app/pair-code">{csrf}<button class="btn">Get a pairing code</button></form>
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
    app.get("/api/v1/me/voicemail")(me_voicemail)
    app.get("/api/v1/me/voicemail/{msg_id}/audio")(me_vm_audio)
    app.get("/api/v1/me/recordings")(me_recordings)
    app.get("/api/v1/me/recordings/{rec_id}/audio")(me_rec_audio)
    app.get("/api/v1/me/settings")(me_settings)
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

    app.get("/ucp/app", response_class=HTMLResponse)(app_page)
    app.post("/ucp/app/pair-code", response_class=HTMLResponse)(app_pair_code)
    app.post("/ucp/app/devices/{dev_id}/delete")(app_device_delete_web)
