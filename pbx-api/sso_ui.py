# SPDX-License-Identifier: GPL-2.0-or-later
"""Single sign-on (OpenID Connect) for the web panel: Authentik, Keycloak,
Authelia and the like.

Admin -> Sign-on sets it up. The login page then shows "Sign in with ..."
above the password form, which stays (unless hidden; /login?password=1
always brings it back, so a provider that is down never locks anyone out).

Accounts are never created here: a PBX login needs an extension, so the
admin creates it. The first SSO sign-in links an existing login, matched by
its username or its notification email; after that the provider's subject
id is stored on the login (logins.sso_subject) and is all that counts.

Flow: authorization code with PKCE, state bound to a cookie, nonce. The ID
token comes straight from the token endpoint over verified TLS, so (per
OIDC Core 3.1.3.7) its signature isn't needed; its issuer, audience,
expiry and nonce are checked. The My Phone app is not affected: it pairs
with a code from a signed-in web session.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

M = None
CALLBACK = "/login/oidc/callback"
STATE_TTL = 600
_pending = {}          # state -> {nonce, verifier, redirect_uri, at}
_disco = {}            # issuer -> (fetched_at, document)
STATE_COOKIE = "pbx_sso_state"


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


def cfg():
    return {
        "enabled": _kv("sso_enabled") == "1",
        "issuer": _kv("sso_issuer").strip(),
        "client_id": _kv("sso_client_id").strip(),
        "client_secret": _kv("sso_client_secret"),
        "button": _kv("sso_button").strip(),
        "scopes": _kv("sso_scopes").strip(),
        "match": _kv("sso_match", "username") or "username",
        "verified_only": _kv("sso_verified_only", "1") != "0",
        "admins": _kv("sso_admins", "1") != "0",
        "auto": _kv("sso_auto_redirect") == "1",
        "hide_password": _kv("sso_hide_password") == "1",
        "base_url": _kv("sso_base_url").strip().rstrip("/"),
    }


def ready(c=None):
    c = c or cfg()
    return c["enabled"] and c["issuer"] and c["client_id"] and c["client_secret"]


def button_label(c=None):
    c = c or cfg()
    return "Sign in with " + (c["button"] or "single sign-on")


def redirect_uri(request: Request, c=None):
    c = c or cfg()
    if c["base_url"]:
        return c["base_url"] + CALLBACK
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    return ("https" if M.is_https(request) else "http") + "://" + host + CALLBACK


# ------------------------------------------------------------------ HTTP

def _http(url, data=None, headers=None, timeout=10):
    """GET (or form POST) returning parsed JSON. Raises ValueError with a
    readable reason."""
    h = {"Accept": "application/json", "User-Agent": "own-pbx"}
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
            detail = detail.get("error_description") or detail.get("error") or ""
        except Exception:
            detail = ""
        raise ValueError(f"{urllib.parse.urlsplit(url).netloc} answered HTTP {e.code}" + (f": {detail}" if detail else ""))
    except urllib.error.URLError as e:
        raise ValueError(f"can't reach {urllib.parse.urlsplit(url).netloc} ({e.reason})")
    except (ValueError, json.JSONDecodeError):
        raise ValueError(f"{urllib.parse.urlsplit(url).netloc} didn't answer with JSON")


def _same_issuer(a, b):
    return (a or "").rstrip("/") == (b or "").rstrip("/")


def discovery(issuer, fresh=False):
    hit = _disco.get(issuer)
    if hit and not fresh and time.time() - hit[0] < 600:
        return hit[1]
    if not issuer.lower().startswith("https://") and not issuer.lower().startswith("http://localhost"):
        raise ValueError("the issuer must be an https:// address")
    d = _http(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if not _same_issuer(d.get("issuer"), issuer):
        raise ValueError(f"the provider calls itself {d.get('issuer')!r}, not {issuer!r}: copy its issuer exactly")
    for k in ("authorization_endpoint", "token_endpoint"):
        if not d.get(k):
            raise ValueError(f"the provider's discovery document has no {k}")
    _disco[issuer] = (time.time(), d)
    return d


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _jwt_claims(tok):
    try:
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        raise ValueError("the provider sent an ID token that can't be read")


# ------------------------------------------------------------------ login flow

def _fail(request, msg, who=""):
    """Back to the login page (with the password form) and a reason."""
    if who:
        M._log_signin(request, f"{who[:40]} (single sign-on)", False)
    M._login_failed(M.client_ip(request), "sso:" + (who or "?"))
    resp = HTMLResponse(M.page("Login", login_box(request, err=msg, force_form=True)), status_code=401)
    resp.delete_cookie(STATE_COOKIE, path=CALLBACK)
    return resp


def start(request: Request):
    """GET /login/oidc: off to the provider."""
    c = cfg()
    if not ready(c):
        return RedirectResponse("/login?password=1", status_code=302)
    if not M._login_allowed(M.client_ip(request), ""):
        return _fail(request, "Too many attempts — try again in 15 minutes.")
    try:
        d = discovery(c["issuer"])
    except ValueError as e:
        return _fail(request, f"Single sign-on isn't working right now: {e}.")
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["at"] > STATE_TTL]:
        _pending.pop(k, None)
    if len(_pending) > 2000:
        for k in sorted(_pending, key=lambda k: _pending[k]["at"])[:500]:
            _pending.pop(k, None)
    ruri = redirect_uri(request, c)
    _pending[state] = {"nonce": nonce, "verifier": verifier, "redirect_uri": ruri, "at": now,
                       "next": next_param(request.query_params.get("next"))}
    scopes = c["scopes"].split() or ["email", "profile"]
    q = {
        "response_type": "code", "client_id": c["client_id"], "redirect_uri": ruri,
        "scope": " ".join(["openid"] + [s for s in scopes if s != "openid"]),
        "state": state, "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    ep = d["authorization_endpoint"]
    resp = RedirectResponse(ep + ("&" if "?" in ep else "?") + urllib.parse.urlencode(q), status_code=302)
    resp.set_cookie(STATE_COOKIE, state, max_age=STATE_TTL, httponly=True, samesite="lax",
                    secure=M.is_https(request), path=CALLBACK)
    return resp


def _find_login(c, claims):
    """(login row, reason). Linked subject first, then the first-time match."""
    subject = c["issuer"].rstrip("/") + "|" + str(claims.get("sub") or "")
    with M.db() as db:
        u = db.execute("SELECT * FROM logins WHERE sso_subject=?", (subject,)).fetchone()
        if u:
            return u, subject, ""
        if c["match"] == "email":
            email = (claims.get("email") or "").strip().lower()
            if not email:
                return None, subject, "the provider didn't send an email address (add the \"email\" scope)"
            if c["verified_only"] and claims.get("email_verified") is not True:
                return None, subject, "the provider hasn't verified that email address"
            rows = db.execute("SELECT * FROM logins WHERE lower(trim(vm_email))=?", (email,)).fetchall()
            if len(rows) > 1:
                return None, subject, "more than one login here uses that email; ask the admin to link it"
            u = rows[0] if rows else None
            if not u:
                return None, subject, "no login here has that email as its notification email"
        else:
            name = (claims.get("preferred_username") or "").strip()
            if not name:
                return None, subject, "the provider didn't send a username (add the \"profile\" scope)"
            u = db.execute("SELECT * FROM logins WHERE lower(username)=lower(?)", (name,)).fetchone()
            if not u:
                return None, subject, "no login here has that username"
        if (u["sso_subject"] or "") not in ("", subject):
            return None, subject, "that login is already linked to a different single sign-on account"
        return u, subject, ""


def callback(request: Request):
    """GET /login/oidc/callback?code=…&state=…"""
    c = cfg()
    if not ready(c):
        return RedirectResponse("/login?password=1", status_code=302)
    p = request.query_params
    state = p.get("state") or ""
    pend = _pending.pop(state, None)
    if not state or not pend or not secrets.compare_digest(state, request.cookies.get(STATE_COOKIE) or ""):
        return _fail(request, "That sign-in link expired or was opened in another browser. Try again.")
    if time.time() - pend["at"] > STATE_TTL:
        return _fail(request, "That sign-in took too long. Try again.")
    if p.get("error"):
        return _fail(request, "The sign-in provider said: " + (p.get("error_description") or p.get("error"))[:200])
    code = p.get("code") or ""
    if not code:
        return _fail(request, "The sign-in provider didn't send a code. Try again.")
    try:
        d = discovery(c["issuer"])
        basic = base64.b64encode(f"{urllib.parse.quote(c['client_id'], safe='')}:"
                                 f"{urllib.parse.quote(c['client_secret'], safe='')}".encode()).decode()
        tok = _http(d["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": pend["redirect_uri"],
            "code_verifier": pend["verifier"], "client_id": c["client_id"],
        }, headers={"Authorization": "Basic " + basic})
        if not tok.get("id_token"):
            raise ValueError("the provider didn't send an ID token (is \"openid\" allowed for this app?)")
        claims = _jwt_claims(tok["id_token"])
        aud = claims.get("aud")
        if not _same_issuer(claims.get("iss"), d.get("issuer")):
            raise ValueError("the ID token is from a different issuer")
        if c["client_id"] not in (aud if isinstance(aud, list) else [aud]):
            raise ValueError("the ID token is for a different client ID")
        if float(claims.get("exp") or 0) < time.time() - 60:
            raise ValueError("the ID token has expired (check the PBX clock)")
        if not secrets.compare_digest(str(claims.get("nonce") or ""), pend["nonce"]):
            raise ValueError("the ID token doesn't belong to this sign-in")
        if not claims.get("sub"):
            raise ValueError("the ID token has no subject")
        if d.get("userinfo_endpoint") and tok.get("access_token"):
            try:
                info = _http(d["userinfo_endpoint"], headers={"Authorization": "Bearer " + tok["access_token"]})
                if str(info.get("sub")) == str(claims["sub"]):
                    claims = {**info, **{k: v for k, v in claims.items() if k in ("iss", "aud", "exp", "nonce", "sub")}}
            except ValueError:
                pass      # the ID token's own claims will do
    except ValueError as e:
        return _fail(request, f"Single sign-on didn't work: {e}.")
    who = str(claims.get("preferred_username") or claims.get("email") or claims.get("sub"))
    u, subject, why = _find_login(c, claims)
    if not u:
        return _fail(request, f"Signed in at the provider as {who}, but {why}.", who)
    if not u["enabled"]:
        return _fail(request, "That login is turned off.", who)
    if u["role"] == "admin" and not c["admins"]:
        return _fail(request, "Admin logins sign in with their password here.", who)
    if not u["sso_subject"]:
        with M.db() as db:
            db.execute("UPDATE logins SET sso_subject=? WHERE id=?", (subject, u["id"]))
            db.commit()
        M._audit_log(u["username"], "sso.link", f"linked to {who} at {c['issuer']}")
    M._log_signin(request, f"{u['username']} (single sign-on)", True)
    tok_s = M._new_session(u)
    resp = RedirectResponse(pend.get("next") or ("/" if u["role"] == "admin" else "/ucp"), status_code=302)
    M._set_session_cookie(request, resp, tok_s)
    resp.delete_cookie(STATE_COOKIE, path=CALLBACK)
    return resp


def next_param(v):
    """A safe place to return to after signing in (only the app's connect
    page), else ''."""
    v = v or ""
    return v if M.app_api.connect_next_ok(v) else ""


def login_box(request: Request, err: str = "", force_form: bool = False, nxt: str | None = None):
    """The login page body: SSO button above the password form."""
    c = cfg()
    nxt = next_param(request.query_params.get("next") if nxt is None else nxt)
    base = M.login_html()
    if nxt:   # come back to it after the password form too
        base = base.replace('<form method="post" action="/login">',
                            f'<form method="post" action="/login"><input type="hidden" name="next" value="{esc(nxt)}">', 1)
    msg = f"<p style='color:red'>{esc(err)}</p>" if err else ""
    if not ready(c):
        return base + msg
    href = "/login/oidc" + ("?" + urllib.parse.urlencode({"next": nxt}) if nxt else "")
    btn = (f'<p><a class="btn" href="{esc(href)}" style="display:inline-block;min-width:220px;text-align:center;text-decoration:none">'
           f'{esc(button_label(c))}</a></p>')
    show_form = (not c["hide_password"]) or force_form or request.query_params.get("password") == "1"
    if not show_form:
        i = base.find("<form")
        head = base[:i] if i >= 0 else ""
        return head + btn + msg
    i = base.find("<form")
    if i < 0:
        return base + btn + msg
    return base[:i] + btn + '<p class="muted" style="margin:6px 0">or with your password</p>' + base[i:] + msg


def should_auto_redirect(request: Request):
    c = cfg()
    q = request.query_params
    return (ready(c) and c["auto"] and q.get("password") != "1" and q.get("out") != "1"
            and not M._sess(request))


# ------------------------------------------------------------------ admin page

def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _back(msg="", err=""):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/sso" + ("?" + q if q else ""), status_code=303)


def page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    csrf = M._csrf_field(s)
    c = cfg()
    flash = ""
    if request.query_params.get("msg"):
        flash += f'<div class="flash ok">{esc(request.query_params["msg"])}</div>'
    if request.query_params.get("err"):
        flash += f'<div class="flash bad">{esc(request.query_params["err"])}</div>'
    state = ('<span class="pill ok">On</span>' if ready(c) else
             '<span class="pill warn">Incomplete</span>' if c["enabled"] else '<span class="pill">Off</span>')
    ruri = redirect_uri(request, c)
    with M.db() as db:
        linked = db.execute("SELECT id, username, exten, display_name, sso_subject FROM logins"
                            " WHERE sso_subject != '' ORDER BY username").fetchall()
    rows = "".join(
        f"<tr><td>{esc(r['display_name'] or r['username'])} <span class='muted'>{esc(r['username'])}</span></td>"
        f"<td>{esc(r['exten'])}</td><td><code>{esc(r['sso_subject'].split('|', 1)[-1])}</code></td>"
        f"<td><form method='post' action='/sso/unlink' class='inline'>{csrf}"
        f"<input type='hidden' name='id' value='{r['id']}'><button class='btn ghost'>Unlink</button></form></td></tr>"
        for r in linked) or "<tr><td colspan='4' class='muted'>Nobody has signed in this way yet.</td></tr>"
    chk = lambda on: "checked" if on else ""
    body = f"""{flash}<h2>Single sign-on {state}</h2>
<div class="grid2">
<section class="panel">
<p class="muted">Let people sign in to the web panel through an OpenID Connect provider you already run:
Authentik, Keycloak, Authelia and the like. The login page then shows a button above the password form,
which stays. The My Phone app isn't affected: it still pairs with a code.</p>
<form method="post" action="/sso/settings">{csrf}
<label class="switch"><input type="checkbox" name="enabled" value="1" {chk(c['enabled'])}> <span>Turn on single sign-on</span></label>
<label>Panel address from outside<br><input name="base_url" value="{esc(c['base_url'])}" placeholder="https://myphone.example.com"></label>
<p class="muted">The address people open the panel with (where the provider sends them back). Empty = the address the login page was opened with.</p>
<label>Redirect URI<br><input value="{esc(ruri)}" readonly onclick="this.select()"></label>
<p class="muted">Register this at the provider as the redirect URI, exactly as shown.</p>
<label>Issuer<br><input name="issuer" value="{esc(c['issuer'])}" placeholder="https://auth.example.com/application/o/pbx/"></label>
<p class="muted">The provider's issuer URL, exactly as its discovery document states it. In Authentik it is the "OpenID Configuration Issuer" on the provider's page.</p>
<label>Client ID<br><input name="client_id" value="{esc(c['client_id'])}" autocomplete="off"></label>
<label>Client secret<br><input name="client_secret" type="password" autocomplete="new-password" placeholder="{'Leave blank to keep the stored secret' if c['client_secret'] else 'paste the client secret'}"></label>
<label class="inline-chk"><input type="checkbox" name="forget_secret" value="1"> Forget the stored client secret</label>
<label>Name on the button<br><input name="button" value="{esc(c['button'])}" placeholder="Authentik"></label>
<p class="muted">The login button reads "Sign in with …" and this name. Empty says "single sign-on".</p>
<label>Scopes<br><input name="scopes" value="{esc(c['scopes'])}" placeholder="email profile"></label>
<p class="muted">Separated by spaces. "openid" is always added. Empty asks for openid, email and profile.</p>
<label>Match a first sign-in to a login by<br><select name="match">
<option value="username" {'selected' if c['match'] == 'username' else ''}>Username: the provider's username is the PBX login name</option>
<option value="email" {'selected' if c['match'] == 'email' else ''}>Email: the provider's email is the login's notification email</option>
</select></label>
<p class="muted">Only the first time: after that the login stays linked to that provider account (unlink it below).
Accounts aren't created here — add the login (with its extension) first.</p>
<label class="inline-chk"><input type="checkbox" name="verified_only" value="1" {chk(c['verified_only'])}> Only link an email the provider has verified</label>
<label class="inline-chk"><input type="checkbox" name="admins" value="1" {chk(c['admins'])}> Admin logins may sign in this way</label>
<p class="muted">Admin pages stay office-network only either way.</p>
<label class="inline-chk"><input type="checkbox" name="auto" value="1" {chk(c['auto'])}> Go straight to the sign-in provider</label>
<p class="muted">Opening the panel takes people directly to the provider instead of this login page. The password form is
still there after signing out, after a failed sign-in, and at <code>/login?password=1</code>, so a provider that is down never locks anyone out.</p>
<label class="inline-chk"><input type="checkbox" name="hide_password" value="1" {chk(c['hide_password'])}> Hide the username and password form</label>
<p class="muted">The login page shows only the sign-in button. The form comes back after a failed sign-in and at
<code>/login?password=1</code> — bookmark that, it is the way in if the provider is down.</p>
<button class="btn">Save</button>
</form>
<form method="post" action="/sso/test" class="inline">{csrf}<button class="btn ghost" {'disabled' if not c['issuer'] else ''}>Test the issuer</button></form>
</section>
<section class="panel">
<h3>Linked logins</h3>
<table><tr><th>Login</th><th>Ext</th><th>Provider account</th><th></th></tr>{rows}</table>
<h3 style="margin-top:18px">Authentik in short</h3>
<ol class="checklist">
<li>Applications → Providers → <b>Create</b> → OAuth2/OpenID Provider. Client type <b>Confidential</b>.</li>
<li>Redirect URI: <code>{esc(ruri)}</code> (strict).</li>
<li>Scopes: keep <b>openid</b>, <b>email</b> and <b>profile</b>.</li>
<li>Create an Application using that provider, and bind the users or group allowed in.</li>
<li>Copy the Client ID, Client Secret and the <b>OpenID Configuration Issuer</b> here.</li>
</ol>
</section>
</div>"""
    return HTMLResponse(M.page("Single sign-on", body, s["username"], s["role"], "sso"))


async def _post(request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return s, M._panel_locked(s, "sso")
    return s, None


async def save(request: Request):
    s, locked = await _post(request)
    if locked:
        return locked
    f = await request.form()
    issuer = (f.get("issuer") or "").strip()
    base = (f.get("base_url") or "").strip().rstrip("/")
    for v, label in ((issuer, "Issuer"), (base, "Panel address")):
        if v and not (v.startswith("https://") or v.startswith("http://")):
            return _back(err=f"{label} must start with https://")
    if issuer and not issuer.startswith("https://"):
        return _back(err="The issuer must be an https:// address.")
    scopes = " ".join((f.get("scopes") or "").replace(",", " ").split())
    if any(not ch.isalnum() and ch not in " _-.:/" for ch in scopes):
        return _back(err="Scopes are words separated by spaces.")
    secret = f.get("client_secret") or ""
    if f.get("forget_secret"):
        _set("sso_client_secret", "")
    elif secret.strip():
        _set("sso_client_secret", secret.strip())
    match = f.get("match") if f.get("match") in ("username", "email") else "username"
    enabled = bool(f.get("enabled"))
    if issuer != _kv("sso_issuer"):
        _disco.clear()
    _set("sso_issuer", issuer)
    _set("sso_base_url", base)
    _set("sso_client_id", (f.get("client_id") or "").strip())
    _set("sso_button", (f.get("button") or "").strip()[:40])
    _set("sso_scopes", scopes)
    _set("sso_match", match)
    _set("sso_verified_only", "1" if f.get("verified_only") else "0")
    _set("sso_admins", "1" if f.get("admins") else "0")
    _set("sso_auto_redirect", "1" if f.get("auto") else "0")
    _set("sso_hide_password", "1" if f.get("hide_password") else "0")
    _set("sso_enabled", "1" if enabled else "0")
    M._audit_log(s["username"], "sso.settings", f"enabled={enabled} issuer={issuer} match={match}")
    c = cfg()
    if enabled and not ready(c):
        return _back(err="Saved, but single sign-on stays off until the issuer, client ID and client secret are all set.")
    return _back(msg="Saved." + (" Try it in a private window before signing out." if enabled else ""))


async def test(request: Request):
    s, locked = await _post(request)
    if locked:
        return locked
    from starlette.concurrency import run_in_threadpool
    try:
        d = await run_in_threadpool(discovery, cfg()["issuer"], True)
    except ValueError as e:
        return _back(err=f"Issuer test failed: {e}.")
    return _back(msg=f"The issuer works: sign-in goes to {urllib.parse.urlsplit(d['authorization_endpoint']).netloc}.")


async def unlink(request: Request):
    s, locked = await _post(request)
    if locked:
        return locked
    f = await request.form()
    try:
        lid = int(f.get("id") or 0)
    except ValueError:
        return _back(err="Unknown login.")
    with M.db() as db:
        r = db.execute("SELECT username FROM logins WHERE id=?", (lid,)).fetchone()
        db.execute("UPDATE logins SET sso_subject='' WHERE id=?", (lid,))
        db.commit()
    if r:
        M._audit_log(s["username"], "sso.unlink", r["username"])
    return _back(msg=f"Unlinked {r['username'] if r else 'that login'}. Their next single sign-on links again by the match rule.")


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.get("/login/oidc")(start)
    app.get(CALLBACK)(callback)
    app.get("/sso", response_class=HTMLResponse)(page)
    app.post("/sso/settings")(save)
    app.post("/sso/test")(test)
    app.post("/sso/unlink")(unlink)
