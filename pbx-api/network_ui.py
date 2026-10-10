# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin Network page (/network): the public SIP domain users connect to from
outside, and Cloudflare dynamic DNS that keeps it pointed at this site.

Users see the domain, port and TLS/SRTP settings under My Phone -> Settings
-> Phone setup.
"""
import re
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import ddns

M = None
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def esc(x):
    return M.esc(x)


def _kv(key, default=""):
    with M.db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set(key, value):
    with M.db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (key, str(value)))
        c.commit()


def remote_settings():
    """What phones need from outside the office (used by the user panel)."""
    return {"domain": _kv("sip_domain"), "port": _kv("sip_tls_port") or "5061",
            "lan_host": _kv("lan_sip_host")}


def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _back(msg="", err=""):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/network" + ("?" + q if q else ""), status_code=303)


def _audit(actor, action, detail=""):
    try:
        with M.db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)", (actor, action, detail))
            c.commit()
    except Exception:
        pass


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
    dom, port = _kv("sip_domain"), _kv("sip_tls_port") or "5061"
    tok = _kv("cf_token")
    enabled = _kv("ddns_enabled") == "1"
    pub, dns_ip = _kv("ddns_public_ip"), _kv("ddns_dns_ip")
    last, err = _kv("ddns_last_check"), _kv("ddns_last_error")
    if not enabled:
        state = '<span class="pill">Off</span>'
    elif err:
        state = '<span class="pill bad">Problem</span>'
    elif pub and pub == dns_ip:
        state = '<span class="pill ok">In sync</span>'
    else:
        state = '<span class="pill warn">Waiting for first check</span>'
    with M.db() as c:
        hist = c.execute("SELECT at, old_ip, new_ip FROM ip_changes ORDER BY id DESC LIMIT 15").fetchall()
    hrows = "".join(f"<tr><td>{esc(h[0])}</td><td>{esc(h[1] or '-')}</td><td>{esc(h[2])}</td></tr>" for h in hist) \
        or '<tr><td colspan="3" class="muted">No changes recorded yet.</td></tr>'
    masked = (tok[:4] + "…" + tok[-4:]) if len(tok) > 12 else ("saved" if tok else "")
    body = f"""{flash}<h2>Network</h2>
<div class="grid2">
<section class="panel">
<h3>Remote access</h3>
<p class="muted">The address phones use outside the office. It points at your Kamailio edge proxy
(router forward TCP {esc(port)} and UDP 20000-20099 to it).</p>
<form method="post" action="/network/settings">{csrf}
<label>SIP domain<br><input name="sip_domain" value="{esc(dom)}" placeholder="sip.example.com"></label>
<label>TLS port<br><input name="sip_tls_port" value="{esc(port)}" inputmode="numeric" style="width:120px"></label>
<label>In-office server address (optional)<br><input name="lan_sip_host" value="{esc(_kv('lan_sip_host'))}" placeholder="e.g. 192.168.1.10"></label>
<p class="muted">Shown to users for phones on the office network. Empty = the address the panel was opened with.</p>
<h3 style="margin-top:18px">Cloudflare dynamic DNS</h3>
<p class="muted">Checks your public IP every few minutes and updates the domain's A record when your ISP
changes it. Create a token at Cloudflare → My Profile → API Tokens → "Edit zone DNS", limited to this domain's zone.</p>
<label>API token<br><input name="cf_token" type="password" autocomplete="off" placeholder="{esc(masked) or 'paste token'}"></label>
<label class="inline-chk"><input type="checkbox" name="clear_token" value="1"> Remove saved token</label>
<label>DNS record (if different from the SIP domain)<br><input name="cf_record" value="{esc(_kv('cf_record'))}" placeholder="{esc(dom) or 'sip.example.com'}"></label>
<label>Check every<br><select name="ddns_interval">{''.join(f'<option value="{v}" {"selected" if ddns.interval() == v else ""}>{v // 60} minute{"s" if v > 60 else ""}</option>' for v in (60, 120, 300, 600, 900))}</select></label>
<label>Email me when the IP changes (optional)<br><input name="ddns_notify" type="email" value="{esc(_kv('ddns_notify'))}" placeholder="you@example.com"></label>
<label class="switch"><input type="checkbox" name="ddns_enabled" value="1" {'checked' if enabled else ''}> <span>Keep DNS updated automatically</span></label>
<button class="btn">Save</button>
</form>
<form method="post" action="/network/test" class="inline">{csrf}<button class="btn ghost" {'disabled' if not tok else ''}>Test token</button></form>
<form method="post" action="/network/check" class="inline">{csrf}<button class="btn ghost" {'disabled' if not tok else ''}>Check &amp; update now</button></form>
<h3 style="margin-top:22px">Panel access from the internet</h3>
<p class="muted">When this panel is published through a Cloudflare Tunnel, visitors from the internet
get My Phone only. Admin pages open only on the office network unless you allow them below.
You're connected {'<b>from the internet</b>' if M.is_remote(request) else '<b>from the office network</b>'}
<span class="muted">({esc(M.proxy_info(request))})</span>. Use <code>http://&lt;pbx-ip&gt;:8001</code> at the office for admin pages:
anything that comes through the tunnel / proxy counts as internet.</p>
<form method="post" action="/network/admin-remote">{csrf}
<label class="switch"><input type="checkbox" name="admin_remote" value="1" {'checked' if _kv('admin_remote') == '1' else ''}> <span>Allow admin pages from the internet (not recommended)</span></label>
<button class="btn ghost">Save</button>
</form>
</section>
<section class="panel">
<h3>Status {state}</h3>
<table class="kv">
<tr><td>Public IP</td><td><code>{esc(pub) or '-'}</code></td></tr>
<tr><td>DNS {esc(ddns.record_name() or '')}</td><td><code>{esc(dns_ip) or '-'}</code></td></tr>
<tr><td>Last check</td><td>{esc(last) or '-'}</td></tr>
{'<tr><td>Problem</td><td class="bad">' + esc(err) + '</td></tr>' if err else ''}
</table>
<h3 style="margin-top:18px">IP changes</h3>
<table><tr><th>When</th><th>Old</th><th>New</th></tr>{hrows}</table>
<h3 style="margin-top:18px">Checklist for a domain</h3>
<ul class="checklist">
<li>The Cloudflare record must be <b>DNS only</b> (grey cloud). Cloudflare can't proxy SIP or call audio, so this page always sets it that way.</li>
<li>The Kamailio edge box must learn new IPs too, or calls connect with no audio. Run once on it:
<code>cd /root/kamailio &amp;&amp; ./update-public-ip.sh --install-timer</code></li>
<li>Give the edge box a certificate for <b>{esc(dom) or 'your domain'}</b>, or phones will reject the TLS connection:
<code>cd /root/kamailio &amp;&amp; CF_Token=… ./get-cert-cloudflare.sh {esc(dom) or 'sip.example.com'} you@example.com</code></li>
<li>Ask users to set their phone's re-registration to 60–120 seconds so they reconnect quickly after an IP change.</li>
</ul>
</section>
</div>"""
    return HTMLResponse(M.page("Network", body, s["username"], s["role"], "network"))


async def _post(request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    return s


async def save(request: Request):
    s = await _post(request)
    if M._admin_locked():
        return M._panel_locked(s, "network")
    f = await request.form()
    dom = (f.get("sip_domain") or "").strip().lower().rstrip(".")
    rec = (f.get("cf_record") or "").strip().lower().rstrip(".")
    port = (f.get("sip_tls_port") or "5061").strip()
    lan = (f.get("lan_sip_host") or "").strip()
    if lan and not re.match(r"^[A-Za-z0-9.-]{1,253}$", lan):
        return _back(err="In-office server address must be an IP address or host name.")
    notify = (f.get("ddns_notify") or "").strip()
    for v, label in ((dom, "SIP domain"), (rec, "DNS record")):
        if v and not DOMAIN_RE.match(v):
            return _back(err=f"{label} doesn't look like a domain name (e.g. sip.example.com).")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        return _back(err="TLS port must be a number between 1 and 65535.")
    if notify and ("@" not in notify or " " in notify):
        return _back(err="Notification email doesn't look like an email address.")
    try:
        iv = int(f.get("ddns_interval") or ddns.DEFAULT_INTERVAL)
    except ValueError:
        iv = ddns.DEFAULT_INTERVAL
    enabled = bool(f.get("ddns_enabled"))
    tok = (f.get("cf_token") or "").strip()
    if f.get("clear_token"):
        _set("cf_token", "")
        enabled = False
    elif tok:
        if len(tok) < 20 or any(ch.isspace() for ch in tok):
            return _back(err="That doesn't look like a Cloudflare API token.")
        _set("cf_token", tok)
    if enabled and not (dom or rec):
        return _back(err="Add the SIP domain before turning on automatic DNS updates.")
    if enabled and not _kv("cf_token"):
        return _back(err="Add a Cloudflare API token before turning on automatic DNS updates.")
    if (rec or dom) != ddns.record_name():
        _set("cf_zone_id", "")
    _set("sip_domain", dom)
    _set("sip_tls_port", port)
    _set("lan_sip_host", lan)
    _set("cf_record", rec)
    _set("ddns_interval", max(ddns.MIN_INTERVAL, min(ddns.MAX_INTERVAL, iv)))
    _set("ddns_notify", notify)
    _set("ddns_enabled", "1" if enabled else "0")
    _audit(s["username"], "network.settings", f"domain={dom} port={port} ddns={enabled}")
    return _back(msg="Saved." + (" Press 'Check & update now' to update DNS right away." if enabled else ""))


async def admin_remote(request: Request):
    s = await _post(request)
    if M._admin_locked():
        return M._panel_locked(s, "network")
    f = await request.form()
    on = bool(f.get("admin_remote"))
    _set("admin_remote", "1" if on else "0")
    _audit(s["username"], "network.admin_remote", f"enabled={on}")
    return _back(msg="Admin pages are now " + ("reachable from the internet." if on else "office-network only."))


async def test_token(request: Request):
    await _post(request)
    from starlette.concurrency import run_in_threadpool
    try:
        zones = await run_in_threadpool(ddns.verify_token, _kv("cf_token"))
    except ddns.DDNSError as e:
        return _back(err=f"Token test failed: {e}")
    return _back(msg=f"Token works. Zones it can manage: {zones}")


async def check(request: Request):
    s = await _post(request)
    from starlette.concurrency import run_in_threadpool
    r = await run_in_threadpool(ddns.check_now, True)
    if not r.get("ok"):
        return _back(err=r.get("error", "Check failed"))
    _audit(s["username"], "network.ddns_check", f"ip={r['public_ip']}")
    return _back(msg=f"Public IP {r['public_ip']}; {ddns.record_name()} now points to {r['dns_ip']}"
                     + (" (changed)." if r.get("changed") else " (already correct)."))


def install(app_module):
    global M
    M = app_module
    ddns.install(app_module)
    app = M.app
    app.get("/network", response_class=HTMLResponse)(page)
    app.post("/network/settings")(save)
    app.post("/network/test")(test_token)
    app.post("/network/admin-remote")(admin_remote)
    app.post("/network/check")(check)
