# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin E911 page (/e911): who gets E911, with which number and address.

Locked by an E911 code (6-12 digits, bcrypt-hashed in kv 'e911_code_hash').
Viewing is always allowed for admins; any change needs the page unlocked
with the code first (unlock lasts 10 minutes for that admin session). Wrong
codes are limited (5 per 15 minutes) and alerted. Every change is audited
and alerted.

What the settings mean for calls (pbx-brain/e911.py):
  * A user with E911 on: 911 calls use their E911 number, whose address is
    registered with the trunk provider (the admin registers it there).
  * Everyone else: the office fallback E911 number/address.
  * 911 is never blocked (US 911 rules).

Lost code: on the PBX run
  sqlite3 /var/lib/pbx/pbx.db "DELETE FROM kv_settings WHERE key='e911_code_hash'"
then set a new one on this page.
"""
import re
import time
import urllib.parse

import bcrypt
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import security

M = None
UNLOCK_SECONDS = 600
CODE_RE = re.compile(r"^\d{6,12}$")
DID_RE = re.compile(r"^\+?1?\d{10}$")
_fails: dict[str, list[float]] = {}


def esc(x):
    return M.esc(x)


def _kv(key, default=""):
    with M.db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r and r[0] is not None else default


def _set(key, value):
    with M.db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (key, str(value)))
        c.commit()


def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _back(msg="", err=""):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/e911" + ("?" + q if q else ""), status_code=303)


def _unlocked(s):
    return s.get("e911_until", 0) > time.time()


def _audit(actor, action, detail=""):
    try:
        with M.db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)", (actor, action, detail))
            c.commit()
    except Exception:
        pass


def _norm_did(v):
    v = re.sub(r"[\s().-]", "", v or "")
    if not v:
        return ""
    if not DID_RE.match(v):
        raise ValueError("A 911 number must be a 10-digit phone number (e.g. 3105551234).")
    digits = v.lstrip("+")
    return digits[-10:] if len(digits) == 11 and digits.startswith("1") else digits


def _clean_addr(v):
    v = "\n".join(line.strip() for line in (v or "").replace("\r", "").split("\n") if line.strip())
    return "".join(ch for ch in v if ch == "\n" or ch.isprintable())[:300]


# ---------------------------------------------------------------- page

def page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    csrf = M._csrf_field(s)
    flash = ""
    if request.query_params.get("msg"):
        flash += f'<div class="flash ok">{esc(request.query_params["msg"])}</div>'
    if request.query_params.get("err"):
        flash += f'<div class="flash bad">{esc(request.query_params["err"])}</div>'
    has_code = bool(_kv("e911_code_hash"))
    unlocked = has_code and _unlocked(s)
    dis = "" if unlocked else "disabled"
    with M.db() as c:
        users = c.execute(
            "SELECT l.id, l.username, l.exten, l.display_name, e.enabled, e.did, e.address, e.updated_at, e.updated_by"
            " FROM logins l LEFT JOIN e911_users e ON e.login_id=l.id"
            " WHERE l.exten != '' ORDER BY l.exten").fetchall()
        trunks = c.execute("SELECT id, name FROM trunks WHERE enabled=1 ORDER BY id").fetchall()
    fb_did, fb_addr = _kv("e911_fallback_did"), _kv("e911_fallback_address")
    trunk_id = _kv("e911_trunk_id")
    nine = _kv("e911_allow_9prefix", "1") == "1"
    provider = _kv("e911_use_provider", "1") == "1"

    if not has_code:
        lock_html = f"""<section class="panel"><h3>Set the E911 code <span class="pill warn">Not set</span></h3>
<p class="muted">Choose a 6-12 digit code. It's needed every time someone changes E911 settings, so E911 can't be
switched on (and billed) by accident or by someone who only has an admin login. Keep it somewhere safe.</p>
<form method="post" action="/e911/setcode">{csrf}
<label>New code<br><input name="code" type="password" inputmode="numeric" autocomplete="new-password" pattern="[0-9]{{6,12}}" required></label>
<label>Repeat<br><input name="code2" type="password" inputmode="numeric" autocomplete="new-password" pattern="[0-9]{{6,12}}" required></label>
<button class="btn">Set code</button></form></section>"""
    elif unlocked:
        left = int(s["e911_until"] - time.time()) // 60 + 1
        lock_html = f"""<section class="panel"><h3>E911 <span class="pill ok">Unlocked</span></h3>
<p class="muted">Changes allowed for about {left} more minute{"s" if left != 1 else ""}, then it locks again on its own.</p>
<form method="post" action="/e911/lock" class="inline">{csrf}<button class="btn ghost">Lock now</button></form>
<details style="margin-top:10px"><summary>Change the E911 code</summary>
<form method="post" action="/e911/changecode">{csrf}
<label>Current code<br><input name="current" type="password" inputmode="numeric" autocomplete="off" required></label>
<label>New code (6-12 digits)<br><input name="code" type="password" inputmode="numeric" autocomplete="new-password" pattern="[0-9]{{6,12}}" required></label>
<label>Repeat<br><input name="code2" type="password" inputmode="numeric" autocomplete="new-password" pattern="[0-9]{{6,12}}" required></label>
<button class="btn ghost">Change code</button></form></details></section>"""
    else:
        lock_html = f"""<section class="panel"><h3>E911 <span class="pill bad">Locked</span></h3>
<p class="muted">Enter the E911 code to make changes.</p>
<form method="post" action="/e911/unlock">{csrf}
<label>E911 code<br><input name="code" type="password" inputmode="numeric" autocomplete="off" required autofocus></label>
<button class="btn">Unlock</button></form></section>"""

    topts = '<option value="">First enabled trunk</option>' + "".join(
        f'<option value="{t["id"]}" {"selected" if str(t["id"]) == trunk_id else ""}>{esc(t["name"])}</option>' for t in trunks)
    no_trunk = '' if trunks else '<div class="flash bad">No trunk is enabled: 911 calls can\'t connect. Add a trunk now.</div>'
    if fb_did:
        no_fb = ''
    elif provider:
        no_fb = ('<div class="flash ok">Users without their own E911 use the E911 address set up at your phone provider '
                 '(the trunk\'s default caller ID). Check it with a 933 test call.</div>')
    else:
        no_fb = ('<div class="flash bad">No office fallback set: 911 calls from users without E911 go out with the trunk\'s '
                 'default caller ID. Turn on "use my provider\'s E911" below if that number has an E911 address at the provider.</div>')
    settings_html = f"""<section class="panel"><h3>Office fallback &amp; routing</h3>
{no_trunk}{no_fb}
<p class="muted">Used for every user who doesn't have their own E911. The provider, not this panel, tells 911 where
to go: the address is the one registered for the caller ID number at your provider (e.g. VoIP.ms → DID → e911).</p>
<form method="post" action="/e911/settings">{csrf}
<label class="switch"><input type="checkbox" name="use_provider" value="1" {"checked" if provider else ""} {dis}> <span>Use my provider's E911 (the trunk's default caller ID already has an E911 address)</span></label>
<label>Office 911 caller ID number <span class="muted">(optional - only to send a different number than the trunk's default)</span><br><input name="fallback_did" value="{esc(fb_did)}" placeholder="leave empty to use the trunk's default" {dis}></label>
<label>Address to show users <span class="muted">(optional - the address on file at the provider, shown in My Phone)</span><br><textarea name="fallback_address" rows="3" placeholder="123 Main St, Suite 4&#10;Los Angeles, CA 90001" {dis}>{esc(fb_addr)}</textarea></label>
<label>Send 911 calls through<br><select name="trunk_id" {dis}>{topts}</select></label>
<label class="switch"><input type="checkbox" name="allow_9prefix" value="1" {"checked" if nine else ""} {dis}> <span>Also treat 9-911 as 911 (for people used to dialing 9 for an outside line)</span></label>
<button class="btn" {dis}>Save</button></form></section>"""

    rows = []
    for u in users:
        on = bool(u["enabled"])
        rows.append(f"""<tr><td>{esc(u['display_name'] or u['username'])} <span class="muted">{esc(u['exten'])}</span></td>
<td><form id="e{u['id']}" method="post" action="/e911/user/{u['id']}">{csrf}</form>
<label class="switch"><input form="e{u['id']}" type="checkbox" name="enabled" value="1" {"checked" if on else ""} {dis}> <span>{"On" if on else "Off (office fallback)"}</span></label></td>
<td><input form="e{u['id']}" name="did" value="{esc(u['did'] or '')}" placeholder="10-digit number" size="12" {dis}></td>
<td><textarea form="e{u['id']}" name="address" rows="2" cols="28" placeholder="Street, city, state, ZIP" {dis}>{esc(u['address'] or '')}</textarea></td>
<td class="muted">{esc(u['updated_at'] or '')}<br>{esc(u['updated_by'] or '')}</td>
<td><button class="link-btn" form="e{u['id']}" {dis}>Save</button></td></tr>""")
    table = "".join(rows) or '<tr><td colspan="6" class="muted">No users with extensions</td></tr>'
    body = f"""{flash}<h2>E911 / 911</h2>
<div class="flash bad"><b>Not tested:</b> own-pbx is a home/hobby project and its 911/E911 features have not been
tested end to end. Don't rely on it for emergency calls - keep a mobile phone or landline available, and test
with your provider (e.g. dial 933) before trusting any address.</div>
<div class="flash warn-note"><b>911 always connects.</b> US 911 rules don't allow a phone system to block 911, so every
phone can dial 911 (and 933, the address test line at many providers) - including users without E911, users with
no plan, phones on DND, and phones connected from outside the office. These settings decide which number and address
the call is sent with. Every 911 call is logged on the dashboard and emailed to the security-alert address.</div>
<div class="grid2">{lock_html}{settings_html}</div>
<h3>Users</h3>
<p class="muted">Turn E911 on for a user and give their own 911 number (a DID whose address you registered with the
provider) and that address. Users see the address in My Phone with a warning that 911 sends help there, wherever
the phone actually is.</p>
<div class="scrollbox"><table><tr><th>User</th><th>E911</th><th>911 number</th><th>Address on file</th><th>Last change</th><th></th></tr>
{table}</table></div>"""
    return HTMLResponse(M.page("E911", body, s["username"], s["role"], "e911"))


# ---------------------------------------------------------------- actions

async def _post(request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return s, M._panel_locked(s, "e911")
    return s, None


def _need_unlock(s):
    if not _kv("e911_code_hash"):
        return _back(err="Set the E911 code first.")
    if not _unlocked(s):
        return _back(err="Unlock E911 with the code first.")
    return None


def _check_code(request, s, code):
    """True if `code` matches. Rate-limited per admin + address; alerts."""
    key = f'{s["username"]}|{M.client_ip(request)}'
    now = time.time()
    hits = [t for t in _fails.get(key, []) if now - t < 900]
    _fails[key] = hits
    if len(hits) >= 5:
        return None  # locked out
    h = _kv("e911_code_hash")
    if h and CODE_RE.match(code or "") and bcrypt.checkpw(code.encode(), h.encode()):
        _fails.pop(key, None)
        return True
    hits.append(now)
    security.record("e911_unlock_fail", M.client_ip(request),
                    f"wrong E911 code by {s['username']} ({len(hits)} of 5 in 15 min)", actor=s["username"])
    return False


async def setcode(request: Request):
    s, locked = await _post(request)
    if locked:
        return locked
    if _kv("e911_code_hash"):
        return _back(err="A code is already set. Unlock and use 'Change the E911 code'.")
    f = await request.form()
    code, code2 = (f.get("code") or "").strip(), (f.get("code2") or "").strip()
    if not CODE_RE.match(code):
        return _back(err="The code must be 6 to 12 digits.")
    if code != code2:
        return _back(err="The two codes don't match.")
    if len(set(code)) == 1 or code in "01234567890123" or code in "98765432109876":
        return _back(err="Pick a code that isn't all the same digit or a simple sequence.")
    _set("e911_code_hash", bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode())
    _audit(s["username"], "e911.code_set")
    security.record("e911_change", M.client_ip(request), "E911 code set", actor=s["username"])
    s["e911_until"] = time.time() + UNLOCK_SECONDS
    return _back(msg="E911 code set. E911 is unlocked for 10 minutes.")


async def unlock(request: Request):
    s, locked = await _post(request)
    if locked:
        return locked
    f = await request.form()
    ok = _check_code(request, s, (f.get("code") or "").strip())
    if ok is None:
        return _back(err="Too many wrong codes. Try again in 15 minutes.")
    if not ok:
        return _back(err="Wrong E911 code.")
    s["e911_until"] = time.time() + UNLOCK_SECONDS
    _audit(s["username"], "e911.unlock")
    return _back(msg="E911 unlocked for 10 minutes.")


async def lock(request: Request):
    s, locked = await _post(request)
    s.pop("e911_until", None)
    return _back(msg="E911 locked.")


async def changecode(request: Request):
    s, locked = await _post(request)
    if locked:
        return locked
    need = _need_unlock(s)
    if need:
        return need
    f = await request.form()
    ok = _check_code(request, s, (f.get("current") or "").strip())
    if not ok:
        return _back(err="Too many wrong codes. Try again in 15 minutes." if ok is None else "Current code is wrong.")
    code, code2 = (f.get("code") or "").strip(), (f.get("code2") or "").strip()
    if not CODE_RE.match(code) or code != code2:
        return _back(err="New code must be 6 to 12 digits, typed the same twice.")
    _set("e911_code_hash", bcrypt.hashpw(code.encode(), bcrypt.gensalt()).decode())
    _audit(s["username"], "e911.code_changed")
    security.record("e911_change", M.client_ip(request), "E911 code changed", actor=s["username"])
    return _back(msg="E911 code changed.")


async def save_settings(request: Request):
    s, locked = await _post(request)
    if locked:
        return locked
    need = _need_unlock(s)
    if need:
        return need
    f = await request.form()
    try:
        did = _norm_did(f.get("fallback_did"))
    except ValueError as e:
        return _back(err=str(e))
    addr = _clean_addr(f.get("fallback_address"))
    tid = (f.get("trunk_id") or "").strip()
    if tid and not tid.isdigit():
        return _back(err="Pick a trunk.")
    _set("e911_fallback_did", did)
    _set("e911_fallback_address", addr)
    _set("e911_trunk_id", tid)
    _set("e911_allow_9prefix", "1" if f.get("allow_9prefix") else "0")
    _set("e911_use_provider", "1" if f.get("use_provider") else "0")
    detail = f"provider E911 {'on' if f.get('use_provider') else 'off'}; office fallback {did or '-'} / {addr.replace(chr(10), ', ') or '-'}; trunk {tid or 'first'}"
    _audit(s["username"], "e911.settings", detail)
    security.record("e911_change", M.client_ip(request), detail, actor=s["username"])
    return _back(msg="E911 office settings saved.")


async def save_user(request: Request, login_id: int):
    s, locked = await _post(request)
    if locked:
        return locked
    need = _need_unlock(s)
    if need:
        return need
    f = await request.form()
    with M.db() as c:
        u = c.execute("SELECT id, username, exten FROM logins WHERE id=? AND exten != ''", (login_id,)).fetchone()
    if not u:
        return _back(err="No such user.")
    on = bool(f.get("enabled"))
    try:
        did = _norm_did(f.get("did"))
    except ValueError as e:
        return _back(err=f"{u['username']}: {e}")
    addr = _clean_addr(f.get("address"))
    if on and (not did or not addr):
        return _back(err=f"{u['username']}: E911 needs both a 911 number and the address registered for it.")
    with M.db() as c:
        c.execute("INSERT INTO e911_users (login_id, enabled, did, address, updated_at, updated_by)"
                  " VALUES (?,?,?,?,datetime('now'),?) ON CONFLICT(login_id) DO UPDATE SET"
                  " enabled=excluded.enabled, did=excluded.did, address=excluded.address,"
                  " updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                  (login_id, 1 if on else 0, did, addr, s["username"]))
        c.commit()
    detail = f"{u['username']} (ext {u['exten']}): E911 {'ON' if on else 'off'}" + (f", {did}, {addr.replace(chr(10), ', ')}" if on else "")
    _audit(s["username"], "e911.user", detail)
    security.record("e911_change", M.client_ip(request), detail, actor=s["username"])
    return _back(msg=f"Saved E911 for {u['username']}.")


# ---------------------------------------------------------------- user notice

def user_notice(login_id):
    """HTML notice for My Phone about where 911 sends help for this user."""
    with M.db() as c:
        r = c.execute("SELECT enabled, did, address FROM e911_users WHERE login_id=?", (login_id,)).fetchone()
    untested = ('<br><b>Note:</b> 911 on this home phone system has not been tested. For emergencies, use a '
                'mobile phone or landline if you can.')
    away = ("If you use this phone anywhere else - another building, at home, on mobile data or Wi-Fi away from "
            "that address - 911 still sends help to the address above, <b>not to where you are</b>. Always tell the "
            "911 operator where you actually are.")
    if r and r["enabled"] and r["did"]:
        addr = esc(r["address"]).replace("\n", "<br>")
        return (f'<div class="flash warn-note"><b>E911 is on for this phone.</b> If you dial 911, emergency services '
                f'are sent to the address on file:<br><b>{addr}</b><br>{away}{untested}</div>')
    fb = _kv("e911_fallback_address")
    if fb and (_kv("e911_fallback_did") or _kv("e911_use_provider", "1") == "1"):
        addr = esc(fb).replace("\n", "<br>")
        return (f'<div class="flash warn-note"><b>911:</b> this phone doesn\'t have its own E911 address. 911 calls '
                f'still connect and use the office address on file:<br><b>{addr}</b><br>{away}{untested}</div>')
    if _kv("e911_use_provider", "1") == "1":
        return (f'<div class="flash warn-note"><b>911:</b> 911 calls connect and use the office address registered '
                f'with our phone provider.<br>{away}{untested}</div>')
    return ('<div class="flash warn-note"><b>911:</b> 911 calls connect, but no address is on file for this phone. '
            f'Always tell the 911 operator where you are.{untested}</div>')


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.get("/e911", response_class=HTMLResponse)(page)
    app.post("/e911/setcode")(setcode)
    app.post("/e911/unlock")(unlock)
    app.post("/e911/lock")(lock)
    app.post("/e911/changecode")(changecode)
    app.post("/e911/settings")(save_settings)
    app.post("/e911/user/{login_id}")(save_user)
