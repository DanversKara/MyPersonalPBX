# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin Email page (/email): SMTP settings for voicemail notifications.

pbx-brain reads the same kv_settings when a voicemail arrives and emails
the mailbox owner's "voicemail email" (set in their control panel or on the
Logins page). The last send result is stored in kv 'smtp_last_result'.
"""
import time
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import mailer
import security

M = None
SECURITY = (("starttls", "STARTTLS (usually port 587)"), ("ssl", "SSL/TLS (usually port 465)"),
            ("none", "None (port 25, not recommended)"))


def esc(x):
    return M.esc(x)


def _kv(key, default=""):
    with M.db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set(key, value):
    with M.db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (key, value))
        c.commit()


def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _back(msg="", err=""):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/email" + ("?" + q if q else ""), status_code=303)


def page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    with M.db() as c:
        cfg = mailer.settings(c)
        n_with = c.execute("SELECT COUNT(*) FROM logins WHERE enabled=1 AND vm_email!='' AND exten!=''").fetchone()[0]
    csrf = M._csrf_field(s)
    flash = ""
    if request.query_params.get("msg"):
        flash += f'<div class="flash ok">{esc(request.query_params["msg"])}</div>'
    if request.query_params.get("err"):
        flash += f'<div class="flash bad">{esc(request.query_params["err"])}</div>'
    badge = ('<span class="pill ok">Set up</span>' if mailer.configured(cfg)
             else '<span class="pill bad">Not set up</span>')
    sec = cfg.get("smtp_security") or "starttls"
    sec_opts = "".join(f'<option value="{k}" {"selected" if k == sec else ""}>{esc(t)}</option>' for k, t in SECURITY)
    last = _kv("smtp_last_result")
    body = f"""{flash}<h2>Email {badge}</h2>
<p class="muted">Used to email users when they get a new voicemail. Each user sets their address in
<b>My Phone → Settings</b> (or you can on the Logins page). {n_with} user(s) have an address set.</p>
<div class="grid2">
<section class="panel">
<h3>SMTP server</h3>
<form method="post" action="/email/settings">{csrf}
<label>Server<br><input name="smtp_host" value="{esc(cfg['smtp_host'])}" placeholder="smtp.example.com"></label>
<label>Security<br><select name="smtp_security">{sec_opts}</select></label>
<label>Port<br><input name="smtp_port" value="{esc(cfg['smtp_port'])}" placeholder="587" inputmode="numeric" style="width:120px"></label>
<label>Username<br><input name="smtp_user" value="{esc(cfg['smtp_user'])}" autocomplete="off" placeholder="often your full email address"></label>
<label>Password<br><input name="smtp_password" type="password" autocomplete="new-password" placeholder="{'(saved — leave empty to keep)' if cfg['smtp_password'] else ''}"></label>
<label class="inline-chk"><input type="checkbox" name="clear_password" value="1"> Clear saved password</label>
<label>From address<br><input name="smtp_from" type="email" value="{esc(cfg['smtp_from'])}" placeholder="pbx@example.com"></label>
<label>From name<br><input name="smtp_from_name" value="{esc(cfg['smtp_from_name'])}" placeholder="Office Phone System"></label>
<label class="switch"><input type="checkbox" name="vm_email_attach" value="1" {'checked' if cfg['vm_email_attach'] == '1' else ''}> <span>Attach the voicemail recording (WAV)</span></label>
<label>Panel address for links in emails (optional)<br><input name="panel_url" value="{esc(cfg['panel_url'])}" placeholder="https://pbx.example.com"></label>
<button class="btn">Save</button>
</form>
</section>
<section class="panel">
<h3>Send a test email</h3>
<form method="post" action="/email/test">{csrf}
<label>To<br><input name="to" type="email" required placeholder="you@example.com"></label>
<button class="btn ghost" {'disabled' if not mailer.configured(cfg) else ''}>Send test</button>
</form>
<h3 style="margin-top:20px">Last email</h3>
<p class="muted">{esc(last) or 'Nothing sent yet.'}</p>
<h3 style="margin-top:20px">Common settings</h3>
<table class="kv">
<tr><td>Gmail / Google Workspace</td><td>smtp.gmail.com · STARTTLS · 587 · an <i>app password</i></td></tr>
<tr><td>Microsoft 365</td><td>smtp.office365.com · STARTTLS · 587</td></tr>
<tr><td>Amazon SES</td><td>email-smtp.&lt;region&gt;.amazonaws.com · STARTTLS · 587</td></tr>
<tr><td>SendGrid</td><td>smtp.sendgrid.net · STARTTLS · 587 · user <code>apikey</code></td></tr>
</table>
<p class="muted">Many home internet providers block outgoing port 25; use 587 or 465.</p>
</section>
{security.settings_panel(csrf)}
</div>"""
    return HTMLResponse(M.page("Email", body, s["username"], s["role"], "email"))


async def save(request: Request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "email")
    f = await request.form()
    host = mailer.clean(f.get("smtp_host"))
    port = (f.get("smtp_port") or "").strip()
    sec = f.get("smtp_security") if f.get("smtp_security") in ("starttls", "ssl", "none") else "starttls"
    frm = mailer.clean(f.get("smtp_from"))
    url = (f.get("panel_url") or "").strip().rstrip("/")
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return _back(err="Port must be a number between 1 and 65535.")
    if frm and ("@" not in frm or " " in frm):
        return _back(err="The From address doesn't look like an email address.")
    if url and not url.startswith(("http://", "https://")):
        return _back(err="Panel address must start with https:// (or http://).")
    _set("smtp_host", host)
    _set("smtp_port", port)
    _set("smtp_security", sec)
    _set("smtp_user", mailer.clean(f.get("smtp_user")))
    if f.get("clear_password"):
        _set("smtp_password", "")
    elif f.get("smtp_password"):
        _set("smtp_password", f.get("smtp_password"))
    _set("smtp_from", frm)
    _set("smtp_from_name", mailer.clean(f.get("smtp_from_name"))[:80])
    _set("vm_email_attach", "1" if f.get("vm_email_attach") else "0")
    _set("panel_url", url)
    try:
        with M.db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)",
                      (s["username"], "email.settings", f"host={host} port={port} security={sec}"))
            c.commit()
    except Exception:
        pass
    return _back(msg="Email settings saved. Send a test to check them.")


async def test(request: Request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    f = await request.form()
    to = mailer.clean(f.get("to"))
    with M.db() as c:
        cfg = mailer.settings(c)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    from starlette.concurrency import run_in_threadpool
    try:
        await run_in_threadpool(mailer.send, cfg, to, "PBX test email",
                                "This is a test from your phone system. Voicemail notifications will look like this sender.")
    except mailer.MailError as e:
        _set("smtp_last_result", f"{stamp}: test to {to} FAILED: {e}")
        return _back(err=f"Test failed: {e}")
    _set("smtp_last_result", f"{stamp}: test to {to} sent")
    return _back(msg=f"Test email sent to {to}. Check the inbox (and spam folder).")


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.get("/email", response_class=HTMLResponse)(page)
    app.post("/email/settings")(save)
    app.post("/email/test")(test)
