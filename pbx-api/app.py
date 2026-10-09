#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""pbx-api: management REST API for the own-PBX.

Owns the SQLite DB. Mutating routes re-run config-gen so Asterisk always
reflects the DB. Auth is a session cookie; the password is verified here
with bcrypt (same login-page-only model as the current panel).
"""
import html
import os
import secrets
import sqlite3
import hmac
import subprocess
import urllib.parse
import logging

log = logging.getLogger("pbx-api")

import bcrypt
from fastapi import FastAPI, HTTPException, Request, Form, File, UploadFile
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from pydantic import BaseModel
from panel_templates import page, LOGIN_HTML, HIDE_ADMIN, BRANDING, BRAND_DEFAULTS, login_html, fmt_ts
import ucp
import ivr_ui
import billing
import email_ui
import mailer
import network_ui
import security
import e911_ui
import ring_groups_ui
import safety_ui
import voipms_sms


def esc(x):
    """HTML-escape a value for the server-rendered panel pages.

    MUST wrap every interpolated DB/user value. Caller ID, message bodies,
    display names etc. are attacker-influenced — unescaped they are stored
    XSS leading to full admin compromise.
    """
    return html.escape(str(x if x is not None else ""), quote=True)

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("PBX_DB", "/var/lib/pbx/pbx.db")
SCHEMA = os.path.join(BASE, "..", "db", "schema.sql")
GEN = os.path.join(BASE, "..", "config-gen", "gen.py")

app = FastAPI(title="pbx-api", version="0.1.0")
_sessions: dict[str, dict] = {}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


@app.on_event("startup")
def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with db() as c, open(SCHEMA) as f:
        c.executescript(f.read())
        _migrate(c)


def _migrate(c):
    """Add columns that CREATE TABLE IF NOT EXISTS can't add to old DBs."""
    def cols(table):
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if "ivr_id" not in cols("inbound_routes"):
        c.execute("ALTER TABLE inbound_routes ADD COLUMN ivr_id INTEGER DEFAULT NULL")
    if "ring_seconds" not in cols("user_prefs"):
        c.execute("ALTER TABLE user_prefs ADD COLUMN ring_seconds INTEGER NOT NULL DEFAULT 0")
    if "answer_ivr_id" not in cols("user_prefs"):
        c.execute("ALTER TABLE user_prefs ADD COLUMN answer_ivr_id INTEGER DEFAULT NULL")
    for col, ddl in (("call_minutes", "INTEGER NOT NULL DEFAULT 0"), ("voicemail", "INTEGER NOT NULL DEFAULT 0"),
                     ("messages", "INTEGER NOT NULL DEFAULT 0"), ("recording", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols("billing_plans"):
            c.execute(f"ALTER TABLE billing_plans ADD COLUMN {col} {ddl}")
    if "recording_consent_at" not in cols("user_prefs"):
        c.execute("ALTER TABLE user_prefs ADD COLUMN recording_consent_at TEXT DEFAULT NULL")
        c.execute("ALTER TABLE user_prefs ADD COLUMN recording_consent_version INTEGER NOT NULL DEFAULT 0")
    # When a login gets an extension number (new login or number changed),
    # remember when: the user panel only shows calls/texts/recordings matched
    # by number from then on, so a reused number never exposes the previous
    # owner's history. NULL (existing logins) = no limit.
    for col in ("text_email", "msg_in", "msg_out"):
        if col not in cols("user_prefs"):
            c.execute(f"ALTER TABLE user_prefs ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
    if "group_id" not in cols("inbound_routes"):
        c.execute("ALTER TABLE inbound_routes ADD COLUMN group_id INTEGER DEFAULT NULL")
    if "exten_since" not in cols("logins"):
        c.execute("ALTER TABLE logins ADD COLUMN exten_since TEXT DEFAULT NULL")
    c.execute("""CREATE TRIGGER IF NOT EXISTS trg_logins_exten_new AFTER INSERT ON logins
                 BEGIN UPDATE logins SET exten_since = datetime('now') WHERE id = NEW.id; END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS trg_logins_exten_change AFTER UPDATE OF exten ON logins
                 WHEN NEW.exten IS NOT OLD.exten
                 BEGIN UPDATE logins SET exten_since = datetime('now') WHERE id = NEW.id; END""")
    if "answered_login_id" not in cols("cdr"):
        c.execute("ALTER TABLE cdr ADD COLUMN answered_login_id INTEGER DEFAULT NULL")
    # Split user recording into admin allow-flag + user wish-flag, so a
    # user's opt-out sticks and an admin can never force user recording on.
    # Backfill wish=0: every user (re-)opts in via Settings -> "Record my calls".
    if "user_wants_record" not in cols("logins"):
        c.execute("ALTER TABLE logins ADD COLUMN user_wants_record INTEGER NOT NULL DEFAULT 0")
    # One-time: the previous build seeded 3 free IVR menus per user; paid
    # plans replace that (no free menus). Only touches the untouched seed.
    done = c.execute("SELECT value FROM kv_settings WHERE key='mig_ivr_free_0'").fetchone()
    if not done:
        c.execute("UPDATE kv_settings SET value='0' WHERE key='default_quota_ivr_menus' AND value='3'")
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('mig_ivr_free_0', '1')")
    # voip.ms SMS/MMS integration tables + settings (best-route)
    c.execute("""CREATE TABLE IF NOT EXISTS did_sms_routes (
      did TEXT PRIMARY KEY,
      dest_exten TEXT NOT NULL,  -- comma-separated destination extensions
      label TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS voipms_sms_dedupe (
      voipms_id TEXT PRIMARY KEY,
      received_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    for k, v in (("voipms_api_username", ""), ("voipms_api_password", ""),
                 ("voipms_webhook_token", ""), ("voipms_default_did", "")):
        c.execute("INSERT OR IGNORE INTO kv_settings (key, value) VALUES (?, ?)", (k, v))
    # Track which DID each SMS/MMS conversation is on, so replies go out
    # from the same DID the contact texted.
    if "via_did" not in cols("messages"):
        c.execute("ALTER TABLE messages ADD COLUMN via_did TEXT NOT NULL DEFAULT ''")
    # Feature codes: admin-controllable on/off + per-user access.
    c.execute("""CREATE TABLE IF NOT EXISTS feature_codes (
      code TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      enabled INTEGER NOT NULL DEFAULT 1,
      default_access TEXT NOT NULL DEFAULT 'all'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS login_feature_access (
      login_id INTEGER NOT NULL REFERENCES logins(id) ON DELETE CASCADE,
      code TEXT NOT NULL REFERENCES feature_codes(code) ON DELETE CASCADE,
      allowed INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY (login_id, code)
    )""")
    # Seed the built-in feature codes (defaults match historical behavior).
    for code, name, desc, access in (
        ("*97", "Voicemail check", "Listen to your voicemail messages", "all"),
        ("*98", "Record greeting", "Record your voicemail greeting", "all"),
        ("*555", "ChanSpy monitor", "Stay on the line and listen to calls: *555<ext> monitors one extension (auto-follows new calls), bare *555 scans any active call; * / # hops between calls", "admin"),
        ("*60", "Conference room", "Join conference room 60", "all"),
        ("*70", "DISA", "Outside dial tone after entering a PIN", "all"),
    ):
        c.execute("INSERT OR IGNORE INTO feature_codes (code, name, description, default_access)"
                  " VALUES (?,?,?,?)", (code, name, desc, access))
    c.execute("""CREATE TABLE IF NOT EXISTS invites (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token_hash TEXT NOT NULL UNIQUE,
      exten TEXT NOT NULL,
      sip_username TEXT NOT NULL,
      sip_secret TEXT NOT NULL,
      email TEXT NOT NULL DEFAULT '',
      expires_at TEXT NOT NULL,
      used_at TEXT DEFAULT NULL,
      revoked_at TEXT DEFAULT NULL,
      created_by TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    c.commit()


def as_dicts(rows):
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- sessions
# In-memory sessions with real expiry, re-checked against the DB on every
# request: a login that is disabled, deleted, has its role changed or its
# password changed loses its sessions immediately (no waiting for a restart).
SESSION_IDLE = 3 * 3600        # signed out after 3 hours without activity
SESSION_MAX = 12 * 3600        # and after 12 hours in any case
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt()).decode()


def _pw_sig(pwhash: str) -> str:
    return hashlib.sha256((pwhash or "").encode()).hexdigest()[:24]


def _new_session(u) -> str:
    import time as _t
    tok = secrets.token_hex(32)
    now = _t.time()
    _sessions[tok] = {"username": u["username"], "role": u["role"], "id": u["id"],
                      "pwsig": _pw_sig(u["pwhash"]), "created": now, "seen": now}
    if len(_sessions) > 5000:  # bound memory: drop the oldest
        for k in sorted(_sessions, key=lambda k: _sessions[k].get("seen", 0))[:1000]:
            _sessions.pop(k, None)
    return tok


def _session_for(tok: str):
    import time as _t
    s = _sessions.get(tok or "")
    if not s:
        return None
    now = _t.time()
    if now - s.get("created", 0) > SESSION_MAX or now - s.get("seen", 0) > SESSION_IDLE:
        _sessions.pop(tok, None)
        return None
    try:
        with db() as c:
            u = c.execute("SELECT id, username, role, enabled, pwhash FROM logins WHERE id=?",
                          (s.get("id"),)).fetchone()
    except Exception:
        return s
    if not u or not u["enabled"] or u["username"] != s["username"] or _pw_sig(u["pwhash"]) != s.get("pwsig"):
        _sessions.pop(tok, None)
        return None
    s["role"] = u["role"]
    s["seen"] = now
    return s


def drop_sessions(login_id: int, keep: str = ""):
    """Sign a login out everywhere (except the session token `keep`)."""
    for k, v in list(_sessions.items()):
        if v.get("id") == login_id and k != keep:
            _sessions.pop(k, None)


def current_login(request: Request) -> dict:
    s = _session_for(request.cookies.get("pbx_session") or "")
    if not s:
        raise HTTPException(401, "login required")
    return s


def require_admin(request: Request) -> dict:
    s = current_login(request)
    if s["role"] != "admin":
        raise HTTPException(403, "admin required")
    return s


def regen():
    """Re-render Asterisk configs and reload. Raises on failure."""
    apply_config()


# ---------------------------------------------------------------- auth

class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(request: Request, body: LoginIn):
    ip = client_ip(request)
    if not _login_allowed(ip, body.username):
        raise HTTPException(429, "too many attempts — try again in 15 minutes")
    with db() as c:
        u = c.execute("SELECT * FROM logins WHERE username=?",
                      (body.username,)).fetchone()
    pw_ok = bcrypt.checkpw(body.password.encode(), (u["pwhash"] if u else _DUMMY_HASH).encode())
    if not u or not pw_ok or not u["enabled"]:
        _log_signin(request, body.username, False)
        _login_failed(ip, body.username)
        raise HTTPException(401, "invalid credentials")
    _log_signin(request, body.username, True)
    tok = _new_session(u)
    resp = JSONResponse({"ok": True, "role": u["role"]})
    _set_session_cookie(request, resp, tok)
    return resp


@app.post("/auth/logout")
def logout(request: Request):
    _sessions.pop(request.cookies.get("pbx_session") or "", None)
    return {"ok": True}


@app.get("/auth/me")
def me(request: Request):
    return current_login(request)


# NOTE: the old /extensions REST API was removed when extensions were merged
# into logins (1:1). Login/extension management now lives in the /logins pages.
# If SIP-secret rotation is ever needed again, re-add it against the logins table.


# ---------------------------------------------------------------- system

@app.post("/system/reload")
async def system_reload(request: Request):
    s = require_admin(request)
    await _check_csrf(request, s)
    regen()
    return {"ok": True}


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "pbx-api"}


# ---------------------------------------------------------------- web panel

def _sess(request: Request):
    return _session_for(request.cookies.get("pbx_session") or "")


# ---------------------------------------------------------------- remote access
# The panel can be published through a Cloudflare Tunnel (often via Nginx
# Proxy Manager). Then every request arrives from the proxy's LAN address,
# so we use Cloudflare's CF-Connecting-IP header to see the real visitor.
# Cloudflare always sets (overwrites) that header, so a visitor coming from
# the internet can't remove it: its presence = "came from outside". We only
# read it when the direct peer is a private/LAN address (the proxy); a LAN
# user forging it only makes themselves "remote" (less access, never more).
#
# Remote visitors get the user panel (My Phone) only; admin pages need the
# office network unless the admin turns on "admin_remote" (Network page).
import ipaddress as _ipaddress

REMOTE_ALLOWED_PREFIXES = (
    "/ucp", "/login", "/logout", "/auth/login", "/auth/logout", "/auth/me",
    "/stripe/webhook", "/healthz", "/api/vm-greeting", "/api/voicemail-audio/",
    "/api/recording-audio/", "/api/ivr-greeting/", "/api/v1/me", "/favicon",
    "/hooks/voipms-sms", "/invite/",
)


def _peer_is_private(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        ip = _ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded",
                 "cf-ray", "cdn-loop", "x-forwarded-host")


def _public_ip(v):
    try:
        ip = _ipaddress.ip_address((v or "").strip().strip('"').split("%")[0])
    except ValueError:
        return None
    return str(ip) if ip.is_global else None


def client_ip(request: Request) -> str:
    """The real visitor address: Cloudflare's header, else the first public
    address in X-Forwarded-For / X-Real-IP (set by NPM / cloudflared), else
    the direct peer."""
    peer = request.client.host if request.client else ""
    if _peer_is_private(request):
        h = request.headers
        cf = (h.get("cf-connecting-ip") or "").strip()
        try:
            return str(_ipaddress.ip_address(cf))   # Cloudflare's header: always the visitor
        except ValueError:
            pass
        cands = (h.get("x-forwarded-for") or "").split(",") + [h.get("x-real-ip", "")]
        for c in cands:
            ip = _public_ip(c)
            if ip:
                return ip
    return peer or "?"


def is_remote(request: Request) -> bool:
    """True unless the browser connected DIRECTLY from the office network.

    Anything that came through a reverse proxy (Cloudflare Tunnel, Nginx
    Proxy Manager, ...) carries forwarding headers the proxy always adds
    (X-Forwarded-For / X-Real-IP / CF-*); a visitor can't remove them. So:
    a public peer, or ANY proxy header = remote. Office users open the
    panel directly (http://<pbx-ip>:8001) and send none of these. A LAN
    user forging one only makes themselves "remote" (less access)."""
    if not _peer_is_private(request):
        return True
    return any(request.headers.get(k) for k in PROXY_HEADERS)


def proxy_info(request: Request) -> str:
    """Short description of how this request arrived (Network page)."""
    peer = request.client.host if request.client else "?"
    seen = [k for k in PROXY_HEADERS if request.headers.get(k)]
    return f"peer {peer}" + (f", via proxy ({', '.join(seen)}), visitor {client_ip(request)}" if seen else ", direct")


def is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    if _peer_is_private(request):
        if (request.headers.get("x-forwarded-proto") or "").lower() == "https":
            return True
        if '"https"' in (request.headers.get("cf-visitor") or ""):
            return True
    return False


def _remote_path_allowed(path: str) -> bool:
    return path == "/" or any(path == p or path.startswith(p.rstrip("/") + "/") or
                              (p.endswith("/") and path.startswith(p)) for p in REMOTE_ALLOWED_PREFIXES)


MAX_BODY = 25 * 1024 * 1024    # uploads (greetings) are far smaller

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",                          # no clickjacking
    "Content-Security-Policy": "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://checkout.stripe.com https://billing.stripe.com",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


@app.middleware("http")
async def _remote_guard(request: Request, call_next):
    _login_peer.set(request.client.host if request.client else "?")
    blocked = is_remote(request) and _get_setting("admin_remote") != "1"
    HIDE_ADMIN.set(blocked)
    _load_branding()
    if blocked:
        path = request.url.path
        if path == "/":
            s = _sess(request)
            return RedirectResponse("/ucp" if s else "/login", status_code=302)
        if not _remote_path_allowed(path):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "admin access is only available on the office network"},
                                    status_code=403)
            return HTMLResponse(page("Office network only",
                                     "<h2>Office network only</h2><p>Admin pages can only be opened from the "
                                     "office network. <a href='/ucp'>Go to My Phone</a></p>"), status_code=403)
    return await call_next(request)


# Added after _remote_guard = outermost: headers also go on its 403 pages.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"detail": "request too large"}, status_code=413)
    except ValueError:
        return JSONResponse({"detail": "bad content-length"}, status_code=400)
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if request.url.path.startswith(("/api/", "/ucp", "/login")) or request.url.path == "/":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _set_session_cookie(request: Request, resp, tok: str):
    resp.set_cookie("pbx_session", tok, httponly=True, samesite="lax",
                    secure=is_https(request), max_age=12 * 3600)


# Login throttling (in memory; resets on restart):
#  - per visitor IP: 10 failures in 15 minutes -> blocked for 15 minutes
#  - per username:  20 failures in 15 minutes -> that name blocked for
#    15 minutes from everywhere (stops slow guessing from many addresses)
_login_failures: dict[str, list[float]] = {}
import contextvars as _cv
_login_peer = _cv.ContextVar("login_peer", default="?")  # direct peer of the current request
LOGIN_WINDOW = 900
LOGIN_MAX_PER_IP = 10
LOGIN_MAX_PER_USER = 20
LOGIN_MAX_PER_PEER = 100   # per proxy/peer address: caps forged X-Forwarded-For rotation


def _login_allowed(ip: str, username: str = "") -> bool:
    import time
    now = time.time()
    ok = True
    for key, limit in ((f"ip:{ip}", LOGIN_MAX_PER_IP),
                       (f"user:{username.strip().lower()}", LOGIN_MAX_PER_USER),
                       (f"peer:{_login_peer.get()}", LOGIN_MAX_PER_PEER)):
        if key == "user:":
            continue
        hits = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW]
        if hits:
            _login_failures[key] = hits
        else:
            _login_failures.pop(key, None)
        if len(hits) >= limit:
            ok = False
    return ok


def _login_failed(ip: str, username: str = ""):
    import time
    now = time.time()
    _login_failures.setdefault(f"ip:{ip}", []).append(now)
    _login_failures.setdefault(f"peer:{_login_peer.get()}", []).append(now)
    n_ip = len([t for t in _login_failures[f"ip:{ip}"] if now - t < LOGIN_WINDOW])
    n_user = 0
    if username.strip():
        uk = f"user:{username.strip().lower()}"
        _login_failures.setdefault(uk, []).append(now)
        n_user = len([t for t in _login_failures[uk] if now - t < LOGIN_WINDOW])
    # Report the moment a lockout starts (not every blocked attempt after).
    if n_ip == LOGIN_MAX_PER_IP:
        security.record("signin_lockout", ip, f"{n_ip} wrong passwords in 15 min (last tried: "
                        f"{username.strip()[:40] or '-'}); this address is blocked for 15 min")
    if n_user == LOGIN_MAX_PER_USER:
        security.record("signin_lockout", ip, f"login '{username.strip()[:40]}' had {n_user} wrong "
                        "passwords in 15 min from several addresses; that login is blocked for 15 min")
    if len(_login_failures) > 5000:  # bound memory under a spray attack
        for k in list(_login_failures)[:1000]:
            _login_failures.pop(k, None)


def _csrf_token(s: dict) -> str:
    """Per-session CSRF token, created on demand."""
    tok = s.get("csrf")
    if not tok:
        tok = secrets.token_hex(16)
        s["csrf"] = tok
    return tok


def _csrf_field(s: dict) -> str:
    """Hidden form field carrying the CSRF token."""
    return f'<input type="hidden" name="csrf_token" value="{_csrf_token(s)}">'


async def _check_csrf(request: Request, s: dict | None = None):
    """Reject cross-site forged POSTs.

    Bearer-token callers are exempt (browsers don't auto-attach
    Authorization headers). Session-cookie callers must present the
    per-session token as a form field or X-CSRF-Token header.
    """
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    s = s if s is not None else _sess(request)
    if not s:
        raise HTTPException(401, "login required")
    want = s.get("csrf") or ""
    got = request.headers.get("x-csrf-token", "")
    if not got and request.method == "POST":
        try:
            form = await request.form()
            got = form.get("csrf_token", "")
        except Exception:
            got = ""
    if not want or not secrets.compare_digest(str(got), want):
        raise HTTPException(403, "CSRF token missing or invalid")


def _check_csrf_v1(request: Request):
    """CSRF for v1 JSON endpoints: Bearer callers exempt; session-cookie
    callers (dashboard JS) must send the X-CSRF-Token header."""
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    s = _sess(request)
    want = (s or {}).get("csrf") or ""
    got = request.headers.get("x-csrf-token", "")
    if not want or not secrets.compare_digest(str(got), want):
        raise HTTPException(403, "CSRF token missing or invalid")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return page("Login", login_html())


@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = client_ip(request)
    if not _login_allowed(ip, username):
        return HTMLResponse(page("Login", login_html() + "<p style='color:red'>Too many attempts — try again in 15 minutes.</p>"), status_code=429)
    with db() as c:
        u = c.execute("SELECT * FROM logins WHERE username=?",
                      (username,)).fetchone()
    pw_ok = bcrypt.checkpw(password.encode(), (u["pwhash"] if u else _DUMMY_HASH).encode())
    if not u or not pw_ok or not u["enabled"]:
        _log_signin(request, username, False)
        _login_failed(ip, username)
        return HTMLResponse(page("Login", login_html() + "<p style='color:red'>Invalid credentials</p>"), status_code=401)
    _log_signin(request, username, True)
    tok = _new_session(u)
    dest = "/" if u["role"] == "admin" else "/ucp"
    resp = RedirectResponse(dest, status_code=302)
    _set_session_cookie(request, resp, tok)
    return resp


@app.get("/logout")
def logout_page(request: Request):
    _sessions.pop(request.cookies.get("pbx_session") or "", None)
    return RedirectResponse("/login")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    s = _sess(request)
    if not s:
        return RedirectResponse("/login")
    if s["role"] != "admin":
        return RedirectResponse("/ucp")
    # Thin consumer of the v1 JSON API (same endpoints external tools use).
    # Refreshes live every 10s; transient errors leave the last good state.
    body = """<h2>Dashboard</h2>
<div id="safety" style="margin-bottom:16px"></div>
<div class="stats" id="stats"></div>
<h3>Live calls</h3>
<div class="scrollbox"><table><tr><th>From</th><th>To</th><th>Type</th><th>State</th><th>Duration</th></tr>
<tbody id="calls"><tr><td colspan="5" class="muted">Loading…</td></tr></tbody></table></div>
<h3>Signed in now <span class="muted" style="font-weight:400" id="dev-note"></span></h3>
<div class="scrollbox"><table><tr><th>Device</th><th>Ext</th><th>IP address</th><th>Where</th><th>Connection</th><th>Status</th><th>RTT</th></tr>
<tbody id="devices"><tr><td colspan="7" class="muted">Loading…</td></tr></tbody></table></div>
<h3>Recent sign-ins <span class="muted" style="font-weight:400">last 24 hours, older entries are removed automatically</span></h3>
<div class="bulkbar">
<button class="btn ghost" id="si-delsel" disabled onclick="deleteSignins(false)">Delete selected</button>
<button class="btn ghost danger" id="si-delall" onclick="deleteSignins(true)">Delete all</button>
<span class="muted" id="si-note"></span>
</div>
<div class="scrollbox"><table><tr><th><input type="checkbox" id="si-all" aria-label="Select all"></th><th>Login</th><th>IP</th><th>Result</th><th>When</th></tr>
<tbody id="signins"><tr><td colspan="5" class="muted">Loading…</td></tr></tbody></table></div>
<h3>Activity <span class="muted" style="font-weight:400">security events and changes, last 24 hours (older entries are removed automatically)</span></h3>
<div class="bulkbar">
<label class="muted"><input type="checkbox" id="act-sec"> Security events only</label>
<span class="muted" id="act-note"></span>
</div>
<div class="scrollbox"><table><tr><th>When</th><th>Event</th><th>IP</th><th>By</th><th>Details</th></tr>
<tbody id="activity"><tr><td colspan="5" class="muted">Loading…</td></tr></tbody></table></div>
<script>
const CSRF_TOKEN = "__CSRF__";
async function jget(u) {
  const r = await fetch(u, {credentials: 'same-origin'});
  if (!r.ok) throw new Error(u + ' -> ' + r.status);
  return r.json();
}
function esc(x) {
  return String(x ?? '').replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}
function ago(ts) {
  const t = new Date(String(ts).replace(' ', 'T') + 'Z').getTime();
  if (isNaN(t)) return String(ts ?? '');
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return Math.floor(s) + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}
function agoEpoch(sec) {
  if (!sec) return '-';
  return ago(new Date(sec * 1000).toISOString().replace('T', ' ').slice(0, 19));
}
function stat(n, label) {
  return '<div class="stat"><b>' + n + '</b><span>' + label + '</span></div>';
}
async function safetyAction(url, body) {
  const r = await fetch(url, {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF_TOKEN},
    body: JSON.stringify(body)});
  if (!r.ok) { alert('Failed: HTTP ' + r.status); return; }
  refresh();
}
// Each section loads independently: one failing source (e.g. the brain or
// Asterisk status being down) must not blank the whole dashboard. A section
// that fails shows the reason inline; one that failed before but has good
// data keeps it and gets a "stale" note.
async function jgetDetail(u) {
  const r = await fetch(u, {credentials: 'same-origin'});
  if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
  if (!r.ok) {
    let msg = 'HTTP ' + r.status;
    try { const j = await r.json(); if (j.detail) msg += ': ' + j.detail; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}
const LOADED = {};
function failRow(id, cols, what, e) {
  const el = document.getElementById(id);
  const msg = '<span class="bad">Could not load ' + what + '</span> <span class="muted">' + esc(e.message || e) + '</span>';
  if (LOADED[id]) {
    // keep last good rows, flag them stale
    let note = el.querySelector('tr.stale');
    if (!note) { note = document.createElement('tr'); note.className = 'stale'; el.prepend(note); }
    note.innerHTML = '<td colspan="' + cols + '">' + msg + ' (showing last known data)</td>';
  } else {
    el.innerHTML = '<tr><td colspan="' + cols + '">' + msg + '</td></tr>';
  }
}
const STATS = {devices: '-', calls: '-', ok24: '-', fail24: '-', total: 0, blocked: '-'};
function renderStats() {
  document.getElementById('stats').innerHTML =
    stat(STATS.calls, 'live calls') + stat(STATS.devices, 'devices online') +
    stat(STATS.ok24, 'sign-ins, 24h') + stat(STATS.fail24, 'failed, 24h') +
    stat(STATS.blocked, 'IPs blocked, 24h');
}
async function loadSafety() {
  try {
    const sz = await jgetDetail('/api/v1/safety');
    const ks = sz.kill_switch, lock = sz.safety_lock;
    try {
      const ps = await jgetDetail('/api/v1/safety/pin/status');
      PIN_STATE = {set: !!ps.pin_set, unlocked: !!ps.unlocked};
    } catch (e) { PIN_STATE = {set: false, unlocked: true}; }
    document.getElementById('safety').innerHTML =
      '<div class="safety-bar' + (ks ? ' engaged' : '') + '">' +
      '<strong>Status:</strong> ' +
      (ks ? '<span class="bad"><strong>KILL SWITCH ENGAGED</strong> — all calls halted, no registrations</span>'
          : '<span class="ok">Active</span>') +
      ' &nbsp;·&nbsp; Safety lock: <strong>' + (lock ? 'ON' : 'off') + '</strong>' +
      ' &nbsp;<button class="btn" onclick="if (confirm(\\'' + (ks ? 'Release the kill switch? Phones will register again.' : 'KILL SWITCH: hang up every call and disconnect all phones?') + '\\')) safetyAction(\\'/api/v1/safety/kill-switch\\',{engaged:' + (!ks) + '})">' +
        (ks ? 'Release kill switch' : 'Kill switch') + '</button>' +
      ' <button class="btn" onclick="safetyAction(\\'/api/v1/safety/lock\\',{locked:' + (!lock) + '})">' +
        (lock ? 'Unlock' : 'Lock') + '</button>' +
      ' &nbsp;·&nbsp; Edit PIN: <strong>' + (PIN_STATE.set ? (PIN_STATE.unlocked ? 'unlocked' : 'locked') : 'off') + '</strong>' +
      (PIN_STATE.set && !PIN_STATE.unlocked ? ' <button class="btn" onclick="pinUnlockNow()">Unlock with PIN</button>' : '') +
      ' <button class="btn ghost" onclick="pinSetChange()">' + (PIN_STATE.set ? 'Change PIN' : 'Set PIN') + '</button>' +
      '</div>';
  } catch (e) {
    document.getElementById('safety').innerHTML =
      '<div class="bad">Could not load safety status: ' + esc(e.message || e) + '</div>';
  }
}
let PIN_STATE = {set: false, unlocked: true};
async function pinUnlockNow() {
  const pin = prompt('Enter edit PIN:');
  if (!pin) return;
  const r = await fetch('/api/v1/safety/pin/unlock', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF_TOKEN},
    body: JSON.stringify({pin: pin})});
  if (!r.ok) { alert(r.status === 429 ? 'Too many attempts — try again later.' : 'Wrong PIN.'); return; }
  refresh();
}
async function pinSetChange() {
  let oldPin = '';
  if (PIN_STATE.set) {
    oldPin = prompt('Enter current PIN (cancel to abort):');
    if (!oldPin) return;
  }
  const pin = prompt('New edit PIN — 6 to 12 digits. Leave empty to remove the PIN:');
  if (pin === null) return;
  if (pin !== '' && !/^[0-9]{6,12}$/.test(pin)) { alert('PIN must be 6-12 digits.'); return; }
  const r = await fetch('/api/v1/safety/pin/set', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF_TOKEN},
    body: JSON.stringify({pin: pin, old_pin: oldPin})});
  if (!r.ok) { alert(r.status === 403 ? 'Current PIN is wrong.' : 'Failed: HTTP ' + r.status); return; }
  alert(pin === '' ? 'Edit PIN removed.' : 'Edit PIN set. The panel is now locked until you unlock it.');
  refresh();
}
async function loadDevices() {
  try {
    const res = await jgetDetail('/api/v1/devices');
    const devices = res.devices || [];
    STATS.devices = devices.length;
    const cnt = res.counts || {};
    document.getElementById('dev-note').textContent = devices.length ?
      (cnt.internal || 0) + ' internal · ' + (cnt.external || 0) + ' external' : '';
    document.getElementById('devices').innerHTML = devices.length ? devices.map(d =>
      '<tr><td>' + esc(d.display_name || d.username) +
      ' <span class="muted">' + esc(d.username) + '</span></td>' +
      '<td>' + esc(d.exten) + '</td>' +
      '<td>' + esc(d.ip) + (d.port ? '<span class="muted">:' + esc(d.port) + '</span>' : '') + '</td>' +
      '<td><span class="pill ' + (d.location === 'external' ? 'warn' : 'ok') + '">' +
        (d.location === 'external' ? 'External' : 'Internal') + '</span></td>' +
      '<td>' + esc(d.via || d.transport || '-') + '</td>' +
      '<td class="' + ({Avail: 'ok', Reachable: 'ok', Unavail: 'bad', Unreachable: 'bad'}[d.status] || 'muted') + '">' +
        esc({Avail: 'Online', Unavail: 'Not answering', NonQual: 'Registered', Unknown: 'Checking…'}[d.status] || d.status) + '</td>' +
      '<td>' + (d.rtt_ms == null ? '-' : Number(d.rtt_ms).toFixed(1) + ' ms') + '</td></tr>'
    ).join('') : '<tr><td colspan="7" class="muted">No devices registered</td></tr>';
    LOADED.devices = true;
  } catch (e) { failRow('devices', 7, 'devices', e); }
}
function dur(epoch) {
  if (!epoch) return '-';
  let s = Math.max(0, Math.floor(Date.now() / 1000 - Number(epoch)));
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}
const CALL_TYPE = {inbound: 'Incoming', outbound: 'Outgoing', internal: 'Internal', forwarded: 'Forwarded', emergency: '🚨 911', spy: '👁 Spy'};
async function loadCalls() {
  try {
    const calls = (await jgetDetail('/api/v1/calls/live')).calls || [];
    STATS.calls = calls.length;
    document.getElementById('calls').innerHTML = calls.length ? calls.map(c => {
      const isSpy = c.direction === 'spy';
      const stateLabel = isSpy ? (c.state === 'bridged' ? 'Listening' : 'Waiting')
                               : (c.state === 'bridged' ? 'Talking' : 'Ringing');
      return '<tr><td><strong>' + esc(c.from_label || c.caller) + '</strong></td>' +
      '<td>' + esc(c.to_label || c.callee) + '</td>' +
      '<td>' + esc(CALL_TYPE[c.direction] || c.direction || 'Internal') + '</td>' +
      '<td class="' + (c.state === 'bridged' ? 'ok' : 'warn') + '">' + stateLabel + '</td>' +
      '<td>' + (c.state === 'bridged' ? dur(c.bridged_at) : dur(c.started_at)) + '</td></tr>';
    }).join('') : '<tr><td colspan="5" class="muted">No calls right now</td></tr>';
    LOADED.calls = true;
  } catch (e) { failRow('calls', 5, 'live calls', e); }
}
// Selected sign-in ids survive the 10s refresh.
const SI_SEL = new Set();
let SI_IDS = [];
function siSync() {
  SI_IDS.forEach(id => { const b = document.querySelector('input.si[value="' + id + '"]'); if (b) b.checked = SI_SEL.has(id); });
  const n = SI_SEL.size, all = document.getElementById('si-all');
  const btn = document.getElementById('si-delsel');
  btn.disabled = !n; btn.textContent = n ? 'Delete selected (' + n + ')' : 'Delete selected';
  all.checked = n > 0 && SI_IDS.length > 0 && SI_IDS.every(id => SI_SEL.has(id));
  all.indeterminate = n > 0 && !all.checked;
  document.getElementById('si-delall').disabled = !SI_IDS.length;
}
document.getElementById('si-all').addEventListener('change', e => {
  SI_IDS.forEach(id => e.target.checked ? SI_SEL.add(id) : SI_SEL.delete(id)); siSync();
});
document.getElementById('signins').addEventListener('change', e => {
  if (!e.target.classList.contains('si')) return;
  const id = Number(e.target.value); e.target.checked ? SI_SEL.add(id) : SI_SEL.delete(id); siSync();
});
async function deleteSignins(all) {
  const n = all ? STATS.total : SI_SEL.size;
  if (!confirm(all ? 'Delete ALL sign-in entries (' + n + ')?' : 'Delete ' + n + ' selected sign-in entr' + (n === 1 ? 'y' : 'ies') + '?')) return;
  const r = await fetch('/api/v1/signins/delete', {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF_TOKEN},
    body: JSON.stringify(all ? {all: true} : {ids: [...SI_SEL]})});
  let msg = 'Failed: HTTP ' + r.status;
  if (r.ok) { msg = 'Deleted ' + (await r.json()).deleted + '.'; SI_SEL.clear(); }
  else { try { const j = await r.json(); if (j.detail) msg += ' ' + j.detail; } catch (_) {} }
  document.getElementById('si-note').textContent = msg;
  await loadSignins(); renderStats();
}
async function loadSignins() {
  try {
    const res = await jgetDetail('/api/v1/signins?limit=200');
    const signins = res.signins || [];
    STATS.ok24 = res.counts ? res.counts.ok_24h : signins.filter(x => x.ok).length;
    STATS.fail24 = res.counts ? res.counts.failed_24h : signins.filter(x => !x.ok).length;
    STATS.total = res.counts ? res.counts.total : signins.length;
    SI_IDS = signins.map(x => x.id);
    [...SI_SEL].forEach(id => { if (!SI_IDS.includes(id)) SI_SEL.delete(id); });
    document.getElementById('signins').innerHTML = signins.map(x =>
      '<tr><td><input type="checkbox" class="si" value="' + Number(x.id) + '" aria-label="Select"></td>' +
      '<td>' + esc(x.login) + '</td><td>' + esc(x.ip || '-') + '</td>' +
      '<td class="' + (x.ok ? 'ok' : 'bad') + '">' + (x.ok ? 'Signed in' : 'Failed') + '</td>' +
      '<td>' + ago(x.at) + '</td></tr>'
    ).join('') || '<tr><td colspan="5" class="muted">No sign-ins in the last 24 hours</td></tr>';
    siSync();
    LOADED.signins = true;
  } catch (e) { failRow('signins', 5, 'sign-ins', e); }
}
const ACT_CLASS = {ban: 'bad', flood: 'bad', signin_lockout: 'bad', kill_switch: 'bad', emergency: 'bad', e911_unlock_fail: 'bad', e911_change: 'warn', report: 'warn',
                   safety_lock: 'warn', unban: 'muted', test: 'muted'};
async function loadActivity() {
  try {
    const res = await jgetDetail('/api/v1/activity?limit=100');
    STATS.blocked = res.blocked_24h;
    const secOnly = document.getElementById('act-sec').checked;
    const items = (res.items || []).filter(x => !secOnly || x.type === 'security');
    document.getElementById('act-note').textContent = res.alerts_to ? '' :
      'Email alerts are off: set an address under Email → Security alerts.';
    document.getElementById('activity').innerHTML = items.map(x =>
      '<tr><td>' + ago(x.at) + '</td>' +
      '<td class="' + (x.type === 'security' ? (ACT_CLASS[x.kind] || '') : '') + '">' +
        (x.type === 'security' ? '<strong>' + esc(x.title) + '</strong>' +
          (x.source === 'edge' ? ' <span class="muted">edge</span>' : '') : esc(x.title)) + '</td>' +
      '<td>' + esc(x.ip || '') + '</td><td>' + esc(x.actor || '') + '</td>' +
      '<td>' + esc(x.detail || '') + '</td></tr>'
    ).join('') || '<tr><td colspan="5" class="muted">Nothing yet</td></tr>';
    LOADED.activity = true;
  } catch (e) { failRow('activity', 5, 'activity', e); }
}
document.getElementById('act-sec').addEventListener('change', loadActivity);
async function refresh() {
  await Promise.all([loadSafety(), loadDevices(), loadCalls(), loadSignins(), loadActivity()]);
  renderStats();
}
refresh();
setInterval(refresh, 10000);
</script>"""
    body = body.replace("__CSRF__", _csrf_token(s))
    return page("Dashboard", body, s["username"], s["role"], "dash")


@app.get("/me", response_class=HTMLResponse)
def me_page(request: Request):
    # Replaced by the user control panel (ucp.py); keep old links working.
    return RedirectResponse("/ucp")


@app.get("/logins", response_class=HTMLResponse)
def logins_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        rows = c.execute("SELECT id, username, role, exten, sip_username, display_name, enabled FROM logins ORDER BY exten").fetchall()
    tr = "".join(
        f"<tr><td>{esc(r['username'])}</td><td>{esc(r['role'])}</td><td>{esc(r['exten'])}</td>"
        f"<td>{esc(r['sip_username'])}</td><td>{esc(r['display_name'])}</td><td>{'yes' if r['enabled'] else 'no'}</td>"
        f"<td><a class='btn ghost' href='/logins/{r['id']}/edit'>Edit</a>"
        + ("" if r['id'] == s.get('id') else
           f" <form method='post' action='/logins/{r['id']}/delete' style='display:inline' "
           f"onsubmit=\"return confirm('Permanently delete login \\'{esc(r['username'])}\\' (ext {esc(r['exten']) or '-'}) "
           f"and ALL of their data — call logs, messages, voicemail, recordings? This cannot be undone.')\">"
           f"{_csrf_field(s)}<button class='btn ghost' type='submit'>Delete</button></form>")
        + "</td></tr>" for r in rows)
    body = f"<h2>Logins / Extensions</h2><table><tr><th>Username</th><th>Role</th><th>Exten</th><th>SIP user</th><th>Name</th><th>Enabled</th><th></th></tr>{tr}</table>"
    body += "<p><a href='/logins/new/edit' class='btn'>Add login</a></p>"
    return page("Logins", body, s["username"], s["role"], "logins")


@app.get("/logins/{lid}/edit", response_class=HTMLResponse)
def login_edit_page(request: Request, lid: str):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    t = None
    if lid != "new":
        with db() as c:
            t = c.execute("SELECT * FROM logins WHERE id=?", (lid,)).fetchone()
        if not t:
            return RedirectResponse("/logins")
    def v(k, d=""):
        return esc(t[k] if t and t[k] is not None else d)
    body = f"""
<h2>{'Edit' if t else 'Add'} login</h2>
<form method="post" action="/logins/{lid}/edit" onsubmit="return confirmExtenChange()">
{_csrf_field(s)}
<label>Username<br><input name="username" value="{v('username')}" required></label><br>
<label>Password<br><input name="password" type="password" placeholder="{'(unchanged)' if t else ''}" {'required' if not t else ''}></label><br>
<label>Role<br><select name="role"><option value="user" {'selected' if v('role','user')=='user' else ''}>user</option><option value="admin" {'selected' if v('role')=='admin' else ''}>admin</option></select></label><br>
<label>Extension<br><input name="exten" id="exten" value="{v('exten')}" placeholder="e.g. 8800"></label>
<p class="muted" style="color:#b31d1d;max-width:560px;margin:2px 0 6px">&#9888; {"Changing" if t else "Assigning"} the extension permanently deletes all call logs, SMS/MMS, voicemails and recordings tied to the old and new extension numbers. This cannot be undone.</p><br>
<label>SIP username<br><input name="sip_username" value="{v('sip_username')}" placeholder="defaults to exten"></label><br>
<label>SIP secret<br><input name="sip_secret" type="password" placeholder="{'(unchanged)' if t and v('sip_secret') else ''}"></label><br>
<label>Display name<br><input name="display_name" value="{v('display_name')}" size="30"></label><br>
<label>Voicemail email<br><input name="vm_email" value="{v('vm_email')}" size="30"></label><br>
<label><input type="checkbox" name="record_admin" {'checked' if t and t['record_admin'] else ''}> Admin call recording</label>
<span class="muted">{'(on system-wide)' if _get_setting("admin_rec_enabled") == "1" else '(off system-wide - nothing is recorded until it is turned on under <a href="/recordings">Recordings</a>)'}</span><br>
<label><input type="checkbox" name="user_record" {'checked' if t and t['user_record'] else ''}> User call recording (allow)</label>
<span class="muted">Lets the user record their own calls. They must still turn on "Record my calls" in Settings themselves — this cannot enable it for them. Uncheck to disable user recording entirely.</span><br>
<p class="muted" style="max-width:560px">Recording laws vary; some US states (e.g. California, Florida, Illinois, Maryland,
Massachusetts, Pennsylvania, Washington) require every party's consent. "User call recording" only records once the user has
accepted the recording notice in their control panel and their plan includes recording. Admin recording is your responsibility
to use lawfully.</p>
<label><input type="checkbox" name="enabled" {'checked' if not t or t['enabled'] else ''}> Enabled</label><br><br>
<button class="btn" type="submit">Save</button>
<a class="btn ghost" href="/logins">Cancel</a>
</form>
<script>
function confirmExtenChange(){{
  var el=document.getElementById('exten');
  if(el && el.value.trim()!==el.defaultValue.trim()){{
    return confirm('Change extension from "'+el.defaultValue+'" to "'+el.value.trim()+'"?\n\nThis PERMANENTLY DELETES all call logs, SMS/MMS, voicemails and recordings for BOTH extensions. This cannot be undone.');
  }}
  return true;
}}
</script>"""
    return page("Edit login", body, s["username"], s["role"], "logins")


@app.post("/logins/{lid}/edit")
async def login_edit_save(request: Request, lid: str,
                    username: str = Form(...), password: str = Form(""),
                    role: str = Form("user"), exten: str = Form(""),
                    sip_username: str = Form(""), sip_secret: str = Form(""),
                    display_name: str = Form(""), vm_email: str = Form(""),
                    record_admin: str = Form(None), user_record: str = Form(None),
                    enabled: str = Form(None)):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "logins")
    await _check_csrf(request, s)
    import subprocess
    new_id, old_exten = None, ""
    with db() as c:
        if lid == "new":
            pwh = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            sip_u = sip_username or exten or username
            if sip_u:
                sip_u = _valid_name(sip_u, "sip_username")
            if exten and not sip_secret:
                raise HTTPException(400, "SIP secret is required when an extension is set")
            cur = c.execute("""INSERT INTO logins (username, pwhash, role, exten, sip_username, sip_secret,
                       display_name, vm_email, record_admin, user_record, enabled)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (username, pwh, role, exten or "", sip_u, sip_secret or "",
                       display_name or "", vm_email or "",
                       1 if record_admin else 0, 1 if user_record else 0,
                       1 if enabled else 0))
            new_id = cur.lastrowid
            # Create their voicemail box
            if exten:
                c.execute("INSERT OR IGNORE INTO voicemail_boxes (mailbox, login_id) VALUES (?,?)",
                          (f"vm-{exten}", new_id))
        else:
            old = c.execute("SELECT exten FROM logins WHERE id=?", (lid,)).fetchone()
            old_exten = (old["exten"] if old else "") or ""
            sets, vals = [], []
            sip_username = _valid_name(sip_username, "sip_username") if sip_username else ""
            for k, val in [("username", username), ("role", role), ("exten", exten or ""),
                           ("sip_username", sip_username), ("display_name", display_name or ""),
                           ("vm_email", vm_email or "")]:
                sets.append(f"{k}=?"); vals.append(val)
            sets.append("record_admin=?"); vals.append(1 if record_admin else 0)
            sets.append("user_record=?"); vals.append(1 if user_record else 0)
            sets.append("enabled=?"); vals.append(1 if enabled else 0)
            if password:
                sets.append("pwhash=?"); vals.append(bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())
            if sip_secret:
                sets.append("sip_secret=?"); vals.append(sip_secret)
            vals.append(lid)
            c.execute(f"UPDATE logins SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
    # Privacy clean slate on (re)assignment. Changing a login's extension
    # purges all user data for BOTH the old and new extension; a brand-new
    # login purges the new extension's previous holder data.
    new_exten = (exten or "").strip()
    if lid == "new":
        if new_exten:
            purged = ucp.purge_extension_data([new_exten])
            _audit_log(s["username"], "logins.exten_purge",
                       f"new login {new_id} on ext {new_exten}: " +
                       ", ".join(f"{k}={v}" for k, v in purged.items()))
    elif old_exten != new_exten:
        purged = ucp.purge_extension_data([old_exten, new_exten], moving_login_id=int(lid))
        _audit_log(s["username"], "logins.exten_purge",
                   f"login {lid}: {old_exten or '(none)'} -> {new_exten or '(none)'}: " +
                   ", ".join(f"{k}={v}" for k, v in purged.items()))
    # Regenerate pjsip.conf for the new/changed extension (best effort;
    # retry via POST /system/reload if this fails)
    _best_effort_apply()
    return RedirectResponse("/logins", status_code=302)


@app.post("/logins/{lid}/delete", response_class=HTMLResponse)
async def login_delete(request: Request, lid: str):
    """Permanently delete a login, their SIP endpoint, and all their data."""
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "logins")
    with db() as c:
        t = c.execute("SELECT * FROM logins WHERE id=?", (lid,)).fetchone()
        if not t:
            return RedirectResponse("/logins", status_code=303)
        lid_i = int(t["id"])
        if lid_i == int(s.get("id") or -1):
            return page("Cannot delete login",
                        "<h2>Cannot delete login</h2>"
                        "<p class='warn'>You cannot delete the login you are currently signed in with.</p>"
                        "<p><a class='btn ghost' href='/logins'>Back to logins</a></p>",
                        s["username"], s["role"], "logins")
        if t["role"] == "admin":
            n = c.execute("SELECT COUNT(*) n FROM logins WHERE role='admin' AND enabled=1 AND id != ?",
                          (lid_i,)).fetchone()["n"]
            if not n:
                return page("Cannot delete login",
                            "<h2>Cannot delete login</h2>"
                            "<p class='warn'>This is the last enabled admin account. "
                            "Promote another login to admin first.</p>"
                            "<p><a class='btn ghost' href='/logins'>Back to logins</a></p>",
                            s["username"], s["role"], "logins")
        exten = (t["exten"] or "").strip()
        uname = t["username"]
    # Privacy purge: extension-tied data (recordings+audio, CDR, SMS/MMS,
    # voicemail boxes+audio), including the login's own voicemail box.
    purged = ucp.purge_extension_data([exten], moving_login_id=lid_i) if exten else {}
    with db() as c:
        # Recordings started by this login that don't match the extension.
        for r in c.execute("SELECT id, path FROM recordings WHERE login_id=?", (lid_i,)).fetchall():
            ucp._safe_unlink(r["path"], ucp.REC_ROOT)
        c.execute("DELETE FROM recordings WHERE login_id=?", (lid_i,))
        # Any voicemail boxes still owned by this login (purge already took
        # vm-<ext> and the login's own box; this is belt-and-braces).
        for b in c.execute("SELECT mailbox, greeting_path FROM voicemail_boxes WHERE login_id=?",
                           (lid_i,)).fetchall():
            for m in c.execute("SELECT path FROM voicemail_messages WHERE mailbox=?",
                               (b["mailbox"],)).fetchall():
                ucp._safe_unlink(m["path"], ucp.VM_ROOT)
            ucp._safe_unlink(b["greeting_path"], ucp.VMGREET_DIR)
            c.execute("DELETE FROM voicemail_messages WHERE mailbox=?", (b["mailbox"],))
        c.execute("DELETE FROM voicemail_boxes WHERE login_id=?", (lid_i,))
        # Login-scoped rows (FK cascades are not enforced in sqlite here).
        for tbl, col in [("login_feature_access", "login_id"), ("message_hidden", "login_id"),
                         ("user_prefs", "login_id"), ("ivr_menus", "owner_login_id"),
                         ("user_entitlements", "login_id"), ("billing_customers", "login_id"),
                         ("api_keys", "login_id"), ("e911_users", "login_id"),
                         ("blocked_numbers", "login_id")]:
            c.execute(f"DELETE FROM {tbl} WHERE {col}=?", (lid_i,))
        c.execute("DELETE FROM logins WHERE id=?", (lid_i,))
        c.commit()
    # Sign them out everywhere, drop the SIP endpoint, and audit.
    drop_sessions(lid_i)
    _best_effort_apply()
    _audit_log(s["username"], "logins.delete",
               f"id={lid_i} user={uname} exten={exten or '-'} purged=" +
               ", ".join(f"{k}={v}" for k, v in (purged or {}).items()))
    return RedirectResponse("/logins", status_code=303)


# ---------------- User invites ----------------
# Admin generates a single-use link; the recipient opens it, picks their own
# password, gives display name + voicemail email, and the login is created.
# Tokens are 256-bit, stored as sha256, single-use, expiring, revocable.
INVITE_EXTEN_START = 8800
INVITE_EXTEN_END = 8899
INVITE_EXPIRY_DAYS = (1, 3, 7, 14, 30)
INVITE_DEFAULT_DAYS = 7


def _invite_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _invite_allocate(c):
    """First free extension + SIP username, avoiding logins and live invites."""
    used_ext = {r[0] for r in c.execute("SELECT exten FROM logins WHERE exten != ''").fetchall()}
    used_ext |= {r[0] for r in c.execute(
        "SELECT exten FROM invites WHERE used_at IS NULL AND revoked_at IS NULL"
        " AND expires_at > datetime('now')").fetchall()}
    used_sip = {r[0] for r in c.execute("SELECT sip_username FROM logins WHERE sip_username != ''").fetchall()}
    used_sip |= {r[0] for r in c.execute(
        "SELECT sip_username FROM invites WHERE used_at IS NULL AND revoked_at IS NULL"
        " AND expires_at > datetime('now')").fetchall()}
    for n in range(INVITE_EXTEN_START, INVITE_EXTEN_END + 1):
        e = str(n)
        if e in used_ext:
            continue
        sip_u = "phone%s" % e
        if sip_u in used_sip:
            sip_u = "phone%s%02d" % (e, secrets.randbelow(90) + 10)
            if sip_u in used_sip:
                continue
        return e, sip_u
    return None, None


def _invite_base_url(request: Request) -> str:
    with db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key='panel_url'").fetchone()
        if r and (r["value"] or "").strip():
            return r["value"].strip().rstrip("/")
    return str(request.base_url).rstrip("/")


def _invite_lookup(token: str):
    """Live invite row dict, or None. Generic: never says why it failed."""
    from datetime import datetime as _dt
    if not token or len(token) > 160:
        return None
    th = _invite_token_hash(token)
    with db() as c:
        r = c.execute("SELECT * FROM invites WHERE token_hash=?", (th,)).fetchone()
    if not r or r["used_at"] or r["revoked_at"]:
        return None
    try:
        exp = _dt.strptime(r["expires_at"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    if _dt.utcnow() > exp:
        return None
    return dict(r)


def _invite_status(r) -> str:
    from datetime import datetime as _dt
    if r["used_at"]:
        return "used"
    if r["revoked_at"]:
        return "revoked"
    try:
        if _dt.utcnow() > _dt.strptime(r["expires_at"], "%Y-%m-%d %H:%M:%S"):
            return "expired"
    except (ValueError, TypeError):
        return "expired"
    return "active"


@app.get("/invites", response_class=HTMLResponse)
def invites_page(request: Request, msg: str = ""):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    flash = ""
    if msg == "noext":
        flash = "<p class='warn'>No free extensions left in the invite range.</p>"
    elif msg == "bademail":
        flash = "<p class='warn'>That doesn't look like an email address.</p>"
    with db() as c:
        rows = c.execute("SELECT * FROM invites ORDER BY id DESC LIMIT 200").fetchall()
    exp_opts = "".join(
        f"<option value='{d}' {'selected' if d == INVITE_DEFAULT_DAYS else ''}>{d} day{'s' if d != 1 else ''}</option>"
        for d in INVITE_EXPIRY_DAYS)
    tr = ""
    for r in rows:
        st = _invite_status(r)
        pill = {"active": "ok", "used": "", "expired": "warn", "revoked": "warn"}[st]
        tr += (f"<tr><td>{esc(r['exten'])}</td><td>{esc(r['sip_username'])}</td>"
               f"<td>{esc(r['email']) or '<span class=muted>—</span>'}</td>"
               f"<td>{esc(r['created_at'])}</td><td>{esc(r['expires_at'])}</td>"
               f"<td><span class='pill {pill}'>{st}</span></td><td>"
               + (f"<form method='post' action='/invites/{r['id']}/revoke' style='display:inline'>{_csrf_field(s)}<button class='btn ghost'>Revoke</button></form> " if st == "active" else "")
               + f"<form method='post' action='/invites/{r['id']}/delete' style='display:inline' onsubmit=\"return confirm('Delete this invite record?')\">{_csrf_field(s)}<button class='btn ghost'>Delete</button></form></td></tr>")
    body = f"""{flash}<h2>User invites</h2>
<div class="card"><h3>Generate invite</h3>
<form method="post" action="/invites/new">
{_csrf_field(s)}
<label>Link expires in<br><select name="expires_days">{exp_opts}</select></label><br>
<label>Email it to (optional)<br><input name="email" size="40" placeholder="newuser@example.com"></label><br>
<span class="muted">Leave email blank to just copy the link yourself. The extension, SIP username and SIP secret are auto-generated. The invite link is shown once, right after generation.</span><br><br>
<button class="btn" type="submit">Generate invite</button>
</form></div>
<h3>Invites</h3>
<table><tr><th>Ext</th><th>SIP user</th><th>Emailed to</th><th>Created</th><th>Expires (UTC)</th><th>Status</th><th>Actions</th></tr>
{tr or '<tr><td colspan=7 class=muted>No invites yet</td></tr>'}</table>"""
    return page("User invites", body, s["username"], s["role"], "invites")


@app.post("/invites/new", response_class=HTMLResponse)
async def invite_new(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "invites")
    from datetime import datetime as _dt, timedelta as _td
    f = await request.form()
    try:
        days = int(f.get("expires_days") or INVITE_DEFAULT_DAYS)
    except ValueError:
        days = INVITE_DEFAULT_DAYS
    if days not in INVITE_EXPIRY_DAYS:
        days = INVITE_DEFAULT_DAYS
    email = (f.get("email") or "").strip()[:254]
    if email and "@" not in email:
        return RedirectResponse("/invites?msg=bademail", status_code=303)
    with db() as c:
        exten, sip_u = _invite_allocate(c)
        if not exten:
            return RedirectResponse("/invites?msg=noext", status_code=303)
        token = secrets.token_urlsafe(32)
        exp = (_dt.utcnow() + _td(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = c.execute("INSERT INTO invites (token_hash, exten, sip_username, sip_secret, email, expires_at, created_by)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (_invite_token_hash(token), exten, sip_u, secrets.token_urlsafe(24), email, exp, s["username"]))
        iid = cur.lastrowid
        c.commit()
    link = f"{_invite_base_url(request)}/invite/{token}"
    mailed = False
    mail_err = ""
    if email:
        try:
            with db() as c:
                cfg = mailer.settings(c)
            mailer.send(cfg, email,
                        f"Your {_get_setting('brand_site_name') or 'PBX'} phone account invite",
                        f"Hi,\n\nYou've been invited to set up your phone account.\n\n"
                        f"Open this link within {days} day{'s' if days != 1 else ''} to choose your password "
                        f"and finish setup:\n\n{link}\n\n"
                        f"Your extension will be {exten}. The link stops working after you use it.\n")
            mailed = True
        except Exception as e:  # noqa: BLE001 - MailError or SMTP failure
            mail_err = str(e)[:120]
    _audit_log(s["username"], "invites.generate",
               f"id={iid} exten={exten} sip={sip_u} email={email or '-'} mailed={mailed}")
    # Result page: the ONLY time the raw link is shown. Copy it now.
    mailed_note = (f"<p class='ok'>Emailed to {esc(email)}.</p>" if mailed else
                   (f"<p class='warn'>Email failed ({esc(mail_err)}) — copy the link manually.</p>" if email else
                    "<p class='muted'>No email entered — copy the link yourself.</p>"))
    body = f"""<h2>Invite generated</h2>{mailed_note}
<p>Extension <b>{esc(exten)}</b> is reserved for this invite. Link expires in {days} day{'s' if days != 1 else ''},
is single-use, and can be revoked below.</p>
<label>Invite link (copy it now — it won't be shown again)<br>
<input id="invlink" size="80" readonly value="{esc(link)}"></label>
<button class="btn" id="copyinv" type="button">Copy link</button>
<p class="muted">Or copy it manually:<br><code style="word-break:break-all;user-select:all">{esc(link)}</code></p>
<p><a class="btn ghost" href="/invites">Back to invites</a></p>
<script>
document.getElementById('copyinv').addEventListener('click', async function() {{
  var el = document.getElementById('invlink');
  var v = el.value, ok = false;
  try {{
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      await navigator.clipboard.writeText(v);
      ok = true;
    }} else {{
      throw new Error('no clipboard API');
    }}
  }} catch (e) {{
    el.focus(); el.select();
    try {{ el.setSelectionRange(0, v.length); }} catch (_e) {{}}
    try {{ ok = document.execCommand('copy'); }} catch (_e) {{}}
  }}
  this.textContent = ok ? 'Copied!' : 'Select the link above to copy';
}});
document.getElementById('invlink').select();
</script>"""
    return page("Invite generated", body, s["username"], s["role"], "invites")


@app.post("/invites/{iid}/revoke")
async def invite_revoke(request: Request, iid: int):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    with db() as c:
        c.execute("UPDATE invites SET revoked_at=datetime('now') WHERE id=? AND used_at IS NULL", (iid,))
        c.commit()
    _audit_log(s["username"], "invites.revoke", f"id={iid}")
    return RedirectResponse("/invites", status_code=303)


@app.post("/invites/{iid}/delete")
async def invite_delete(request: Request, iid: int):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    with db() as c:
        c.execute("DELETE FROM invites WHERE id=?", (iid,))
        c.commit()
    _audit_log(s["username"], "invites.delete", f"id={iid}")
    return RedirectResponse("/invites", status_code=303)


_INVITE_BAD = "<p class='warn'>This invite link is invalid or has expired.</p><p><a href='/login'>Go to login</a></p>"


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_redeem_page(request: Request, token: str):
    ip = client_ip(request)
    if not _login_allowed(ip, "invite"):
        return HTMLResponse("<p class='warn'>Too many attempts — try again later.</p>", status_code=429)
    inv = _invite_lookup(token)
    if not inv:
        _login_failed(ip, "invite")
        return page("Invite", _INVITE_BAD)
    body = f"""<h2>Set up your phone account</h2>
<p class="muted">You've been invited to join. Your extension will be <b>{esc(inv['exten'])}</b>.</p>
<form method="post" action="/invite/{esc(token)}">
<input type="hidden" name="token" value="{esc(token)}">
<label>Display name<br><input name="display_name" size="40" required maxlength="64" placeholder="e.g. Jane Smith"></label><br>
<label>Voicemail email (optional)<br><input name="vm_email" size="40" maxlength="254" placeholder="you@example.com"></label><br>
<label>Choose a login password (min 8 characters)<br><input name="password" type="password" required minlength="8"></label><br>
<label>Confirm password<br><input name="password2" type="password" required minlength="8"></label><br><br>
<button class="btn" type="submit">Create my account</button>
</form>"""
    return page("Accept invite", body)


@app.post("/invite/{token}", response_class=HTMLResponse)
async def invite_redeem(request: Request, token: str):
    ip = client_ip(request)
    if not _login_allowed(ip, "invite"):
        return HTMLResponse("<p class='warn'>Too many attempts — try again later.</p>", status_code=429)
    f = await request.form()
    # CSRF: the secret token must match the URL (an attacker can't know it).
    if not hmac.compare_digest(f.get("token") or "", token or ""):
        _login_failed(ip, "invite")
        return page("Invite", _INVITE_BAD, status_code=403)
    inv = _invite_lookup(token)
    if not inv:
        _login_failed(ip, "invite")
        return page("Invite", _INVITE_BAD)
    display_name = (f.get("display_name") or "").strip()[:64]
    vm_email = (f.get("vm_email") or "").strip()[:254]
    pw1 = f.get("password") or ""
    pw2 = f.get("password2") or ""
    err = ""
    if not display_name:
        err = "Please enter a display name."
    elif vm_email and "@" not in vm_email:
        err = "That voicemail email doesn't look valid."
    elif len(pw1) < 8:
        err = "Password must be at least 8 characters."
    elif not hmac.compare_digest(pw1, pw2):
        err = "Passwords don't match."
    if err:
        _login_failed(ip, "invite")
        return page("Accept invite",
                    f"<p class='warn'>{esc(err)}</p><p><a class='btn ghost' href='/invite/{esc(token)}'>Back</a></p>")
    pwh = bcrypt.hashpw(pw1.encode(), bcrypt.gensalt()).decode()
    with db() as c:
        # Atomic single-use claim: exactly one redemption wins the race.
        cur = c.execute("UPDATE invites SET used_at=datetime('now') WHERE id=? AND used_at IS NULL"
                        " AND revoked_at IS NULL AND expires_at > datetime('now')", (inv["id"],))
        if cur.rowcount != 1:
            c.rollback()
            return page("Invite", _INVITE_BAD)
        exten, sip_u = inv["exten"], inv["sip_username"]
        if c.execute("SELECT 1 FROM logins WHERE exten=? OR username=? OR sip_username=?",
                     (exten, exten, sip_u)).fetchone():
            # Extension was taken manually since generation: re-allocate.
            exten2, sip_u2 = _invite_allocate(c)
            if not exten2:
                c.rollback()
                return page("Invite", "<p class='warn'>No extensions available — please contact your administrator.</p>")
            c.execute("UPDATE invites SET exten=?, sip_username=? WHERE id=?", (exten2, sip_u2, inv["id"]))
            exten, sip_u = exten2, sip_u2
        c.execute("INSERT INTO logins (username, pwhash, role, exten, sip_username, sip_secret,"
                  " display_name, vm_email, record_admin, user_record, enabled)"
                  " VALUES (?,?,?,?,?,?,?,?,0,0,1)",
                  (exten, pwh, "user", exten, sip_u, inv["sip_secret"], display_name, vm_email))
        c.commit()
    _best_effort_apply()
    _audit_log("invite", "invites.redeemed", f"exten={exten} sip={sip_u}")
    body = f"""<h2>Account created</h2>
<p class="ok">Welcome, {esc(display_name)}! Your extension is <b>{esc(exten)}</b>.</p>
<div class="card"><h3>Save these — the SIP secret won't be shown again</h3>
<table>
<tr><td>Login username</td><td><b>{esc(exten)}</b></td></tr>
<tr><td>Extension</td><td><b>{esc(exten)}</b></td></tr>
<tr><td>SIP username</td><td><b>{esc(sip_u)}</b></td></tr>
<tr><td>SIP secret</td><td><code>{esc(inv['sip_secret'])}</code></td></tr>
</table>
<p class="muted">Use the SIP username/secret in your softphone (e.g. Zoiper), and the login username + the password you chose to sign in to My Phone.</p></div>
<p><a class="btn" href="/login">Go to login</a></p>"""
    return page("Account created", body)


@app.get("/trunks", response_class=HTMLResponse)
def trunks_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        rows = c.execute("SELECT name, registrar, username, enabled FROM trunks ORDER BY name").fetchall()
    tr = "".join(f"<tr><td>{esc(r['name'])}</td><td>{esc(r['registrar'])}</td><td>{esc(r['username'])}</td><td>{'yes' if r['enabled'] else 'no'}</td><td><a class='btn ghost' href='/trunks/{esc(r['name'])}/edit'>Edit</a></td></tr>" for r in rows)
    # NOTE: trunk name in URL path — names are validated to [A-Za-z0-9_.-]
    # on save (see _valid_name), so URL-embedding is safe.
    body = f"<h2>Trunks</h2><table><tr><th>Name</th><th>Registrar</th><th>Username</th><th>Enabled</th><th></th></tr>{tr}</table>"
    body += "<p><a href='/trunks/new/edit' class='btn'>Add trunk</a></p>"
    return page("Trunks", body, s["username"], s["role"], "trunks")


@app.get("/trunks/{name}/edit", response_class=HTMLResponse)
def trunk_edit_page(request: Request, name: str):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    t = None
    if name != "new":
        with db() as c:
            t = c.execute("SELECT * FROM trunks WHERE name=?", (name,)).fetchone()
        if not t:
            return RedirectResponse("/trunks")
    def v(k, d=""):
        return esc((t[k] if t and t[k] else d) if t else d)
    body = f"""
<h2>{'Edit' if t else 'Add'} trunk</h2>
<form method="post" action="/trunks/{name}/edit">
{_csrf_field(s)}
<label>Name<br><input name="new_name" value="{v('name', name if name!='new' else '')}" {'readonly' if t else 'required'}></label><br>
<label>Registrar (e.g. sip:yourserver.voip.ms)<br><input name="registrar" value="{v('registrar')}" size="40" required></label><br>
<label>Username<br><input name="username" value="{v('username')}" size="30" required></label><br>
<label>Secret<br><input name="secret" type="password" value="" placeholder="{'(unchanged)' if t else ''}" {'required' if not t else ''}></label><br>
<label>Codecs<br><input name="codecs" value="{v('codecs','ulaw,alaw,g722')}" size="40"></label><br>
<label><input type="checkbox" name="enabled" {'checked' if not t or t['enabled'] else ''}> Enabled</label><br><br>
<button class="btn" type="submit">Save</button>
<a class="btn ghost" href="/trunks">Cancel</a>
</form>"""
    return page("Edit trunk", body, s["username"], s["role"], "trunks")


@app.post("/trunks/{name}/edit")
async def trunk_edit_save(request: Request, name: str,
                    new_name: str = Form(...), registrar: str = Form(...),
                    username: str = Form(...), secret: str = Form(""),
                    codecs: str = Form("ulaw,alaw,g722"),
                    enabled: str = Form(None)):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "trunks")
    await _check_csrf(request, s)
    en = 1 if enabled else 0
    new_name = _valid_name(new_name, "trunk name")
    with db() as c:
        if name == "new":
            c.execute("INSERT INTO trunks (name, registrar, username, secret, codecs, enabled) VALUES (?,?,?,?,?,?)",
                      (new_name, registrar, username, secret, codecs, en))
        else:
            if secret:
                c.execute("UPDATE trunks SET registrar=?, username=?, secret=?, codecs=?, enabled=? WHERE name=?",
                          (registrar, username, secret, codecs, en, name))
            else:
                c.execute("UPDATE trunks SET registrar=?, username=?, codecs=?, enabled=? WHERE name=?",
                          (registrar, username, codecs, en, name))
        c.commit()
    # Regenerate pjsip.conf so Asterisk picks up the change (best effort;
    # retry via POST /system/reload if this fails)
    _best_effort_apply()
    return RedirectResponse("/trunks", status_code=302)


@app.get("/routes", response_class=HTMLResponse)
def routes_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        inb = c.execute("SELECT r.id, r.did, r.ring_extens, r.timeout_sec, r.enabled, r.ivr_id, m.name AS ivr_name,"
                        " r.group_id, g.name AS group_name, g.ring_seconds AS group_secs"
                        " FROM inbound_routes r LEFT JOIN ivr_menus m ON m.id=r.ivr_id"
                        " LEFT JOIN ring_groups g ON g.id=r.group_id ORDER BY r.did").fetchall()
        outb = c.execute("SELECT r.name, r.patterns, t.name as trunk, r.priority, r.enabled FROM outbound_routes r LEFT JOIN trunks t ON r.trunk_id=t.id ORDER BY r.priority").fetchall()
    def _dest(r):
        if r['ivr_id']:
            return 'IVR: ' + esc(r['ivr_name'])
        if r['group_id']:
            return 'Ring group: ' + esc(r['group_name'] or '(deleted)')
        return 'Ring ' + esc(r['ring_extens'])
    def _secs(r):
        if r['ivr_id']:
            return '-'
        return str(r['group_secs'] if r['group_id'] and r['group_secs'] else r['timeout_sec']) + 's'
    in_tr = "".join(f"<tr><td>{esc(r['did'])}</td><td>{_dest(r)}</td><td>{_secs(r)}</td><td>{'yes' if r['enabled'] else 'no'}</td><td><a class='btn ghost' href='/routes/inbound/{r['id']}/edit'>Edit</a></td></tr>" for r in inb)
    out_tr = "".join(f"<tr><td>{esc(r['name'])}</td><td>{esc(r['patterns'])}</td><td>{esc(r['trunk'])}</td><td>{r['priority']}</td><td>{'yes' if r['enabled'] else 'no'}</td></tr>" for r in outb)
    body = f"<h2>Inbound Routes</h2><table><tr><th>DID</th><th>Destination</th><th>Ring time</th><th>Enabled</th><th></th></tr>{in_tr}</table>"
    body += "<p><a href='/routes/inbound/new/edit' class='btn'>Add DID route</a></p>"
    body += f"<h2>Outbound Routes</h2><table><tr><th>Name</th><th>Patterns</th><th>Trunk</th><th>Priority</th><th>Enabled</th></tr>{out_tr}</table>"
    pmode = _get_setting("outside_prefix_mode") if _get_setting("outside_prefix_mode") in ("optional", "required") else "off"
    pdig = _get_setting("outside_prefix") if _get_setting("outside_prefix") in ("8", "9") else "9"
    mopts = "".join(f'<option value="{k}" {"selected" if k == pmode else ""}>{t}</option>' for k, t in (
        ("off", "Off - dial numbers directly (default)"),
        ("optional", "Optional - prefix + number works, the number alone works too"),
        ("required", "Required - outside numbers must start with the prefix")))
    dopts = "".join(f'<option value="{d}" {"selected" if d == pdig else ""}>{d}</option>' for d in ("9", "8"))
    body += f"""<h2>Outside line prefix</h2>
<p class="muted">For offices used to "dial 9 for an outside line". The prefix is removed before the call goes out,
so call history and caller ID show the real number. <b>911 always works without a prefix</b> (9-911 also works,
see E911). Desk phones may need their dial plan updated (e.g. <code>9xxxxxxxxxx</code>); Zoiper and other
softphones need no changes.</p>
<form method="post" action="/routes/prefix" class="panel">{_csrf_field(s)}
<label>Mode<br><select name="mode">{mopts}</select></label>
<label>Prefix digit<br><select name="digit">{dopts}</select></label>
<button class="btn">Save</button></form>
<p class="muted">"Required" means numbers dialed without the prefix (including callbacks from call history on
some phones) won't connect. "Optional" is the safest way to introduce a prefix.</p>"""
    return page("Routes", body, s["username"], s["role"], "routes")


@app.get("/routes/inbound/{rid}/edit", response_class=HTMLResponse)
def inbound_edit_page(request: Request, rid: str):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    t = None
    if rid != "new":
        with db() as c:
            t = c.execute("SELECT * FROM inbound_routes WHERE id=?", (rid,)).fetchone()
        if not t:
            return RedirectResponse("/routes")
    def v(k, d=""):
        return esc(t[k] if t and t[k] is not None else d)
    cur_ivr = dict(t).get("ivr_id") if t else None
    cur_grp = dict(t).get("group_id") if t else None
    ivr_opts = '<option value="">Ring the extensions below</option>' + "".join(
        f'<option value="g{g["id"]}" {"selected" if g["id"] == cur_grp and not cur_ivr else ""}>Ring group: {esc(g["name"])}</option>'
        for g in ring_groups_ui.all_groups()) + "".join(
        f'<option value="{m["id"]}" {"selected" if m["id"] == cur_ivr else ""}>IVR menu: {esc(m["name"])}'
        f'{"" if not m["owner_login_id"] else " (user " + esc(m["owner"] or "?") + ")"}</option>'
        for m in ivr_ui.system_menus())
    body = f"""
<h2>{'Edit' if t else 'Add'} inbound route</h2>
<form method="post" action="/routes/inbound/{rid}/edit">
{_csrf_field(s)}
<label>Phone number (with or without the leading 1 - both work)<br><input name="did" value="{v('did')}" size="20" required></label><br>
<label>Send callers to<br><select name="ivr_id">{ivr_opts}</select></label><br>
<label>Ring extensions (comma-separated, first answer wins)<br><input name="ring_extens" value="{v('ring_extens','[]')}" size="40" placeholder='["8800", "8801"] or 8800,8801'></label><br>
<label>Timeout (seconds)<br><input name="timeout_sec" value="{v('timeout_sec','30')}" size="6"></label><br>
<p><small>With an IVR menu selected, the menu answers and the ring list is only used if the menu is switched off.
A number that rings a single user uses that user's own ring time and "answer with IVR" setting.</small></p>
<label><input type="checkbox" name="enabled" {'checked' if not t or t['enabled'] else ''}> Enabled</label><br><br>
<button class="btn" type="submit">Save</button>
<a class="btn ghost" href="/routes">Cancel</a>
</form>
{('<form method="post" action="/routes/inbound/' + str(t['id']) + '/delete" onsubmit="return confirm(&quot;Delete this route? Calls to this number will no longer ring anyone.&quot;)">' + _csrf_field(s) + '<button class="btn ghost danger">Delete route</button></form>') if t else ''}
<p><small>Ringing a list: no-answer goes to the first listed extension's voicemail box. Ringing a group: the
group's own ring time and voicemail setting apply (<a href="/ring-groups">Ring groups</a>).</small></p>"""
    return page("Edit inbound route", body, s["username"], s["role"], "routes")


def _did_norm(v: str) -> str:
    """Digits only; US/Canada numbers stored as 10 digits (matches +1 / 1 / 10-digit)."""
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[1:] if len(d) == 11 and d.startswith("1") else d


@app.post("/routes/prefix")
async def routes_prefix(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "routes")
    f = await request.form()
    mode = f.get("mode") if f.get("mode") in ("off", "optional", "required") else "off"
    digit = f.get("digit") if f.get("digit") in ("8", "9") else "9"
    _set_setting("outside_prefix_mode", mode)
    _set_setting("outside_prefix", digit)
    with db() as c:
        c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)",
                  (s["username"], "routes.prefix", f"mode={mode} digit={digit}"))
        c.commit()
    return RedirectResponse("/routes", status_code=303)


@app.post("/routes/inbound/{rid}/delete")
async def inbound_delete(request: Request, rid: int):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "routes")
    with db() as c:
        r = c.execute("SELECT did FROM inbound_routes WHERE id=?", (rid,)).fetchone()
        if r:
            c.execute("DELETE FROM inbound_routes WHERE id=?", (rid,))
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)",
                      (s["username"], "route.inbound.delete", r["did"]))
        c.commit()
    return RedirectResponse("/routes", status_code=303)


@app.post("/routes/inbound/{rid}/edit")
async def inbound_edit_save(request: Request, rid: str,
                      did: str = Form(...), ring_extens: str = Form(""),
                      timeout_sec: int = Form(30), enabled: str = Form(None),
                      ivr_id: str = Form("")):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "routes")
    await _check_csrf(request, s)
    import json
    # Accept '["8800"]' or '8800,8801'
    ring_extens = ring_extens.strip()
    if not ring_extens.startswith("["):
        ring_extens = json.dumps([e.strip() for e in ring_extens.split(",") if e.strip()])
    en = 1 if enabled else 0
    did = _did_norm(did)
    if not did:
        return HTMLResponse(page("Edit inbound route", "<p class='bad'>Enter the phone number (digits).</p>"
                                 "<p><a href='javascript:history.back()'>Back</a></p>", s["username"], s["role"], "routes"), status_code=400)
    with db() as c:
        for r in c.execute("SELECT id, did FROM inbound_routes"):
            if _did_norm(r["did"]) == did and str(r["id"]) != str(rid):
                return HTMLResponse(page("Edit inbound route",
                    f"<p class='bad'>There's already a route for {esc(did)} (written as {esc(r['did'])}). "
                    "One route covers the number with or without the leading 1 - edit that one instead.</p>"
                    f"<p><a href='/routes/inbound/{r['id']}/edit'>Open the existing route</a></p>",
                    s["username"], s["role"], "routes"), status_code=400)
    ivr = None
    grp = None
    if ivr_id.strip().startswith("g") and ivr_id.strip()[1:].isdigit():
        with db() as c:
            if c.execute("SELECT 1 FROM ring_groups WHERE id=?", (int(ivr_id.strip()[1:]),)).fetchone():
                grp = int(ivr_id.strip()[1:])
    elif ivr_id.strip().isdigit():
        with db() as c:
            if c.execute("SELECT 1 FROM ivr_menus WHERE id=?", (int(ivr_id),)).fetchone():
                ivr = int(ivr_id)
    timeout_sec = max(5, min(int(timeout_sec or 30), 300))
    with db() as c:
        if rid == "new":
            c.execute("INSERT INTO inbound_routes (did, ring_extens, timeout_sec, enabled, ivr_id, group_id) VALUES (?,?,?,?,?,?)",
                      (did.strip(), ring_extens, timeout_sec, en, ivr, grp))
        else:
            c.execute("UPDATE inbound_routes SET did=?, ring_extens=?, timeout_sec=?, enabled=?, ivr_id=?, group_id=? WHERE id=?",
                      (did.strip(), ring_extens, timeout_sec, en, ivr, grp, rid))
        c.commit()
    return RedirectResponse("/routes", status_code=302)


@app.get("/cdr", response_class=HTMLResponse)
def cdr_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        rows = c.execute("SELECT id, src, dst, direction, disposition, duration_sec, bill_sec, started_at FROM cdr ORDER BY id DESC LIMIT 500").fetchall()
        total = c.execute("SELECT COUNT(*) FROM cdr").fetchone()[0]
    tr = "".join(f"<tr><td><input type='checkbox' class='sel' name='ids' value='{r['id']}' form='cdrdel' aria-label='Select'></td><td>{esc(r['src'])}</td><td>{esc(r['dst'])}</td><td>{esc(r['direction'])}</td><td>{esc(r['disposition'])}</td><td>{r['duration_sec']}s</td><td>{r['bill_sec']}s</td><td>{fmt_ts(r['started_at'], from_utc=False)}</td></tr>" for r in rows)
    flash = ""
    d = request.query_params.get("deleted")
    if d is not None and d.isdigit():
        flash = f"<div class='flash ok'>Deleted {int(d)} record{'s' if d != '1' else ''}.</div>"
    shown = f"Showing the latest {len(rows)} of {total}." if total > len(rows) else f"{total} record{'s' if total != 1 else ''}."
    body = f"""{flash}<h2>Call Detail Records</h2>
<form id="cdrdel" method="post" action="/cdr/delete" class="bulkbar">{_csrf_field(s)}
<span class="muted">{shown}</span>
<button class="btn ghost" name="mode" value="selected" id="delsel" disabled onclick="return confirm('Delete the selected records?')">Delete selected</button>
<button class="btn ghost danger" name="mode" value="all" {'disabled' if not total else ''} onclick="return confirm('Delete ALL {total} call records? This also clears the call history for all users and cannot be undone.')">Delete all</button>
</form>
<table><tr><th><input type="checkbox" id="selall" aria-label="Select all"></th><th>From</th><th>To</th><th>Dir</th><th>Status</th><th>Dur</th><th>Bill</th><th>Time</th></tr>{tr or "<tr><td colspan='8' class='muted'>No call records</td></tr>"}</table>
<p class="muted">Records older than 90 days are removed automatically. Deleting records also removes them from users' call history. Recordings and voicemail are kept.</p>
<script>
const all = document.getElementById('selall'), boxes = () => [...document.querySelectorAll('input.sel')];
function sync() {{ const n = boxes().filter(b => b.checked).length;
  const btn = document.getElementById('delsel'); btn.disabled = !n; btn.textContent = n ? 'Delete selected (' + n + ')' : 'Delete selected';
  all.checked = n && n === boxes().length; all.indeterminate = n > 0 && n < boxes().length; }}
all.addEventListener('change', () => {{ boxes().forEach(b => b.checked = all.checked); sync(); }});
boxes().forEach(b => b.addEventListener('change', sync));
</script>"""
    return page("CDR", body, s["username"], s["role"], "cdr")


@app.post("/cdr/delete")
async def cdr_delete(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "cdr")
    form = await request.form()
    everything = form.get("mode") == "all"
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    n = _delete_rows("cdr", ids, everything)
    _audit_log(s["username"], "cdr.delete", "all" if everything else f"{n} rows")
    return RedirectResponse(f"/cdr?deleted={n}", status_code=303)


@app.get("/voicemail", response_class=HTMLResponse)
def vm_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        rows = c.execute("SELECT m.id, m.mailbox, m.caller, m.duration_sec, m.folder, m.received_at, m.path, b.login_id FROM voicemail_messages m LEFT JOIN voicemail_boxes b ON m.mailbox=b.mailbox ORDER BY m.id DESC LIMIT 500").fetchall()
        total = c.execute("SELECT COUNT(*) FROM voicemail_messages").fetchone()[0]
    tr = "".join(f"<tr><td><input type='checkbox' class='sel' name='ids' value='{r['id']}' form='avm' aria-label='Select'></td><td>{esc(r['mailbox'])}</td><td>{esc(r['caller'])}</td><td>{r['duration_sec']}s</td><td>{esc(r['folder'])}</td><td>{fmt_ts(r['received_at'])}</td><td><audio controls preload=\"none\" src=\"/api/voicemail-audio/{r['id']}\"></audio> <a href=\"/api/voicemail-audio/{r['id']}?download=1\">DL</a></td></tr>" for r in rows)
    body = (f"{_deleted_flash(request)}<h2>Voicemail Messages</h2>{_shown(len(rows), total, 'message')}"
            + ucp._bulkbar(s, "avm", "/voicemail/delete", total, "voicemail messages (every mailbox)")
            + f"<table><tr><th><input type='checkbox' id='avm-all' aria-label='Select all'></th><th>Box</th><th>Caller</th><th>Length</th><th>Folder</th><th>Received</th><th>Play</th></tr>{tr or '<tr><td colspan=7 class=muted>No messages</td></tr>'}</table>"
            + ucp._bulk_js("avm"))
    return page("Voicemail", body, s["username"], s["role"], "vm")


@app.post("/voicemail/delete")
async def vm_delete_admin(request: Request):
    s, form, everything, ids = await _admin_bulk_form(request, "vm")
    if s is None:
        return form  # redirect / locked page
    with db() as c:
        rows = (c.execute("SELECT id, path FROM voicemail_messages").fetchall() if everything else
                [r for i in ids for r in c.execute("SELECT id, path FROM voicemail_messages WHERE id=?", (i,))])
        c.executemany("DELETE FROM voicemail_messages WHERE id=?", [(r["id"],) for r in rows])
        c.commit()
    for r in rows:
        _unlink_under(r["path"], "/var/spool/pbx/voicemail")
    _audit_log(s["username"], "voicemail.delete", "all" if everything else f"{len(rows)} rows")
    return RedirectResponse(f"/voicemail?deleted={len(rows)}", status_code=303)


@app.get("/api/voicemail-audio/{msg_id}")
def vm_audio(msg_id: int, request: Request, download: int = 0):
    s = _sess(request)
    if not s:
        raise HTTPException(401)
    if s["role"] != "admin" and not ucp.can_play_voicemail(s, msg_id):
        raise HTTPException(403)
    with db() as c:
        row = c.execute("SELECT path FROM voicemail_messages WHERE id=?", (msg_id,)).fetchone()
    if not row or not row["path"]:
        raise HTTPException(404)
    fpath = row["path"]
    # Security: contain path within the voicemail dir. (Paths come from our
    # own DB, but belt-and-braces against DB tampering.)
    if os.path.commonpath([os.path.abspath(fpath), "/var/spool/pbx/voicemail"]) != "/var/spool/pbx/voicemail":
        raise HTTPException(403)
    if not os.path.isfile(fpath):
        raise HTTPException(404)
    import os as _os
    fname = _os.path.basename(fpath)
    headers = {"Content-Disposition": f"attachment; filename={fname}"} if download else {}
    return FileResponse(fpath, media_type="audio/wav", headers=headers)


RECORDING_LAW_NOTICE = (
    "Recording phone calls without the consent of everyone on the call is illegal in some US states "
    "(\"two-party\" or all-party consent states, such as California) and in other countries, and may "
    "break workplace policies or union agreements. Admin recording is silent to the people on the call "
    "unless the announcement below is turned on. Check federal, state and local laws, and your "
    "employer's rules, before turning it on.")


def _rec_settings_html(request, s):
    on = _get_setting("admin_rec_enabled") == "1"
    ann = _get_setting("rec_announce") == "1"
    path = _get_setting("rec_announce_path")
    has_file = bool(path) and path != "0" and os.path.isfile(path)
    ack_by = _get_setting("admin_rec_ack_by")
    ack_at = _get_setting("admin_rec_ack_at")
    with db() as c:
        n_flag = c.execute("SELECT COUNT(*) FROM logins WHERE record_admin=1").fetchone()[0]
    msg = {"recon": ("ok", "Admin recording is on."), "recoff": ("ok", "Admin recording is off for the whole system."),
           "recack": ("bad", "Tick the acknowledgement to turn admin recording on."),
           "annsaved": ("ok", "Announcement saved."), "annbad": ("bad", request.query_params.get("why", "That audio file couldn't be used.")),
           "annremoved": ("ok", "Announcement file removed (a short beep plays instead).")}.get(request.query_params.get("rmsg", ""))
    flash = f'<div class="flash {msg[0]}">{esc(msg[1])}</div>' if msg else ""
    csrf = _csrf_field(s)
    state = ('<span class="pill ok">On</span>' if on else '<span class="pill">Off</span>')
    who = f'<p class="muted">Turned on by {esc(ack_by)} on {esc(ack_at)} after acknowledging the notice.</p>' if on and ack_by else ""
    return f"""{flash}<h2>Recording settings</h2>
<div class="grid2">
<section class="panel"><h3>Admin call recording {state}</h3>
<div class="flash warn-note">{esc(RECORDING_LAW_NOTICE)}</div>
<p class="muted">When on, calls of extensions with "Admin call recording" ticked on the Logins page are recorded here
({n_flag} extension{"s" if n_flag != 1 else ""} ticked). When off, no admin recordings are made at all, whatever the
Logins page says. Users' own "Record my calls" is separate (their plan + their own acknowledgement).</p>
{who}
<form method="post" action="/recordings/settings">{csrf}
<input type="hidden" name="what" value="admin">
{'' if on else '<label class="inline-chk"><input type="checkbox" name="ack" value="1"> I have checked the laws and rules that apply and I am responsible for getting any consent they require.</label>'}
<button class="btn {'ghost' if on else ''}" name="admin_rec" value="{'0' if on else '1'}">{'Turn admin recording off' if on else 'Turn admin recording on'}</button>
</form></section>
<section class="panel"><h3>"This call may be recorded" announcement {'<span class="pill ok">On</span>' if ann else '<span class="pill">Off</span>'}</h3>
<p class="muted">Plays to both people when a call starts being recorded (admin or user recording) - the usual way to
let callers know. Upload your own message (WAV or MP3, up to 30 seconds); without one, a short beep plays.</p>
<form method="post" action="/recordings/settings">{csrf}<input type="hidden" name="what" value="announce">
<label class="switch"><input type="checkbox" name="rec_announce" value="1" {"checked" if ann else ""}> <span>Play the announcement on recorded calls</span></label>
<button class="btn ghost">Save</button></form>
<form method="post" action="/recordings/announcement" enctype="multipart/form-data">{csrf}
<label>Announcement file {'<span class="pill ok">uploaded</span>' if has_file else '<span class="muted">(none - beep)</span>'}<br><input type="file" name="file" accept="audio/*" required></label>
<button class="btn ghost">Upload</button></form>
{('<audio controls preload="none" src="/api/rec-announcement"></audio><form method="post" action="/recordings/announcement/remove" class="inline">' + csrf + '<button class="link-btn bad">Remove file</button></form>') if has_file else ''}
</section></div>"""


@app.post("/recordings/settings")
async def rec_settings_save(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "rec")
    f = await request.form()
    import time as _t
    if f.get("what") == "admin":
        turn_on = f.get("admin_rec") == "1"
        if turn_on and f.get("ack") != "1":
            return RedirectResponse("/recordings?rmsg=recack", status_code=303)
        _set_setting("admin_rec_enabled", "1" if turn_on else "0")
        if turn_on:
            _set_setting("admin_rec_ack_by", s["username"])
            _set_setting("admin_rec_ack_at", _t.strftime("%Y-%m-%d %H:%M"))
        detail = ("ON (law notice acknowledged)" if turn_on else "off")
        with db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)", (s["username"], "recording.admin", detail))
            c.commit()
        security.record("recording_change", client_ip(request), f"admin call recording {detail}", actor=s["username"])
        return RedirectResponse("/recordings?rmsg=" + ("recon" if turn_on else "recoff"), status_code=303)
    if f.get("what") == "announce":
        _set_setting("rec_announce", "1" if f.get("rec_announce") else "0")
        with db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)",
                      (s["username"], "recording.announce", "on" if f.get("rec_announce") else "off"))
            c.commit()
        return RedirectResponse("/recordings", status_code=303)
    return RedirectResponse("/recordings", status_code=303)


@app.post("/recordings/announcement")
async def rec_announcement_upload(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "rec")
    f = await request.form()
    up = f.get("file")
    if not up or not hasattr(up, "read"):
        return RedirectResponse("/recordings?rmsg=annbad", status_code=303)
    data = await up.read()
    from starlette.concurrency import run_in_threadpool
    try:
        path = await run_in_threadpool(ivr_ui._convert_greeting, data, up.filename or "", "announce",
                                       None, "recann", 30)
    except ValueError as e:
        import urllib.parse as _up
        return RedirectResponse("/recordings?rmsg=annbad&why=" + _up.quote(str(e)), status_code=303)
    old = _get_setting("rec_announce_path")
    _set_setting("rec_announce_path", path)
    if old and old != "0" and old != path:
        ucp._safe_unlink(old, os.path.dirname(path))
    with db() as c:
        c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)", (s["username"], "recording.announce_file", "uploaded"))
        c.commit()
    return RedirectResponse("/recordings?rmsg=annsaved", status_code=303)


@app.post("/recordings/announcement/remove")
async def rec_announcement_remove(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "rec")
    old = _get_setting("rec_announce_path")
    _set_setting("rec_announce_path", "")
    if old and old != "0":
        ucp._safe_unlink(old, os.path.dirname(old))
    return RedirectResponse("/recordings?rmsg=annremoved", status_code=303)


@app.get("/api/rec-announcement")
def rec_announcement_audio(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        raise HTTPException(403)
    p = _get_setting("rec_announce_path")
    root = os.path.abspath(ivr_ui.GREET_DIR)
    if not p or p == "0" or not os.path.isfile(p) or os.path.commonpath([os.path.abspath(p), root]) != root:
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav")


@app.get("/recordings", response_class=HTMLResponse)
def rec_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        rows = c.execute("SELECT id, system, exten, direction, peer, duration_sec, started_at, path FROM recordings ORDER BY id DESC LIMIT 500").fetchall()
        total = c.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
    tr = "".join(f"<tr><td><input type='checkbox' class='sel' name='ids' value='{r['id']}' form='arec' aria-label='Select'></td><td>{esc(r['system'])}</td><td>{esc(r['exten'])}</td><td>{esc(r['direction'])}</td><td>{esc(r['peer'])}</td><td>{r['duration_sec']}s</td><td>{fmt_ts(r['started_at'])}</td><td><audio controls preload=\"none\" src=\"/api/recording-audio/{r['id']}\"></audio> <a href=\"/api/recording-audio/{r['id']}?download=1\">DL</a></td></tr>" for r in rows)
    body = (f"{_deleted_flash(request)}{_rec_settings_html(request, s)}<h2>Call Recordings</h2>{_shown(len(rows), total, 'recording')}"
            + ucp._bulkbar(s, "arec", "/recordings/delete", total, "call recordings (admin and user)")
            + f"<table><tr><th><input type='checkbox' id='arec-all' aria-label='Select all'></th><th>System</th><th>Exten</th><th>Dir</th><th>Peer</th><th>Length</th><th>Time</th><th>Play</th></tr>{tr or '<tr><td colspan=8 class=muted>No recordings</td></tr>'}</table>"
            + ucp._bulk_js("arec")
            + "<p class='muted'>Deleting a recording keeps the call in the call history.</p>")
    return page("Recordings", body, s["username"], s["role"], "rec")


@app.post("/recordings/delete")
async def rec_delete_admin(request: Request):
    s, form, everything, ids = await _admin_bulk_form(request, "rec")
    if s is None:
        return form
    with db() as c:
        rows = (c.execute("SELECT id, path FROM recordings").fetchall() if everything else
                [r for i in ids for r in c.execute("SELECT id, path FROM recordings WHERE id=?", (i,))])
        for r in rows:
            c.execute("UPDATE cdr SET recording_id=NULL WHERE recording_id=?", (r["id"],))
            c.execute("DELETE FROM recordings WHERE id=?", (r["id"],))
        c.commit()
    for r in rows:
        _unlink_under(r["path"], "/var/spool/pbx/monitor")
    _audit_log(s["username"], "recordings.delete", "all" if everything else f"{len(rows)} rows")
    return RedirectResponse(f"/recordings?deleted={len(rows)}", status_code=303)


@app.get("/messages", response_class=HTMLResponse)
def messages_admin_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        rows = c.execute("SELECT id, from_ext, to_ext, body, sent_at FROM messages ORDER BY id DESC LIMIT 500").fetchall()
        total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    tr = "".join(f"<tr><td><input type='checkbox' class='sel' name='ids' value='{r['id']}' form='amsg' aria-label='Select'></td><td>{esc(r['from_ext'])}</td><td>{esc(r['to_ext'])}</td><td class='msgbody'>{esc(r['body'])}</td><td>{fmt_ts(r['sent_at'])}</td></tr>" for r in rows)
    body = (f"{_deleted_flash(request)}<h2>Text Messages</h2>{_shown(len(rows), total, 'message')}"
            + ucp._bulkbar(s, "amsg", "/messages/delete", total, "text messages (for everyone)")
            + f"<table><tr><th><input type='checkbox' id='amsg-all' aria-label='Select all'></th><th>From</th><th>To</th><th>Message</th><th>Sent</th></tr>{tr or '<tr><td colspan=5 class=muted>No messages</td></tr>'}</table>"
            + ucp._bulk_js("amsg")
            + "<p class='muted'>Deleting here removes the message for both people.</p>")
    return page("Messages", body, s["username"], s["role"], "msgs")


@app.post("/messages/delete")
async def messages_delete_admin(request: Request):
    s, form, everything, ids = await _admin_bulk_form(request, "msgs")
    if s is None:
        return form
    with db() as c:
        if everything:
            c.execute("DELETE FROM message_hidden")
            n = c.execute("DELETE FROM messages").rowcount
        else:
            n = 0
            for i in ids:
                c.execute("DELETE FROM message_hidden WHERE message_id=?", (i,))
                n += c.execute("DELETE FROM messages WHERE id=?", (i,)).rowcount
        c.commit()
    _audit_log(s["username"], "messages.delete", "all" if everything else f"{n} rows")
    return RedirectResponse(f"/messages?deleted={n}", status_code=303)


# ---------------------------------------------------------------- voip.ms SMS/MMS webhook (best-route)
# Inbound: voip.ms -> URL Callback -> here -> messages table -> pbx-brain
# delivers to Zoiper via SIP MESSAGE, My Phone, and ESP via /messages/inbox.
# Configure each DID in voip.ms: DID Numbers > Manage DIDs > Edit >
# Message Service (SMS/MMS) > SMS URL Callback =
#   https://<your-pbx>/hooks/voipms-sms?token=<webhook_token>&to={TO}&from={FROM}&message={MESSAGE}&id={ID}&date={TIMESTAMP}
# voip.ms sends GET for liveness + POST (form) per message. If "URL Callback
# Retry" is on, we must return plain "ok".

def _voipms_token_ok(request: Request, qs: dict) -> bool:
    with db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key='voipms_webhook_token'").fetchone()
        want = (r["value"] if r else "").strip()
    if not want:
        return True  # no token configured yet -> accept (set one in UI)
    got = (qs.get("token") or [""])[0] if isinstance(qs.get("token"), list) else qs.get("token", "")
    if got == want:
        return True
    if request.headers.get("X-Webhook-Token") == want:
        return True
    return False


def _try_mms_enrich(did_raw: str, voipms_id: str) -> str:
    """An MMS callback often arrives with an empty message body. Look the
    message up via the getMMS API and build a display body (text + media links).
    Returns "" when not found."""
    if not voipms_id:
        return ""
    from datetime import datetime as _dt, timedelta as _td
    try:
        with db() as c:
            cfg = voipms_sms.get_voipms_config(c)
            user = (cfg.get("voipms_api_username") or "").strip()
            pw = cfg.get("voipms_api_password") or ""
        if not user or not pw:
            return ""
        now_e = _dt.now(voipms_sms.VOIPMS_TZ)
        rows = voipms_sms.get_mms_rows(user, pw, did_raw,
                                       (now_e - _td(days=2)).strftime("%Y-%m-%d"),
                                       now_e.strftime("%Y-%m-%d"), timeout=15)
        for row in rows:
            if str(row.get("id") or "").strip() == voipms_id and str(row.get("type")) == "1":
                return voipms_sms.mms_row_to_body(row)
    except Exception as e:
        log.warning("voip.ms MMS enrich %s: %s", voipms_id, e)
    return ""


def _handle_voipms_inbound(params: dict) -> tuple[bool, str]:
    """Store + route an inbound voip.ms SMS/MMS. Returns (handled, reason)."""
    did_raw = params.get("to") or params.get("did") or params.get("TO") or ""
    from_raw = params.get("from") or params.get("FROM") or ""
    body = params.get("message") or params.get("MESSAGE") or params.get("body") or ""
    voipms_id = str(params.get("id") or params.get("ID") or "").strip()
    # MMS media: voip.ms may include media_url; append as link
    media_url = params.get("media_url") or params.get("media") or ""
    if media_url and media_url not in body:
        body = (body + f"\n[MMS: {media_url}]").strip()[:1600]
    body = (body or "").strip()[:1600]
    if not did_raw or not from_raw:
        return False, "missing to/from"
    if not body and voipms_id:
        # Empty body on a callback is usually MMS (voip.ms puts no media in
        # the callback): enrich via the getMMS API before giving up.
        body = _try_mms_enrich(did_raw, voipms_id)
    if not body:
        return False, "missing message"

    with db() as c:
        # dedupe retries
        if voipms_id:
            if c.execute("SELECT 1 FROM voipms_sms_dedupe WHERE voipms_id=?", (voipms_id,)).fetchone():
                return True, "duplicate"
            c.execute("INSERT OR IGNORE INTO voipms_sms_dedupe (voipms_id) VALUES (?)", (voipms_id,))
        dest_exten = voipms_sms.lookup_sms_route(c, did_raw)
        dests = voipms_sms.split_dest_exten(dest_exten)
        if not dests:
            c.commit()
            return False, f"no SMS route for DID {did_raw}"
        valid = [d for d in dests
                 if c.execute("SELECT 1 FROM logins WHERE exten=? AND enabled=1", (d,)).fetchone()]
        if not valid:
            c.commit()
            return False, f"dest exts {','.join(dests)} not found/disabled"
        from_disp = voipms_sms.e164(from_raw) or from_raw.strip()[:32]
        # Store: from_ext = external E.164, to_ext = internal exten.
        # My Phone groups by correspondent, so the user sees "+1555..." thread.
        # The poller passes the original UTC timestamp via params["sent_at"].
        sent_at = (params.get("sent_at") or "").strip()
        # One stored copy per destination exten, so each inbox (My Phone,
        # ESP/HA) sees it. Replies stay conversation-aware via via_did.
        via_did = voipms_sms.normalize_did(did_raw)
        if sent_at and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", sent_at):
            for d in valid:
                c.execute("INSERT INTO messages (from_ext, to_ext, body, login_id, sent_at, via_did)"
                          " VALUES (?,?,?,?,?,?)",
                          (from_disp, d, body, None, sent_at, via_did))
        else:
            for d in valid:
                c.execute("INSERT INTO messages (from_ext, to_ext, body, login_id, via_did)"
                          " VALUES (?,?,?,?,?)",
                          (from_disp, d, body, None, via_did))
        c.commit()
    # Deliver to Zoiper/softphones via pbx-brain (best effort; stored anyway)
    for d in valid:
        try:
            import json as _json, urllib.request as _ur
            brain_url = os.environ.get("PBX_BRAIN_STATUS", "http://127.0.0.1:8099") + "/messages/send"
            req = _ur.Request(brain_url,
                              data=_json.dumps({"to": d, "from": from_disp, "body": body}).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
            _ur.urlopen(req, timeout=5).read()
        except Exception:
            pass
    return True, "ok"


@app.get("/hooks/voipms-sms")
async def voipms_sms_get(request: Request):
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(str(request.url)).query, keep_blank_values=True))
    # voip.ms liveness check hits the URL with no params -> answer 200
    qsl = urllib.parse.parse_qs(urllib.parse.urlparse(str(request.url)).query)
    if not _voipms_token_ok(request, qsl):
        return PlainTextResponse("forbidden", status_code=403)
    if not qs.get("from") and not qs.get("FROM"):
        return PlainTextResponse("ok")
    ok, reason = _handle_voipms_inbound({k.lower(): v for k, v in qs.items()})
    # Always return "ok" so voip.ms doesn't retry forever on route-miss;
    # check /messages and logs for misses.
    return PlainTextResponse("ok")


@app.post("/hooks/voipms-sms")
async def voipms_sms_post(request: Request):
    qsl = urllib.parse.parse_qs(urllib.parse.urlparse(str(request.url)).query)
    if not _voipms_token_ok(request, qsl):
        return PlainTextResponse("forbidden", status_code=403)
    ctype = (request.headers.get("content-type") or "").lower()
    params: dict = {}
    # query string first (voip.ms template vars)
    for k, v in qsl.items():
        params[k.lower()] = v[0] if v else ""
    try:
        if "application/json" in ctype:
            body = await request.json()
            for k, v in (body or {}).items():
                params[str(k).lower()] = str(v)
        else:
            form = await request.form()
            for k, v in form.items():
                params[str(k).lower()] = str(v)
    except Exception:
        pass
    ok, reason = _handle_voipms_inbound(params)
    return PlainTextResponse("ok")


# ------------------------------------------------- voip.ms SMS inbound poller
# URL callbacks can misfire (tunnel hiccup, timeout) and lose a message.
# The poller is the safety net: every 60s it fetches getSMS per routed DID
# and ingests anything the webhook missed. Webhook = instant, poll = truth.
def _voipms_poll_once() -> tuple[int, int]:
    """One poll cycle. Returns (dids_polled, missed_messages_ingested)."""
    from datetime import datetime as _dt, timedelta as _td
    with db() as c:
        cfg = voipms_sms.get_voipms_config(c)
        user = (cfg.get("voipms_api_username") or "").strip()
        pw = cfg.get("voipms_api_password") or ""
        dids = [r["did"] for r in c.execute("SELECT did FROM did_sms_routes").fetchall()]
    if not user or not pw or not dids:
        return 0, 0
    now_e = _dt.now(voipms_sms.VOIPMS_TZ)
    date_to = now_e.strftime("%Y-%m-%d")
    date_from = (now_e - _td(days=2)).strftime("%Y-%m-%d")
    ingested = 0
    for did in dids:
        # SMS first, then MMS (same row shape; MMS rows carry col_media*).
        for is_mms, fetch, mname in ((False, voipms_sms.get_sms_rows, "getSMS"),
                                     (True, voipms_sms.get_mms_rows, "getMMS")):
            try:
                rows = fetch(user, pw, did, date_from, date_to)
            except Exception as e:
                log.warning("voip.ms poll %s %s: %s", mname, did, e)
                continue
            for row in rows:
                try:
                    if str(row.get("type")) != "1":
                        continue  # inbound only; skip our own outbound echo
                    msg_id = str(row.get("id") or "").strip()
                    if not msg_id:
                        continue
                    with db() as c:
                        seen = c.execute(
                            "SELECT 1 FROM voipms_sms_dedupe WHERE voipms_id=?", (msg_id,)).fetchone()
                    if seen:
                        continue
                    if is_mms:
                        body = voipms_sms.mms_row_to_body(row)
                    else:
                        body = voipms_sms.decode_voipms_body(row.get("message")).strip()[:1600]
                    if not body:
                        # empty-body row: mark seen, skip
                        with db() as c:
                            c.execute("INSERT OR IGNORE INTO voipms_sms_dedupe (voipms_id) VALUES (?)",
                                      (msg_id,))
                            c.commit()
                        continue
                    ok, _reason = _handle_voipms_inbound({
                        "to": str(row.get("did") or did),
                        "from": str(row.get("contact") or ""),
                        "message": body,
                        "id": msg_id,
                        "sent_at": voipms_sms.parse_voipms_date(row.get("date")) or "",
                    })
                    if ok:
                        ingested += 1
                        log.info("voip.ms poll: recovered missed %s id=%s from=%s",
                                 "MMS" if is_mms else "SMS",
                                 msg_id, str(row.get("contact") or "")[:16])
                except Exception as e:
                    log.warning("voip.ms poll ingest: %s", e)
    return len(dids), ingested


def _voipms_poll_loop():
    import time
    time.sleep(45)  # let boot settle
    while True:
        try:
            _dids, n = _voipms_poll_once()
        except Exception as e:
            log.warning("voip.ms poll cycle: %s", e)
        time.sleep(60)


@app.on_event("startup")
def _start_voipms_poll():
    import threading
    threading.Thread(target=_voipms_poll_loop, daemon=True, name="voipms-poll").start()


# ---------------------------------------------------------------- voip.ms SMS admin UI
@app.get("/sms-routes", response_class=HTMLResponse)
def sms_routes_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    import urllib.parse as _up
    with db() as c:
        routes = c.execute("SELECT did, dest_exten, label FROM did_sms_routes ORDER BY did").fetchall()
        cfg = voipms_sms.get_voipms_config(c)
        exts = [r["exten"] for r in c.execute("SELECT exten FROM logins WHERE enabled=1 ORDER BY exten")]
    tr = "".join(
        f"<tr><td>{esc(voipms_sms.e164(r['did']))}</td><td>{esc(r['dest_exten']).replace(',', ', ')}</td>"
        f"<td>{esc(r['label'])}</td>"
        f"<td><a class='btn ghost' href='/sms-routes/{esc(r['did'])}/edit'>Edit</a> "
        f"<a class='btn ghost' href='/sms-routes/{esc(r['did'])}/delete' onclick=\"return confirm('Delete route for {esc(r['did'])}?')\">Delete</a></td></tr>"
        for r in routes)
    # webhook URL preview
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "your-pbx").split(",")[0].strip()
    scheme = "https" if is_https(request) else "http"
    tok = cfg.get("voipms_webhook_token") or "<set-token-below>"
    hook = f"{scheme}://{host}/hooks/voipms-sms?token={tok}&to={{TO}}&from={{FROM}}&message={{MESSAGE}}&id={{ID}}&date={{TIMESTAMP}}"
    body = f"""<h2>SMS / MMS (voip.ms)</h2>
<p class="muted">Inbound: voip.ms URL Callback -> this PBX -> Zoiper (SIP MESSAGE) + My Phone + ESP. Outbound: ext -> external number -> voip.ms API.</p>
<h3>DID routes (all 4 DIDs)</h3>
<table><tr><th>DID</th><th>To extension</th><th>Label</th><th></th></tr>{tr or '<tr><td colspan=4 class=muted>No routes yet — add your 4 DIDs below.</td></tr>'}</table>
<p><a class="btn" href="/sms-routes/new/edit">Add DID route</a></p>
<h3>Webhook URL (paste into voip.ms per DID)</h3>
<p><code style="word-break:break-all">{esc(hook)}</code></p>
<p class="muted">In voip.ms: DID Numbers &gt; Manage DIDs &gt; Edit &gt; Message Service (SMS/MMS) &gt; enable, paste URL, choose E.164, Apply.</p>
<h3>voip.ms API</h3>
<form method="post" action="/sms-routes/settings">
{_csrf_field(s)}
<label>API username (voip.ms login email)<br><input name="voipms_api_username" value="{esc(cfg.get('voipms_api_username',''))}" size="40"></label><br>
<label>API password<br><input name="voipms_api_password" type="password" value="" placeholder="{'(set)' if cfg.get('voipms_api_password') else ''}"></label><br>
<label>Webhook token (random string in URL)<br><input name="voipms_webhook_token" value="{esc(cfg.get('voipms_webhook_token',''))}" size="40"></label><br>
<label>Default DID for outbound (digits)<br><input name="voipms_default_did" value="{esc(cfg.get('voipms_default_did',''))}" size="20"></label><br>
<button class="btn" type="submit">Save</button>
</form>
<p class="muted">API password is the voip.ms <b>API Password</b> (Main Menu &gt; SOAP and REST/JSON API), not your login password. Stored in kv_settings.</p>
"""
    return page("SMS routes", body, s["username"], s["role"], "sms")


@app.get("/sms-routes/{did}/edit", response_class=HTMLResponse)
def sms_route_edit_page(request: Request, did: str):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    r = None
    if did != "new":
        with db() as c:
            r = c.execute("SELECT * FROM did_sms_routes WHERE did=?", (voipms_sms.normalize_did(did),)).fetchone()
            exts = [x["exten"] for x in c.execute("SELECT exten FROM logins WHERE enabled=1 ORDER BY exten")]
    else:
        with db() as c:
            exts = [x["exten"] for x in c.execute("SELECT exten FROM logins WHERE enabled=1 ORDER BY exten")]
    def v(k, d=""):
        return esc(r[k] if r and r[k] else d)
    cur = set()
    if r and r["dest_exten"]:
        cur = set(voipms_sms.split_dest_exten(r["dest_exten"]))
    opts = "".join(f"<label style='display:block'><input type='checkbox' name='dest_exten' value='{esc(e)}' {'checked' if e in cur else ''}> {esc(e)}</label>" for e in exts)
    body = f"""<h2>{'Edit' if r else 'Add'} DID SMS route</h2>
<form method="post" action="/sms-routes/{esc(did)}/edit">
{_csrf_field(s)}
<label>DID (digits, e.g. 15551234567)<br><input name="new_did" value="{v('did', '' if did=='new' else did)}" {'readonly' if r else 'required'}></label><br>
<label>Destination extensions (check one or more)<br>{opts}</label><br>
<label>Label<br><input name="label" value="{v('label')}" size="40" placeholder="e.g. Main line"></label><br><br>
<button class="btn" type="submit">Save</button> <a class="btn ghost" href="/sms-routes">Cancel</a>
</form>"""
    return page("Edit SMS route", body, s["username"], s["role"], "sms")


@app.post("/sms-routes/{did}/edit")
async def sms_route_edit_save(request: Request, did: str):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "msgs")
    await _check_csrf(request, s)
    f = await request.form()
    new_did = voipms_sms.normalize_did((f.get("new_did") or did).strip())
    picked = [e.strip() for e in f.getlist("dest_exten") if e.strip()]
    label = (f.get("label") or "").strip()[:80]
    if not new_did or not picked:
        return RedirectResponse("/sms-routes", status_code=303)
    with db() as c:
        ok_exts = {x["exten"] for x in c.execute("SELECT exten FROM logins WHERE enabled=1")}
        dests = [e for e in dict.fromkeys(picked) if e in ok_exts]
        if not dests:
            return RedirectResponse("/sms-routes", status_code=303)
        dest_exten = ",".join(dests)
        c.execute("INSERT INTO did_sms_routes (did, dest_exten, label) VALUES (?,?,?) "
                  "ON CONFLICT(did) DO UPDATE SET dest_exten=excluded.dest_exten, label=excluded.label",
                  (new_did, dest_exten, label))
        c.commit()
    _audit_log(s["username"], "sms-routes.save", f"{new_did}->{dest_exten}")
    return RedirectResponse("/sms-routes", status_code=303)


@app.get("/sms-routes/{did}/delete")
async def sms_route_delete(request: Request, did: str):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        c.execute("DELETE FROM did_sms_routes WHERE did=?", (voipms_sms.normalize_did(did),))
        c.commit()
    return RedirectResponse("/sms-routes", status_code=303)


@app.post("/sms-routes/settings")
async def sms_routes_settings_save(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "msgs")
    await _check_csrf(request, s)
    f = await request.form()
    import secrets as _sec
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('voipms_api_username', ?)",
                  ((f.get("voipms_api_username") or "").strip(),))
        pw = (f.get("voipms_api_password") or "").strip()
        if pw:
            c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('voipms_api_password', ?)", (pw,))
        tok = (f.get("voipms_webhook_token") or "").strip()
        if not tok:
            tok = _sec.token_hex(16)
        else:
            # People paste the whole webhook URL by mistake; pull the token out.
            _m = re.search(r"[?&]token=([^&\s]+)", tok)
            if _m:
                tok = _m.group(1)
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('voipms_webhook_token', ?)", (tok,))
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('voipms_default_did', ?)",
                  (voipms_sms.normalize_did((f.get("voipms_default_did") or "").strip()),))
        c.commit()
    _audit_log(s["username"], "sms-routes.settings", "updated")
    return RedirectResponse("/sms-routes", status_code=303)


# ---------------------------------------------------------------- feature codes admin
@app.get("/features", response_class=HTMLResponse)
def features_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        feats = c.execute("SELECT code, name, description, enabled, default_access"
                          " FROM feature_codes ORDER BY code").fetchall()
        exc = {r["code"]: r["n"] for r in c.execute(
            "SELECT code, COUNT(*) n FROM login_feature_access GROUP BY code")}
    def access_label(a):
        return {"all": "All users", "admin": "Admins only", "none": "No one"}.get(a, a)
    tr = "".join(
        f"<tr><td><code>{esc(f['code'])}</code></td><td>{esc(f['name'])}"
        f"<br><span class='muted'>{esc(f['description'])}</span></td>"
        f"<td>{'Yes' if f['enabled'] else '<b>No</b>'}</td>"
        f"<td>{esc(access_label(f['default_access']))}</td>"
        f"<td>{exc.get(f['code'], 0)}</td>"
        f"<td><a class='btn ghost' href='/features/{esc(f['code'])}/edit'>Edit</a></td></tr>"
        for f in feats)
    body = f"""<h2>Feature codes</h2>
<p class="muted">Star codes users can dial from their phones (e.g. <code>*97</code> for voicemail).
Turn each code on/off, set who may use it by default, and override per user below.</p>
<table><tr><th>Code</th><th>Name</th><th>Enabled</th><th>Default access</th><th>User overrides</th><th></th></tr>
{tr or '<tr><td colspan=6 class=muted>No feature codes.</td></tr>'}</table>
"""
    return page("Feature codes", body, s["username"], s["role"], "features")


@app.get("/features/{code}/edit", response_class=HTMLResponse)
def feature_edit_page(request: Request, code: str):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        f = c.execute("SELECT * FROM feature_codes WHERE code=?", (code,)).fetchone()
        if not f:
            return RedirectResponse("/features")
        logins = c.execute("SELECT id, exten, display_name, role FROM logins"
                           " WHERE enabled=1 ORDER BY exten").fetchall()
        overrides = {r["login_id"]: r["allowed"] for r in c.execute(
            "SELECT login_id, allowed FROM login_feature_access WHERE code=?", (code,))}
    def acc_opt(val, label):
        sel = "selected" if f["default_access"] == val else ""
        return f"<option value='{val}' {sel}>{label}</option>"
    urows = []
    for u in logins:
        ov = overrides.get(u["id"])
        sel_d = "selected" if ov is None else ""
        sel_a = "selected" if ov == 1 else ""
        sel_n = "selected" if ov == 0 else ""
        nm = esc(u["display_name"] or u["exten"])
        urows.append(
            f"<tr><td>{esc(u['exten'])}</td><td>{nm}</td><td>{esc(u['role'])}</td>"
            f"<td><select name='u_{u['id']}'>"
            f"<option value='' {sel_d}>Default</option>"
            f"<option value='1' {sel_a}>Allow</option>"
            f"<option value='0' {sel_n}>Deny</option>"
            f"</select></td></tr>")
    body = f"""<h2>Edit feature <code>{esc(f['code'])}</code> — {esc(f['name'])}</h2>
<p class="muted">{esc(f['description'])}</p>
<form method="post" action="/features/{esc(code)}/edit">
{_csrf_field(s)}
<label><input type="checkbox" name="enabled" value="1" {'checked' if f['enabled'] else ''}> Enabled</label><br><br>
<label>Default access<br><select name="default_access">
{acc_opt('all', 'All users')}
{acc_opt('admin', 'Admins only')}
{acc_opt('none', 'No one (allow specific users below)')}
</select></label><br><br>
<h3>Per-user overrides</h3>
<p class="muted">Leave on Default to follow the setting above, or force Allow/Deny per user.</p>
<table><tr><th>Ext</th><th>Name</th><th>Role</th><th>Access</th></tr>
{''.join(urows)}
</table><br>
<button class="btn" type="submit">Save</button> <a class="btn ghost" href="/features">Cancel</a>
</form>
"""
    return page("Edit feature", body, s["username"], s["role"], "features")


@app.post("/features/{code}/edit")
async def feature_edit_save(request: Request, code: str):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "features")
    await _check_csrf(request, s)
    f = await request.form()
    enabled = 1 if f.get("enabled") else 0
    default_access = (f.get("default_access") or "all").strip()
    if default_access not in ("all", "admin", "none"):
        default_access = "all"
    with db() as c:
        c.execute("UPDATE feature_codes SET enabled=?, default_access=? WHERE code=?",
                  (enabled, default_access, code))
        # per-user overrides: only store non-default rows
        for key in f.keys():
            if not key.startswith("u_"):
                continue
            try:
                login_id = int(key[2:])
            except ValueError:
                continue
            val = (f.get(key) or "").strip()
            if val == "":
                c.execute("DELETE FROM login_feature_access WHERE login_id=? AND code=?",
                          (login_id, code))
            elif val in ("0", "1"):
                c.execute("INSERT OR REPLACE INTO login_feature_access (login_id, code, allowed)"
                          " VALUES (?,?,?)", (login_id, code, int(val)))
        c.commit()
    _audit_log(s["username"], "features.edit", code)
    return RedirectResponse("/features", status_code=303)


# ---------------------------------------------------------------- branding
BRAND_IMAGE_TYPES = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
    "image/svg+xml": "svg", "image/webp": "webp", "image/x-icon": "ico",
}
BRAND_MAX_UPLOAD = 500 * 1024  # 500 KB


def _brand_image_to_data_uri(upload):
    """Validate an uploaded image and return a data URI, or '' / None.

    Returns '' when no file was uploaded, None when the file is rejected.
    """
    if upload is None or not getattr(upload, "filename", ""):
        return ""
    ctype = (upload.content_type or "").split(";")[0].strip().lower()
    if ctype not in BRAND_IMAGE_TYPES:
        return None
    data = upload.file.read(BRAND_MAX_UPLOAD + 1)
    if not data or len(data) > BRAND_MAX_UPLOAD:
        return None
    import base64 as _b64
    return "data:%s;base64,%s" % (ctype, _b64.b64encode(data).decode("ascii"))


def _brand_settings():
    with db() as c:
        rows = c.execute("SELECT key, value FROM kv_settings WHERE key LIKE 'brand_%'").fetchall()
    d = dict(BRAND_DEFAULTS)
    for r in rows:
        d[r["key"][6:]] = r["value"]
    return d


@app.get("/branding", response_class=HTMLResponse)
def branding_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    b = _brand_settings()
    def v(k):
        return esc(b.get(k, ""))
    def sel(cur, val):
        return "selected" if cur == val else ""
    def chk(val):
        return "checked" if val == "1" else ""
    logo_preview = (f'<img src="{esc(b["logo"])}" style="max-height:48px;max-width:220px;border:1px solid #eee;border-radius:4px"><br>'
                    if b["logo"] else '<span class="muted">No logo uploaded.</span><br>')
    fav_preview = (f'<img src="{esc(b["favicon"])}" style="height:16px;width:16px"><br>'
                   if b["favicon"] else "")
    _presets = "".join(
        '<button type="button" class="preset" data-header="%s" data-accent="%s" title="%s" '
        'style="width:30px;height:30px;border-radius:50%%;border:2px solid #999;cursor:pointer;'
        'margin:2px;background:linear-gradient(135deg,%s 50%%,%s 50%%)"></button>' % (h, a, n, h, a)
        for n, h, a in [
            ("Default", "#1a1a2e", "#1a1a2e"), ("Ocean", "#0b3d66", "#0099cc"),
            ("Midnight", "#101024", "#5a5ad6"), ("Sunset", "#431407", "#ea580c"),
            ("Forest", "#0c2417", "#22a355"), ("Royal", "#2b0f4d", "#8b3fd9"),
            ("Crimson", "#3d0a12", "#e0263c"), ("Amber", "#2e2008", "#d9a416"),
        ])
    body = f"""<h2>Branding</h2>
<p class="muted">White-label the panel: your name, logo, colors, login page and footer. Changes apply immediately.</p>
<form method="post" action="/branding" enctype="multipart/form-data">
{_csrf_field(s)}
<h3>Site identity</h3>
<label>Site name<br><input name="site_name" value="{v('site_name')}" size="40" maxlength="60"></label><br>
<label>Header shows<br><select name="header_mode">
<option value="text" {sel(b['header_mode'],'text')}>Text only</option>
<option value="logo" {sel(b['header_mode'],'logo')}>Logo only</option>
<option value="both" {sel(b['header_mode'],'both')}>Logo + text</option>
</select></label><br>
<label>Logo image<br><input type="file" name="logo_file" accept="image/*"></label><br>
<label>…or logo image URL<br><input name="logo_url" value="{'' if b['logo'].startswith('data:') else v('logo')}" size="60" placeholder="https://…"></label><br>
{logo_preview}
<label><input type="checkbox" name="logo_remove" value="1"> Remove current logo</label><br>
<label>Dark mode logo (optional)<br><input type="file" name="logo_dark_file" accept="image/*"></label><br>
<label>…or image URL<br><input name="logo_dark_url" value="{'' if b['logo_dark'].startswith('data:') else v('logo_dark')}" size="60" placeholder="https://…"></label><br>
{f'<img src="'+esc(b["logo_dark"])+'" style="max-height:48px;max-width:220px;border:1px solid #eee;border-radius:4px;background:#222"><br>' if b["logo_dark"] else '<span class="muted">No dark mode logo — main logo is used in both modes.</span><br>'}
<label><input type="checkbox" name="logo_dark_remove" value="1"> Remove dark mode logo</label>
<h3>Colors</h3>
<label>Header background<br><input type="color" name="header_color" value="{v('header_color') or '#1a1a2e'}"></label><br>
<label>Buttons &amp; active tabs<br><input type="color" name="accent_color" value="{v('accent_color') or '#1a1a2e'}"></label><br>
<label>Default theme<br><select name="theme_default">
<option value="dark" {sel(b['theme_default'],'dark')}>Dark</option>
<option value="light" {sel(b['theme_default'],'light')}>Light</option>
</select></label>
<p class="muted">Visitors can switch with the 🌙/☀️ icon in the header; their choice is remembered in the browser.</p>
<label>Time zone<br><select name="timezone">
{"".join(f'<option value="{tz}" {sel(b["timezone"], tz)}>{label}</option>' for tz, label in [
    ("America/Los_Angeles", "Los Angeles (PT)"), ("America/Denver", "Denver (MT)"),
    ("America/Chicago", "Chicago (CT)"), ("America/New_York", "New York (ET)"),
    ("America/Anchorage", "Anchorage (AKT)"), ("Pacific/Honolulu", "Honolulu (HST)"),
    ("UTC", "UTC")])}</select></label>
<p class="muted">Call/message times show in 12-hour format in this zone.</p>
<label>Color presets<br><span class="muted">Click to apply:</span><br>
{_presets}</label>
<script>
document.querySelectorAll('.preset').forEach(function(btn){{btn.addEventListener('click',function(){{
  var h=document.querySelector('input[name=header_color]');
  var a=document.querySelector('input[name=accent_color]');
  if(h)h.value=btn.dataset.header; if(a)a.value=btn.dataset.accent;
}});}});
</script><br>
<label><input type="checkbox" name="header_animated" value="1" {chk(b['header_animated'])}> Animated gradient header <span class="muted">(flows between the header and accent colors)</span></label><br>
<h3>Login page</h3>
<label>Shows<br><select name="login_mode">
<option value="text" {sel(b['login_mode'],'text')}>Text only</option>
<option value="logo" {sel(b['login_mode'],'logo')}>Logo only</option>
<option value="both" {sel(b['login_mode'],'both')}>Logo + text</option>
</select></label><br>
<label>Login page logo (blank = use header logo)<br><input type="file" name="login_logo_file" accept="image/*"></label><br>
<label>…or image URL<br><input name="login_logo_url" value="{'' if b['login_logo'].startswith('data:') else v('login_logo')}" size="60" placeholder="https://…"></label><br>
{f'<img src="'+esc(b["login_logo"])+'" style="max-height:48px;max-width:220px;border:1px solid #eee;border-radius:4px"><br>' if b["login_logo"] else '<span class="muted">Using header logo.</span><br>'}
<label><input type="checkbox" name="login_logo_remove" value="1"> Remove login logo (fall back to header logo)</label><br>
<label>Dark mode login logo (optional)<br><input type="file" name="login_logo_dark_file" accept="image/*"></label><br>
<label>…or image URL<br><input name="login_logo_dark_url" value="{'' if b['login_logo_dark'].startswith('data:') else v('login_logo_dark')}" size="60" placeholder="https://…"></label><br>
{f'<img src="'+esc(b["login_logo_dark"])+'" style="max-height:48px;max-width:220px;border:1px solid #eee;border-radius:4px;background:#222"><br>' if b["login_logo_dark"] else '<span class="muted">No dark mode login logo — day logo is used in both modes.</span><br>'}
<label><input type="checkbox" name="login_logo_dark_remove" value="1"> Remove dark mode login logo</label><br>
<label>Heading (blank = site name)<br><input name="login_title" value="{v('login_title')}" size="40" maxlength="80"></label><br>
<label>Subheading<br><input name="login_subtitle" value="{v('login_subtitle')}" size="60" maxlength="140"></label><br>
<h3>Favicon</h3>
<label>Icon image<br><input type="file" name="favicon_file" accept="image/*"></label><br>
<label>…or icon URL<br><input name="favicon_url" value="{'' if b['favicon'].startswith('data:') else v('favicon')}" size="60" placeholder="https://…"></label><br>
{fav_preview}
<label><input type="checkbox" name="favicon_remove" value="1"> Remove current favicon</label>
<h3>Footer</h3>
<label><input type="checkbox" name="footer_show" value="1" {chk(b['footer_show'])}> Show footer on every page</label><br>
<label>Footer text<br><input name="footer_text" value="{v('footer_text')}" size="60" maxlength="140" placeholder="© 2026 My Company"></label><br><br>
<button class="btn" type="submit">Save branding</button>
</form>
<h3>Preview</h3>
<div class="nav" style="{'background:'+esc(b['header_color']) if b['header_color'] else ''}">
  {"<img src='"+esc(b["logo"])+"' style='height:28px;vertical-align:middle;border-radius:4px'>" if b["logo"] and b["header_mode"] in ("logo","both") else ""}
  {("<strong style='margin-left:8px'>"+esc(b["site_name"])+"</strong>") if b["header_mode"] in ("text","both") else ""}
  <span class="sp"></span><span>preview</span>
</div>
<p class="muted">Logos are stored in the database (max 500 KB: PNG, JPEG, GIF, SVG, WebP). They ride along with database backups.</p>
"""
    return page("Branding", body, s["username"], s["role"], "branding")


@app.post("/branding")
async def branding_save(request: Request,
                        logo_file: UploadFile = File(None),
                        favicon_file: UploadFile = File(None),
                        login_logo_file: UploadFile = File(None),
                        logo_dark_file: UploadFile = File(None),
                        login_logo_dark_file: UploadFile = File(None)):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "branding")
    await _check_csrf(request, s)
    f = await request.form()
    import re as _re
    def clean_color(val, fallback):
        val = (val or "").strip()
        return val if _re.fullmatch(r"#[0-9a-fA-F]{6}", val) else fallback
    vals = {
        "site_name": (f.get("site_name") or "").strip()[:60] or "PBX Panel",
        "header_mode": (f.get("header_mode") or "text") if (f.get("header_mode") in ("text", "logo", "both")) else "text",
        "header_color": clean_color(f.get("header_color"), "#1a1a2e"),
        "accent_color": clean_color(f.get("accent_color"), "#1a1a2e"),
        "login_mode": (f.get("login_mode") or "text") if (f.get("login_mode") in ("text", "logo", "both")) else "text",
        "login_title": (f.get("login_title") or "").strip()[:80],
        "login_subtitle": (f.get("login_subtitle") or "").strip()[:140],
        "footer_show": "1" if f.get("footer_show") else "0",
        "footer_text": (f.get("footer_text") or "").strip()[:140],
        "theme_default": (f.get("theme_default") or "dark") if (f.get("theme_default") in ("dark", "light")) else "dark",
        "header_animated": "1" if f.get("header_animated") else "0",
        "timezone": (f.get("timezone") or "America/Los_Angeles")
        if (f.get("timezone") in ("America/Los_Angeles", "America/Denver", "America/Chicago",
                                  "America/New_York", "America/Anchorage", "Pacific/Honolulu", "UTC"))
        else "America/Los_Angeles",
    }
    def _save_image(field_prefix, label):
        """Handle upload/URL/remove for an image setting. Returns (key, value)
        to store, (None, None) to keep existing, or raises via error page."""
        up = {"logo_dark": logo_dark_file, "login_logo_dark": login_logo_dark_file}[field_prefix]
        uri = _brand_image_to_data_uri(up)
        if uri is None:
            raise ValueError(label)
        url = (f.get(field_prefix + "_url") or "").strip()[:500]
        if f.get(field_prefix + "_remove"):
            return field_prefix, ""
        if uri:
            return field_prefix, uri
        if url.startswith(("https://", "http://", "data:")):
            return field_prefix, url
        return None, None
    for _prefix, _label in (("logo_dark", "Dark mode logo"), ("login_logo_dark", "Dark mode login logo")):
        try:
            _k, _v = _save_image(_prefix, _label)
        except ValueError as e:
            return HTMLResponse(page("Branding", f"<h2>Branding</h2><p style='color:red'>{esc(str(e))} rejected: use a PNG, JPEG, GIF, SVG or WebP under 500 KB.</p><p><a href='/branding'>Back</a></p>",
                                      s["username"], s["role"], "branding"), status_code=400)
        if _k:
            vals[_k] = _v
    # Logo: upload wins, else URL field, else keep existing unless removed.
    logo_uri = _brand_image_to_data_uri(logo_file)
    if logo_uri is None:
        return HTMLResponse(page("Branding", "<h2>Branding</h2><p style='color:red'>Logo rejected: use a PNG, JPEG, GIF, SVG or WebP under 500 KB.</p><p><a href='/branding'>Back</a></p>",
                                  s["username"], s["role"], "branding"), status_code=400)
    logo_url = (f.get("logo_url") or "").strip()[:500]
    if f.get("logo_remove"):
        vals["logo"] = ""
    elif logo_uri:
        vals["logo"] = logo_uri
    elif logo_url.startswith(("https://", "http://", "data:")):
        vals["logo"] = logo_url
    fav_uri = _brand_image_to_data_uri(favicon_file)
    if fav_uri is None:
        return HTMLResponse(page("Branding", "<h2>Branding</h2><p style='color:red'>Favicon rejected: use a PNG, JPEG, GIF, SVG or WebP under 500 KB.</p><p><a href='/branding'>Back</a></p>",
                                  s["username"], s["role"], "branding"), status_code=400)
    fav_url = (f.get("favicon_url") or "").strip()[:500]
    if f.get("favicon_remove"):
        vals["favicon"] = ""
    elif fav_uri:
        vals["favicon"] = fav_uri
    elif fav_url.startswith(("https://", "http://", "data:")):
        vals["favicon"] = fav_url
    # Login page logo: upload wins, else URL, else keep unless removed.
    login_uri = _brand_image_to_data_uri(login_logo_file)
    if login_uri is None:
        return HTMLResponse(page("Branding", "<h2>Branding</h2><p style='color:red'>Login logo rejected: use a PNG, JPEG, GIF, SVG or WebP under 500 KB.</p><p><a href='/branding'>Back</a></p>",
                                  s["username"], s["role"], "branding"), status_code=400)
    login_url = (f.get("login_logo_url") or "").strip()[:500]
    if f.get("login_logo_remove"):
        vals["login_logo"] = ""
    elif login_uri:
        vals["login_logo"] = login_uri
    elif login_url.startswith(("https://", "http://", "data:")):
        vals["login_logo"] = login_url
    with db() as c:
        # logo/favicon/login_logo/logo_dark/login_logo_dark are only in vals
        # when uploaded, pasted or removed; otherwise the existing value
        # stays untouched.
        for k, val in vals.items():
            c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?, ?)",
                      ("brand_" + k, val))
        c.commit()
    _audit_log(s["username"], "branding.save", "updated")
    return RedirectResponse("/branding", status_code=303)



async def _admin_bulk_form(request, tab):
    """Common checks for admin bulk deletes.
    Returns (session, form, everything, ids) or (None, response, ...)."""
    s = _sess(request)
    if not s or s["role"] != "admin":
        return None, RedirectResponse("/login", status_code=303), False, []
    await _check_csrf(request, s)
    if _get_setting("safety_lock") == "1":
        return None, _panel_locked(s, tab), False, []
    form = await request.form()
    everything = form.get("mode") == "all"
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()][:5000]
    return s, form, everything, ids


def _unlink_under(path, root):
    try:
        if path and os.path.commonpath([os.path.abspath(path), root]) == root and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def _deleted_flash(request):
    d = request.query_params.get("deleted")
    if d is not None and d.isdigit():
        return f"<div class='flash ok'>Deleted {int(d)}.</div>"
    return ""


def _shown(n, total, noun):
    if total > n:
        return f"<p class='muted'>Showing the latest {n} of {total} {noun}s. Delete all removes every one.</p>"
    return ""


@app.get("/api/recording-audio/{rec_id}")
def rec_audio(rec_id: int, request: Request, download: int = 0):
    s = _sess(request)
    if not s:
        raise HTTPException(401)
    if s["role"] != "admin" and not ucp.can_play_recording(s, rec_id):
        raise HTTPException(403)
    with db() as c:
        row = c.execute("SELECT path FROM recordings WHERE id=?", (rec_id,)).fetchone()
    if not row or not row["path"]:
        raise HTTPException(404)
    fpath = row["path"]
    if os.path.commonpath([os.path.abspath(fpath), "/var/spool/pbx/monitor"]) != "/var/spool/pbx/monitor":
        raise HTTPException(403)
    if not os.path.isfile(fpath):
        raise HTTPException(404)
    fname = os.path.basename(fpath)
    headers = {"Content-Disposition": f"attachment; filename={fname}"} if download else {}
    return FileResponse(fpath, media_type="audio/wav", headers=headers)


# ---------------------------------------------------------------- v1 REST API
# "apis are the ext": JSON API with per-login Bearer tokens for managing
# extensions (logins) programmatically.
#
# Auth: Authorization: Bearer pbx_<token>. Only the sha256 hash is stored;
# the raw token is shown once, at creation.
# Secrets (sip_secret, login password) are only ever returned once, at
# creation/rotation time. They never appear in GET responses.
# Mutations regenerate Asterisk configs via the privileged wrapper and
# report "applied": true/false so callers know whether to retry.

import hashlib
import re

APPLY_SCRIPT = "/opt/pbx/bin/pbx-apply-config.sh"
EXT_RE = re.compile(r"^\d{2,6}$")
# Names that end up in pjsip.conf section headers / SIP usernames.
# Strict charset kills config-section injection at the source
# (gen.py also strips control chars as defense in depth).
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _valid_name(v: str, what: str = "name") -> str:
    v = (v or "").strip()
    if not NAME_RE.match(v):
        raise HTTPException(400, f"{what} must match [A-Za-z0-9_.-], 1-64 chars")
    return v

# SIP secrets are written into pjsip.conf as `password=<value>`. Printable
# ASCII without spaces, ';' (starts a pjsip.conf comment) or backslash.
SIP_SECRET_RE = re.compile(r"^[!-~]{8,128}$")


def _valid_sip_secret(v: str) -> str:
    if not SIP_SECRET_RE.match(v) or ";" in v or "\\" in v:
        raise HTTPException(400, "sip_secret must be 8-128 printable characters "
                                 "with no spaces, ';' or '\\'")
    return v


EXT_PUBLIC_FIELDS = ("id", "username", "role", "exten", "sip_username",
                     "display_name", "vm_email", "record_admin", "user_record",
                     "max_messages", "enabled", "created_at")


def _public_ext(row) -> dict:
    d = dict(row)
    return {k: d.get(k) for k in EXT_PUBLIC_FIELDS}


def apply_config():
    """Regenerate Asterisk configs from the DB and reload PJSIP.

    Runs the root-owned wrapper via sudo (pbx service user). Raises on failure.
    """
    r = subprocess.run(["sudo", "-n", APPLY_SCRIPT],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(
            "apply-config failed: " + (r.stderr.strip() or r.stdout.strip()
                                       or f"exit {r.returncode}"))


def _apply_guarded():
    """apply_config() -> (applied: bool, error: str | None)."""
    try:
        apply_config()
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _best_effort_apply():
    """apply_config() that never raises (panel handlers). DB is the source
    of truth; a failure can be retried via POST /system/reload."""
    try:
        apply_config()
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: apply-config failed: {e}", flush=True)


def _api_auth(request: Request) -> dict:
    """Bearer token auth. Returns login row + key info. 401 on failure."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "bearer token required")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "bearer token required")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        row = c.execute(
            """SELECT k.id AS key_id, k.name AS key_name, l.*
               FROM api_keys k JOIN logins l ON l.id = k.login_id
               WHERE k.key_hash = ? AND k.enabled = 1 AND l.enabled = 1""",
            (digest,)).fetchone()
        if not row:
            raise HTTPException(401, "invalid api key")
        c.execute("UPDATE api_keys SET last_used_at = datetime('now') WHERE id = ?",
                  (row["key_id"],))
        c.commit()
    return dict(row)


def _v1_actor(request: Request) -> dict:
    """Bearer token or session cookie -> full login dict (any role).

    Lets the dashboard JS use the v1 JSON endpoints with its session cookie
    while API consumers use Bearer tokens. Either way the actor is a login.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return _api_auth(request)
    s = _sess(request)
    if not s:
        raise HTTPException(401, "login required")
    with db() as c:
        row = c.execute("SELECT * FROM logins WHERE username = ?",
                        (s["username"],)).fetchone()
    if not row or not row["enabled"]:
        raise HTTPException(401, "login required")
    actor = dict(row)
    actor["key_id"] = None
    actor["key_name"] = None
    return actor


def _v1_admin(request: Request) -> dict:
    """Admin actor for v1: Bearer token or admin session cookie."""
    actor = _v1_actor(request)
    if actor["role"] != "admin":
        raise HTTPException(403, "admin required")
    return actor


def _get_extension_or_404(exten: str) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM logins WHERE exten = ? AND exten != ''",
                        (exten,)).fetchone()
    if not row:
        raise HTTPException(404, "extension not found")
    return dict(row)


def _create_api_key(username: str, name: str) -> tuple[str, dict]:
    """Create a Bearer token for a login. Returns (token, info)."""
    with db() as c:
        login = c.execute("SELECT id, username, enabled FROM logins WHERE username = ?",
                          (username,)).fetchone()
        if not login:
            raise HTTPException(404, "login not found")
        if not login["enabled"]:
            raise HTTPException(400, "login is disabled")
        token = "pbx_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        cur = c.execute(
            "INSERT INTO api_keys (login_id, name, key_hash, prefix) VALUES (?,?,?,?)",
            (login["id"], name or "", digest, token[:12]))
        c.commit()
        key_id = cur.lastrowid
    return token, {"id": key_id, "prefix": token[:12],
                   "username": login["username"], "name": name or ""}


# ---------------------------------------------------------------- v1: api keys

class ApiKeyIn(BaseModel):
    username: str
    name: str = ""


@app.post("/api/v1/api-keys", status_code=201)
def v1_key_create(request: Request, body: ApiKeyIn):
    _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    token, info = _create_api_key(body.username, body.name)
    return {"key": token, **info}


@app.get("/api/v1/api-keys")
def v1_key_list(request: Request):
    _v1_admin(request)
    with db() as c:
        rows = c.execute(
            """SELECT k.id, k.name, k.prefix, k.created_at, k.last_used_at,
                      k.enabled, l.username
               FROM api_keys k JOIN logins l ON l.id = k.login_id
               ORDER BY k.created_at DESC""").fetchall()
    return {"api_keys": [dict(r) for r in rows]}


@app.delete("/api/v1/api-keys/{key_id}")
def v1_key_revoke(request: Request, key_id: int):
    _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    with db() as c:
        cur = c.execute("UPDATE api_keys SET enabled = 0 WHERE id = ?", (key_id,))
        c.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "api key not found")
    return {"ok": True}


# ---------------------------------------------------------------- v1: extensions

class ExtIn(BaseModel):
    username: str
    password: str = ""
    exten: str
    sip_username: str = ""
    sip_secret: str = ""
    display_name: str = ""
    role: str = "user"
    vm_email: str = ""
    record_admin: bool = False
    user_record: bool = False
    max_messages: int = 500
    enabled: bool = True


class ExtPatch(BaseModel):
    display_name: str | None = None
    sip_secret: str | None = None   # set a specific SIP password (echoed once)
    vm_email: str | None = None
    record_admin: bool | None = None
    user_record: bool | None = None
    max_messages: int | None = None
    enabled: bool | None = None


class PasswordIn(BaseModel):
    password: str


@app.get("/api/v1/me")
def v1_me(request: Request):
    actor = _v1_actor(request)
    return {"login": _public_ext(actor), "key_id": actor.get("key_id"),
            "key_name": actor.get("key_name")}


@app.get("/api/v1/extensions")
def v1_ext_list(request: Request):
    actor = _v1_actor(request)
    with db() as c:
        if actor["role"] == "admin":
            rows = c.execute("SELECT * FROM logins WHERE exten != '' ORDER BY exten").fetchall()
        else:
            rows = c.execute("SELECT * FROM logins WHERE id = ? AND exten != ''",
                             (actor["id"],)).fetchall()
    return {"extensions": [_public_ext(r) for r in rows]}


@app.post("/api/v1/extensions", status_code=201)
def v1_ext_create(request: Request, body: ExtIn):
    _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    if not EXT_RE.match(body.exten):
        raise HTTPException(400, "exten must be 2-6 digits")
    if body.role not in ("user", "admin"):
        raise HTTPException(400, "role must be user or admin")
    sip_u = _valid_name(body.sip_username, "sip_username") if body.sip_username else f"phone{body.exten}"
    password = body.password or secrets.token_urlsafe(16)
    generated_pw = "" if body.password else password
    pwh = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    sip_secret = body.sip_secret or secrets.token_urlsafe(24)
    with db() as c:
        if c.execute("SELECT 1 FROM logins WHERE username = ?", (body.username,)).fetchone():
            raise HTTPException(409, "username already exists")
        if c.execute("SELECT 1 FROM logins WHERE exten = ? AND exten != ''",
                     (body.exten,)).fetchone():
            raise HTTPException(409, "extension already exists")
        if c.execute("SELECT 1 FROM logins WHERE sip_username = ? AND sip_username != ''",
                     (sip_u,)).fetchone():
            raise HTTPException(409, "sip username already exists")
        cur = c.execute(
            """INSERT INTO logins (username, pwhash, role, exten, sip_username, sip_secret,
                                   display_name, vm_email, record_admin, user_record,
                                   max_messages, enabled)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (body.username, pwh, body.role, body.exten, sip_u, sip_secret,
             body.display_name or "", body.vm_email or "",
             1 if body.record_admin else 0, 1 if body.user_record else 0,
             body.max_messages, 1 if body.enabled else 0))
        new_id = cur.lastrowid
        c.execute("INSERT OR IGNORE INTO voicemail_boxes (mailbox, login_id) VALUES (?, ?)",
                  (f"vm-{body.exten}", new_id))
        c.commit()
        row = c.execute("SELECT * FROM logins WHERE id = ?", (new_id,)).fetchone()
    applied, err = _apply_guarded()
    resp = {"extension": _public_ext(row), "sip_secret": sip_secret, "applied": applied}
    if generated_pw:
        resp["password"] = generated_pw
    if err:
        resp["apply_error"] = err
    return resp


@app.get("/api/v1/extensions/{exten}")
def v1_ext_get(request: Request, exten: str):
    actor = _v1_actor(request)
    row = _get_extension_or_404(exten)
    if actor["role"] != "admin" and row["id"] != actor["id"]:
        raise HTTPException(403, "forbidden")
    return {"extension": _public_ext(row)}


@app.patch("/api/v1/extensions/{exten}")
def v1_ext_patch(request: Request, exten: str, body: ExtPatch):
    _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    row = _get_extension_or_404(exten)
    sets, vals = [], []
    for field in ("display_name", "vm_email", "max_messages"):
        v = getattr(body, field)
        if v is not None:
            sets.append(f"{field} = ?")
            vals.append(v)
    for field in ("record_admin", "user_record", "enabled"):
        v = getattr(body, field)
        if v is not None:
            sets.append(f"{field} = ?")
            vals.append(1 if v else 0)
    new_secret = None
    if body.sip_secret:  # "" / null = not provided
        new_secret = _valid_sip_secret(body.sip_secret)
        sets.append("sip_secret = ?")
        vals.append(new_secret)
    if not sets:
        raise HTTPException(400, "nothing to update")
    vals.append(row["id"])
    with db() as c:
        c.execute(f"UPDATE logins SET {', '.join(sets)} WHERE id = ?", vals)
        c.commit()
        new = c.execute("SELECT * FROM logins WHERE id = ?", (row["id"],)).fetchone()
    applied, err = _apply_guarded()
    resp = {"extension": _public_ext(new), "applied": applied}
    if new_secret is not None:
        resp["sip_secret"] = new_secret  # returned once, never in GETs
    if err:
        resp["apply_error"] = err
    return resp


@app.delete("/api/v1/extensions/{exten}")
def v1_ext_delete(request: Request, exten: str):
    _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    row = _get_extension_or_404(exten)
    with db() as c:
        box = c.execute("SELECT mailbox FROM voicemail_boxes WHERE login_id = ?",
                        (row["id"],)).fetchone()
        if box:
            c.execute("DELETE FROM voicemail_messages WHERE mailbox = ?", (box["mailbox"],))
        c.execute("DELETE FROM logins WHERE id = ?", (row["id"],))
        c.commit()
    applied, err = _apply_guarded()
    resp = {"ok": True, "applied": applied}
    if err:
        resp["apply_error"] = err
    return resp


@app.post("/api/v1/extensions/{exten}/rotate-secret")
def v1_ext_rotate_secret(request: Request, exten: str):
    _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    row = _get_extension_or_404(exten)
    new_secret = secrets.token_urlsafe(24)
    with db() as c:
        c.execute("UPDATE logins SET sip_secret = ? WHERE id = ?", (new_secret, row["id"]))
        c.commit()
    applied, err = _apply_guarded()
    resp = {"exten": exten, "sip_secret": new_secret, "applied": applied}
    if err:
        resp["apply_error"] = err
    return resp


@app.post("/api/v1/extensions/{exten}/reset-password")
def v1_ext_reset_password(request: Request, exten: str, body: PasswordIn):
    _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    row = _get_extension_or_404(exten)
    if len(body.password) < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    pwh = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    with db() as c:
        c.execute("UPDATE logins SET pwhash = ? WHERE id = ?", (pwh, row["id"]))
        c.commit()
    return {"ok": True}


# ---------------------------------------------------------------- panel: api key management (bootstrap UI)

@app.get("/api-keys", response_class=HTMLResponse)
def apikeys_page(request: Request):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with db() as c:
        rows = c.execute(
            """SELECT k.id, k.name, k.prefix, k.created_at, k.last_used_at,
                      k.enabled, l.username
               FROM api_keys k JOIN logins l ON l.id = k.login_id
               ORDER BY k.created_at DESC""").fetchall()
        logins = c.execute(
            "SELECT username FROM logins WHERE enabled = 1 ORDER BY username").fetchall()
    revoked = sum(1 for r in rows if not r["enabled"])
    tr = "".join(
        f"<tr><td>{'' if r['enabled'] else chr(60) + 'input type=checkbox class=sel name=ids value=' + str(r['id']) + ' form=akeys aria-label=Select' + chr(62)}</td>"
        f"<td><code>{esc(r['prefix'])}...</code></td><td>{esc(r['name'])}</td>"
        f"<td>{esc(r['username'])}</td><td>{fmt_ts(r['created_at'])}</td>"
        f"<td>{fmt_ts(r['last_used_at']) if r['last_used_at'] else '-'}</td>"
        f"<td>{'yes' if r['enabled'] else 'no'}</td>"
        f"<td>{'<form method=post action=/api-keys/' + str(r['id']) + '/revoke style=display:inline>' + _csrf_field(s) + '<button class=btn>Revoke</button></form>' if r['enabled'] else ''}</td></tr>"
        for r in rows)
    opts = "".join(f"<option value='{esc(l['username'])}'>{esc(l['username'])}</option>" for l in logins)
    body = f"""<h2>API Keys</h2>
<p>Bearer tokens for the <code>/api/v1</code> REST API. The full key is shown
<strong>once</strong> at creation.</p>
{_deleted_flash(request)}{ucp._bulkbar(s, "akeys", "/api-keys/delete", revoked, "revoked API keys")}
<table><tr><th><input type="checkbox" id="akeys-all" aria-label="Select all revoked"></th><th>Key</th><th>Name</th><th>Login</th><th>Created</th><th>Last used</th><th>Active</th><th></th></tr>
{tr or '<tr><td colspan=8>No API keys yet</td></tr>'}</table>
{ucp._bulk_js("akeys")}
<p class="muted">Only revoked keys can be deleted. Revoke a key first; it stops working immediately.</p>
<h3>Issue new key</h3>
<form method="post" action="/api-keys">
{_csrf_field(s)}
<label>Login<br><select name="username">{opts}</select></label><br>
<label>Name<br><input name="name" placeholder="e.g. provisioning"></label><br><br>
<button class="btn" type="submit">Create key</button>
</form>"""
    return page("API Keys", body, s["username"], s["role"], "keys")


@app.post("/api-keys")
async def apikeys_create(request: Request, username: str = Form(...), name: str = Form("")):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "keys")
    await _check_csrf(request, s)
    try:
        token, info = _create_api_key(username, name)
    except HTTPException as e:
        return HTMLResponse(page("API Keys",
                                 f"<p style='color:red'>{e.detail}</p><p><a href='/api-keys'>Back</a></p>",
                                 s["username"], s["role"], "keys"),
                            status_code=e.status_code)
    body = f"""<h2>API key created</h2>
<p>Copy it now — it will not be shown again.</p>
<pre class="token">{token}</pre>
<p>Login: <strong>{esc(info['username'])}</strong> &middot; Name: {esc(info['name'] or '-')}<br>
Use as: <code>Authorization: Bearer {token}</code></p>
<p><a href="/api-keys" class="btn">Done</a></p>"""
    return page("API Keys", body, s["username"], s["role"], "keys")


@app.post("/api-keys/delete")
async def apikeys_delete(request: Request):
    s, form, everything, ids = await _admin_bulk_form(request, "keys")
    if s is None:
        return form
    with db() as c:
        if everything:
            n = c.execute("DELETE FROM api_keys WHERE enabled=0").rowcount
        else:
            n = sum(c.execute("DELETE FROM api_keys WHERE id=? AND enabled=0", (i,)).rowcount for i in ids)
        c.commit()
    _audit_log(s["username"], "api_keys.delete", "all revoked" if everything else f"{n} rows")
    return RedirectResponse(f"/api-keys?deleted={n}", status_code=303)


@app.post("/api-keys/{key_id}/revoke")
async def apikeys_revoke(request: Request, key_id: int):
    s = _sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    if _get_setting("safety_lock") == "1":
        return _panel_locked(s, "keys")
    await _check_csrf(request, s)
    with db() as c:
        c.execute("UPDATE api_keys SET enabled = 0 WHERE id = ?", (key_id,))
        c.commit()
    return RedirectResponse("/api-keys", status_code=302)


# ---------------------------------------------------------------- v1: live status
# The dashboard is a thin consumer of these JSON endpoints (it refreshes them
# with JS). No screen-scraping as the core: the brain is the
# source of truth for calls, Asterisk for registrations.

import json
import time
import urllib.request

STATUS_SCRIPT = "/opt/pbx/bin/pbx-status.sh"
BRAIN_STATUS_URL = os.environ.get("PBX_BRAIN_STATUS", "http://127.0.0.1:8099")
_status_cache = {"at": 0.0, "data": None}


def _asterisk_status():
    """Registered contacts via the root-owned status wrapper. 5s cache."""
    now = time.time()
    if _status_cache["data"] is not None and now - _status_cache["at"] < 5:
        return _status_cache["data"]
    r = subprocess.run(["sudo", "-n", STATUS_SCRIPT],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError("pbx-status failed: " +
                           (r.stderr.strip() or f"exit {r.returncode}"))
    data = json.loads(r.stdout)
    _status_cache.update(at=now, data=data)
    return data


def _brain_calls():
    """Live calls from the brain's localhost status server."""
    try:
        with urllib.request.urlopen(BRAIN_STATUS_URL + "/calls", timeout=5) as r:
            return json.load(r).get("calls", [])
    except Exception as e:
        raise HTTPException(503, f"brain status unavailable: {e}")


def _finite(v):
    """None for NaN/inf/non-numbers: those break JSON encoding."""
    import math
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@app.get("/api/v1/devices")
def v1_devices(request: Request):
    """Currently registered SIP devices. Deduped on (username, ip, port): a
    phone re-registering on network change leaves its old contact alive until
    expiry — one row per real device, not per stale contact."""
    actor = _v1_actor(request)
    try:
        status = _asterisk_status()
    except Exception as e:
        raise HTTPException(503, f"asterisk status unavailable: {e}")
    with db() as c:
        logins = {r["sip_username"]: dict(r) for r in c.execute(
            "SELECT id, username, exten, sip_username, display_name "
            "FROM logins WHERE enabled = 1")}
    seen = set()
    devices = []
    for ct in status.get("contacts", []):
        login = logins.get(ct["aor"])
        if not login:
            continue
        if actor["role"] != "admin" and login["id"] != actor["id"]:
            continue
        key = (login["username"], ct["ip"], ct["port"])
        if key in seen:
            continue
        seen.add(key)
        where, via = _device_location(ct["ip"], ct["transport"])
        devices.append({
            "username": login["username"],
            "exten": login["exten"],
            "display_name": login["display_name"],
            "ip": ct["ip"], "port": ct["port"],
            "transport": ct["transport"],
            "location": where, "via": via,
            "status": ct["status"], "rtt_ms": _finite(ct.get("rtt_ms")),
        })
    devices.sort(key=lambda d: (d["location"] != "external", d["exten"] or "", d["ip"]))
    return {"devices": devices,
            "counts": {"internal": sum(d["location"] == "internal" for d in devices),
                       "external": sum(d["location"] == "external" for d in devices)}}


def _device_location(ip: str, transport: str):
    """(internal|external, how it connects). Phones on the internet reach
    Asterisk through the Kamailio edge over TLS; Kamailio rewrites their
    Contact to their real public IP. LAN phones talk UDP straight to
    Asterisk. A home phone using the public domain comes back through the
    router (NAT loopback) and shows the router's LAN address over TLS."""
    via_edge = (transport or "").lower() in ("tls", "wss")
    try:
        _ip = _ipaddress.ip_address(ip)
        # Documentation-only ranges (RFC 5737) never occur on a real LAN.
        doc = any(_ip in _ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
        private = _ip.is_private and not doc
    except ValueError:
        private = False
    if private:
        return "internal", ("home network via the public domain" if via_edge else "office network, direct")
    return "external", ("internet via edge (TLS + SRTP)" if via_edge else "internet, direct")


@app.get("/api/v1/calls/live")
def v1_calls_live(request: Request):
    """Live calls, from the brain (source of truth via ARI)."""
    actor = _v1_actor(request)
    calls = _brain_calls()
    if actor["role"] != "admin":
        exten = actor.get("exten") or ""
        calls = [c for c in calls
                 if c.get("caller") == exten or c.get("callee") == exten]
    with db() as c:
        rows = c.execute("SELECT id, exten, display_name, username FROM logins WHERE exten != ''").fetchall()
    by_ext = {r["exten"]: r for r in rows}
    by_id = {r["id"]: r for r in rows}
    for call in calls:
        if call.get("direction") == "spy":
            # *555 monitor session: label who is listening to what.
            call["from_label"], call["from_kind"] = _party_label(call.get("caller", ""), by_ext)
            target = call.get("callee") or ""
            if target:
                tlabel, _ = _party_label(target, by_ext)
                to_label = "👁 monitoring " + tlabel
            else:
                to_label = "👁 scanning all calls"
            if call.get("spy_label"):
                to_label += " — hearing " + call["spy_label"]
            call["to_label"], call["to_kind"] = to_label, "spy"
            continue
        call["from_label"], call["from_kind"] = _party_label(call.get("caller", ""), by_ext)
        to, kind = _party_label(call.get("callee", ""), by_ext)
        if call.get("direction") == "outbound" and call.get("trunk"):
            to += " via " + str(call["trunk"])
        if call.get("did") and kind != "internal":
            to += " (called " + str(call["did"]) + ")"
        ans = by_id.get(call.get("answered_id"))
        if ans and ans["exten"] != call.get("callee"):
            to += " → answered by " + (ans["display_name"] or ans["username"]) + " (" + ans["exten"] + ")"
        if call.get("forwarded_from"):
            fw = by_ext.get(call["forwarded_from"])
            to += " (forwarded from " + ((fw["display_name"] or fw["username"]) if fw else call["forwarded_from"]) + ")"
        call["to_label"], call["to_kind"] = to, kind
    return {"calls": calls}


def _party_label(v: str, by_ext: dict):
    """Readable name for a call party + kind (internal/outside/voicemail/...)."""
    v = str(v or "")
    r = by_ext.get(v)
    if r:
        return f"{r['display_name'] or r['username']} ({v})", "internal"
    if "," in v:
        names = [_party_label(x, by_ext)[0] for x in v.split(",") if x]
        return "Ringing " + ", ".join(names), "internal"
    if v.startswith("voicemail-"):
        r = by_ext.get(v[10:])
        return "Voicemail of " + ((r["display_name"] or r["username"]) if r else v[10:]), "voicemail"
    if v.startswith("conf-"):
        return "Conference room " + v[5:], "conference"
    if v.startswith("ivr-"):
        return "IVR menu " + v[4:], "ivr"
    if v.startswith("ringgroup-") or v.startswith("group-"):
        return "Ring group " + v.split("-", 1)[1], "group"
    if v in ("911", "933"):
        return v + " (EMERGENCY)", "emergency"
    if v.lstrip("+").isdigit():
        return v + " (outside)", "outside"
    return v or "-", "other"


@app.get("/api/v1/signins")
def v1_signins(request: Request, limit: int = 50, failed_only: int = 0):
    """Panel sign-in history (admin). Only the last 24 hours are kept."""
    _v1_admin(request)
    _purge_signins()
    limit = max(1, min(limit, 200))
    q = "SELECT id, login, at, ip, ok FROM signins"
    if failed_only:
        q += " WHERE ok = 0"
    q += " ORDER BY id DESC LIMIT ?"
    with db() as c:
        rows = c.execute(q, (limit,)).fetchall()
        ok24, fail24 = c.execute("SELECT COALESCE(SUM(ok=1),0), COALESCE(SUM(ok=0),0) FROM signins").fetchone()
    return {"signins": [dict(r) for r in rows],
            "counts": {"ok_24h": ok24, "failed_24h": fail24, "total": ok24 + fail24},
            "retention_hours": 24}


# ---------------------------------------------------------------- log housekeeping
# Sign-in log: rolling 24 hours (older rows are deleted automatically).
# Admins can also delete selected rows or everything, for sign-ins and CDRs.

SIGNIN_RETENTION = "-1 day"
CDR_RETENTION = "-90 days"     # call history (started_at is server local time)


def _purge_signins():
    try:
        with db() as c:
            c.execute("DELETE FROM signins WHERE at < datetime('now', ?)", (SIGNIN_RETENTION,))
            c.commit()
    except Exception:
        pass


def _purge_cdr():
    """Call history older than 90 days is removed (users can't delete it;
    recordings and voicemail are separate and kept)."""
    try:
        with db() as c:
            c.execute("DELETE FROM cdr WHERE started_at < datetime('now', 'localtime', ?)", (CDR_RETENTION,))
            c.commit()
    except Exception:
        pass


def _housekeeping_loop():
    import time as _t
    while True:
        _purge_signins()
        _purge_cdr()
        security.purge()
        _t.sleep(600)


@app.on_event("startup")
def _start_housekeeping():
    import threading
    _purge_signins()
    _purge_cdr()
    threading.Thread(target=_housekeeping_loop, daemon=True, name="housekeeping").start()


class DeleteIn(BaseModel):
    ids: list[int] = []
    all: bool = False


def _delete_rows(table: str, ids, everything: bool) -> int:
    assert table in ("signins", "cdr")
    with db() as c:
        if everything:
            n = c.execute(f"DELETE FROM {table}").rowcount
        else:
            ids = [int(i) for i in ids][:5000]
            if not ids:
                return 0
            n = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                n += c.execute(f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(chunk))})", chunk).rowcount
        c.commit()
    return n


def _audit_log(actor, action, detail=""):
    try:
        with db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)", (actor, action, detail))
            c.commit()
    except Exception:
        pass


@app.post("/api/v1/signins/delete")
def v1_signins_delete(request: Request, body: DeleteIn):
    """Delete selected sign-in rows ({"ids": [...]}) or all ({"all": true})."""
    actor = _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    n = _delete_rows("signins", body.ids, body.all)
    _audit_log(actor["username"], "signins.delete", "all" if body.all else f"{n} rows")
    return {"deleted": n}


@app.post("/api/v1/cdr/delete")
def v1_cdr_delete(request: Request, body: DeleteIn):
    """Delete selected CDRs ({"ids": [...]}) or all ({"all": true})."""
    actor = _v1_admin(request)
    _check_csrf_v1(request)
    _safety_allows_mutation()
    n = _delete_rows("cdr", body.ids, body.all)
    _audit_log(actor["username"], "cdr.delete", "all" if body.all else f"{n} rows")
    return {"deleted": n}


def _log_signin(request: Request, username: str, ok: bool):
    """Audit a panel sign-in attempt. Never breaks login on failure."""
    ip = client_ip(request)
    try:
        with db() as c:
            c.execute("INSERT INTO signins (login, ip, ok) VALUES (?,?,?)",
                      (username, ip, 1 if ok else 0))
            c.commit()
    except Exception:
        pass


# ---------------------------------------------------------------- v1: safety
# Kill switch: emergency stop. Engaging hangs up all active calls, renders
# an empty pjsip.conf (no registrations, no trunk) and reloads. The brain
# also hangs up any new channel while the switch is on (belt and braces).
# Safety lock: when ON, routine mutations (API + panel) are rejected with
# 403. The kill switch and the lock toggle itself always stay available.

class SafetyIn(BaseModel):
    engaged: bool = True


class LockIn(BaseModel):
    locked: bool = True


def _get_setting(key: str) -> str:
    with db() as c:
        row = c.execute("SELECT value FROM kv_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else "0"


def _load_branding():
    """Load brand_* settings into the BRANDING contextvar for this request."""
    try:
        with db() as c:
            rows = c.execute("SELECT key, value FROM kv_settings WHERE key LIKE 'brand_%'").fetchall()
        BRANDING.set({r["key"][6:]: r["value"] for r in rows})
    except Exception:
        BRANDING.set({})


def _set_setting(key: str, value: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?, ?)",
                  (key, value))
        c.commit()


def actor_name(request: Request) -> str:
    try:
        return _v1_actor(request).get("username", "")
    except Exception:
        return ""


def _safety_allows_mutation():
    if _get_setting("safety_lock") == "1":
        raise HTTPException(403, "safety lock is engaged — disengage it to make changes")


def _brain_hangup_all() -> int:
    """Best-effort hangup of all active calls via the brain. Returns count."""
    try:
        req = urllib.request.Request(BRAIN_STATUS_URL + "/calls/hangup-all",
                                     data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("hung_up", 0)
    except Exception as e:
        print(f"WARNING: hangup-all failed: {e}", flush=True)
        return 0


@app.get("/api/v1/safety")
def v1_safety(request: Request):
    _v1_admin(request)
    return {"kill_switch": _get_setting("kill_switch") == "1",
            "safety_lock": _get_setting("safety_lock") == "1"}


@app.post("/api/v1/safety/kill-switch")
def v1_kill_switch(request: Request, body: SafetyIn):
    _v1_admin(request)
    _check_csrf_v1(request)
    _set_setting("kill_switch", "1" if body.engaged else "0")
    hung = _brain_hangup_all() if body.engaged else 0
    security.record("kill_switch", client_ip(request),
                    ("ENGAGED: all calls hung up (%d), phones unregistered" % hung) if body.engaged
                    else "released: phones can register again", actor=actor_name(request))
    applied, err = _apply_guarded()
    resp = {"kill_switch": body.engaged, "applied": applied,
            "calls_hung_up": hung}
    if err:
        resp["apply_error"] = err
    return resp


@app.post("/api/v1/safety/lock")
def v1_safety_lock(request: Request, body: LockIn):
    _v1_admin(request)
    _check_csrf_v1(request)
    _set_setting("safety_lock", "1" if body.locked else "0")
    security.record("safety_lock", client_ip(request),
                    "ON: changes are blocked" if body.locked else "off: changes allowed",
                    actor=actor_name(request))
    return {"safety_lock": body.locked}


# ---------------------------------------------------------------- edit PIN
# Optional 6-12 digit PIN that gates every admin-panel mutation (save/edit,
# checkbox toggles, invite generation, kill switch, ...). Reads stay open.
# Unlocking lasts PIN_GRACE_SECS per session (sliding). Bearer-token API
# calls are exempt — the PIN is a panel protection, not an API credential.
PIN_GRACE_SECS = 15 * 60
_PIN_FAILS: dict[str, list[float]] = {}


def _pin_is_set() -> bool:
    h = _get_setting("safety_pin_hash")
    return bool(h) and h != "0"


def _pin_unlocked(s: dict | None) -> bool:
    if not _pin_is_set():
        return True
    import time as _t
    try:
        return bool(s) and (_t.time() - float(s.get("pin_unlocked_at") or 0) < PIN_GRACE_SECS)
    except (TypeError, ValueError):
        return False


def _pin_touch(s: dict) -> None:
    import time as _t
    s["pin_unlocked_at"] = _t.time()


def _pin_attempt_ok(ip: str) -> bool:
    import time as _t
    now = _t.time()
    hits = [t for t in _PIN_FAILS.get(ip, []) if now - t < 900]
    _PIN_FAILS[ip] = hits
    return len(hits) < 10


def _pin_attempt_fail(ip: str, actor: str) -> None:
    import time as _t
    _PIN_FAILS.setdefault(ip, []).append(_t.time())
    if len(_PIN_FAILS[ip]) == 10:
        security.record("pin_lockout", ip,
                        "10 wrong edit-PIN attempts in 15 min; this address is blocked for 15 min",
                        actor=actor)


class PinUnlockIn(BaseModel):
    pin: str = ""


class PinSetIn(BaseModel):
    pin: str = ""      # "" removes the PIN
    old_pin: str = ""


def _pin_valid_format(pin: str) -> bool:
    return pin.isdigit() and 6 <= len(pin) <= 12


@app.get("/api/v1/safety/pin/status")
def v1_pin_status(request: Request):
    _v1_admin(request)
    s = _sess(request)
    return {"pin_set": _pin_is_set(), "unlocked": _pin_unlocked(s)}


@app.post("/api/v1/safety/pin/unlock")
async def v1_pin_unlock(request: Request, body: PinUnlockIn):
    s = _sess(request)
    if not s or s["role"] != "admin":
        raise HTTPException(401, "login required")
    _check_csrf_v1(request)
    ip = client_ip(request)
    if not _pin_is_set():
        return {"unlocked": True}
    if not _pin_attempt_ok(ip):
        raise HTTPException(429, "Too many wrong PIN attempts — try again later")
    ok = bcrypt.checkpw((body.pin or "").encode(),
                        _get_setting("safety_pin_hash").encode())
    if not ok:
        _pin_attempt_fail(ip, s["username"])
        raise HTTPException(403, "Wrong PIN")
    _pin_touch(s)
    security.record("pin_unlock", ip, "edit PIN accepted; panel unlocked for 15 min",
                    actor=s["username"])
    return {"unlocked": True}


@app.post("/api/v1/safety/pin/lock")
async def v1_pin_lock(request: Request):
    """Relock immediately (the Unlock button's counterpart)."""
    s = _sess(request)
    if not s or s["role"] != "admin":
        raise HTTPException(401, "login required")
    _check_csrf_v1(request)
    s["pin_unlocked_at"] = 0
    return {"unlocked": False}


@app.post("/api/v1/safety/pin/set")
async def v1_pin_set(request: Request, body: PinSetIn):
    s = _sess(request)
    if not s or s["role"] != "admin":
        raise HTTPException(401, "login required")
    _check_csrf_v1(request)
    pin = (body.pin or "").strip()
    if pin and not _pin_valid_format(pin):
        raise HTTPException(400, "PIN must be 6-12 digits")
    if _pin_is_set():
        if not body.old_pin or not bcrypt.checkpw(
                body.old_pin.encode(), _get_setting("safety_pin_hash").encode()):
            raise HTTPException(403, "Current PIN is wrong")
    if pin:
        _set_setting("safety_pin_hash", bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode())
    else:
        _set_setting("safety_pin_hash", "")
    s["pin_unlocked_at"] = 0  # changing the PIN relocks immediately
    security.record("pin_change", client_ip(request),
                    "edit PIN set" if pin else "edit PIN removed", actor=s["username"])
    return {"pin_set": bool(pin)}


_PIN_EXEMPT_EXACT = frozenset({
    "/api/v1/safety/pin/unlock", "/api/v1/safety/pin/set",
    "/api/v1/safety/pin/status", "/api/v1/safety/pin/lock",
    "/login", "/logout", "/auth/login", "/auth/logout",
})
_PIN_EXEMPT_PREFIX = ("/ucp", "/hooks/", "/invite/", "/stripe/webhook")


def _pin_required_html(s: dict) -> str:
    import json as _json
    tok = _json.dumps(_csrf_token(s))
    return f"""<h2>PIN required</h2>
<p class='warn'>The edit PIN is on — unlock the panel to make changes.</p>
<form id="pinform" onsubmit="return pinUnlockSubmit(event)">
<input type="password" id="pinentry" inputmode="numeric" pattern="[0-9]*" maxlength="12"
 placeholder="6-12 digit PIN" autocomplete="off" style="font-size:18px;letter-spacing:4px">
<button class="btn" type="submit">Unlock</button></form>
<p class="muted">After unlocking you have 15 minutes. Then go back and retry your change.</p>
<p><a class="btn ghost" href="javascript:history.back()">Back</a></p>
<script>
var PIN_CSRF = {tok};
async function pinUnlockSubmit(e) {{
  e.preventDefault();
  var p = document.getElementById('pinentry').value;
  var r = await fetch('/api/v1/safety/pin/unlock', {{method: 'POST', credentials: 'same-origin',
    headers: {{'Content-Type': 'application/json', 'X-CSRF-Token': PIN_CSRF}},
    body: JSON.stringify({{pin: p}})}});
  if (r.ok) {{
    document.getElementById('pinform').innerHTML = '<p class="ok">Unlocked — go back and retry your change.</p>';
  }} else {{
    alert(r.status === 429 ? 'Too many attempts — try again later.' : 'Wrong PIN.');
  }}
  return false;
}}
</script>"""


@app.middleware("http")
async def _pin_guard(request: Request, call_next):
    """When the edit PIN is set, block admin-session mutations until unlocked.

    Bearer-token API calls are exempt (separate credential). Reads are free.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        path = request.url.path
        bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        if (not bearer and path not in _PIN_EXEMPT_EXACT
                and not path.startswith(_PIN_EXEMPT_PREFIX) and _pin_is_set()):
            s = _sess(request)
            if s and s.get("role") == "admin":
                if not _pin_unlocked(s):
                    if path.startswith("/api/"):
                        return JSONResponse(
                            {"detail": "Edit PIN required — unlock to make changes.",
                             "pin_required": True}, status_code=403)
                    return HTMLResponse(
                        page("PIN required", _pin_required_html(s),
                             s["username"], s["role"], ""), status_code=403)
                _pin_touch(s)  # sliding 15-minute grace
    return await call_next(request)


def _panel_locked(s, tab):
    """403 page for panel mutations attempted while the safety lock is on."""
    return HTMLResponse(
        page("Safety lock",
             "<p style='color:red'>Safety lock is engaged — disengage it on the "
             "dashboard to make changes.</p>"
             "<p><a href='/' class='btn'>Back to dashboard</a></p>",
             s["username"], s["role"], tab),
        status_code=403)


# ---------------------------------------------------------------- user control panel
import sys as _sys
ucp.install(_sys.modules[__name__])
ivr_ui.install(_sys.modules[__name__])
billing.install(_sys.modules[__name__])
email_ui.install(_sys.modules[__name__])
network_ui.install(_sys.modules[__name__])
security.install(_sys.modules[__name__])
e911_ui.install(_sys.modules[__name__])
ring_groups_ui.install(_sys.modules[__name__])
safety_ui.install(_sys.modules[__name__])
