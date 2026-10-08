# SPDX-License-Identifier: GPL-2.0-or-later
"""Security events, email alerts and the dashboard Activity feed.

Events come from:
  - the Kamailio edge box: fail2ban bans/unbans, reported by
    /usr/local/sbin/pbx-edge-notify -> POST /api/v1/security/events
    (Bearer API key of an admin)
  - this panel: sign-in lockouts (too many failed passwords), kill switch,
    safety lock

Alerts are emailed with the SMTP settings from the Email page. Throttled so
an attack can't flood your inbox: one email per (event type, IP) per hour
and at most ALERT_MAX_PER_HOUR emails per hour in total.

GET /api/v1/activity merges these events with the change log (audit
table) for the dashboard's live Activity feed.
"""
import ipaddress
import threading
import urllib.parse
import time

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

import mailer

M = None
RETENTION = "-1 day"   # security events auto-delete after 24 hours
ALERT_MAX_PER_HOUR = 20
KINDS = {
    "ban": "IP blocked",
    "unban": "IP unblocked",
    "flood": "Flood blocked",
    "signin_lockout": "Sign-ins locked out",
    "kill_switch": "Kill switch",
    "safety_lock": "Safety lock",
    "e911_change": "E911 settings changed",
    "e911_unlock_fail": "Wrong E911 unlock code",
    "emergency": "911 call",
    "report": "User report",
    "recording_change": "Admin recording switched",
    "test": "Test",
}
# Which kinds email, and the setting that turns each group on/off.
ALERT_GROUP = {"ban": "sec_alert_bans", "flood": "sec_alert_bans",
               "signin_lockout": "sec_alert_signin",
               "kill_switch": "sec_alert_safety", "safety_lock": "sec_alert_safety",
               "e911_change": "sec_alert_safety", "e911_unlock_fail": "sec_alert_safety",
               "report": "sec_alert_reports", "recording_change": "sec_alert_safety",
               "test": None}
JAIL_NAMES = {"kamailio-edge": "scanning / password guessing on the phone server",
              "kamailio-edge-flood": "flooding the phone server with requests",
              "setup": "edge box connected", "test": "test"}

_alert_lock = threading.Lock()
_sent_keys: dict[str, float] = {}
_sent_times: list[float] = []


def _kv(key, default=""):
    with M.db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set(key, value):
    with M.db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (key, str(value)))
        c.commit()


def _clean(v, n=400):
    return "".join(ch for ch in str(v or "") if ch.isprintable())[:n]


def _clean_ip(v):
    try:
        return str(ipaddress.ip_address((v or "").strip()))
    except ValueError:
        return ""


# ---------------------------------------------------------------- record

def record(kind, ip="", detail="", source="panel", jail="", actor=""):
    """Store a security event and send an alert email if that's turned on.
    Never raises (security logging must not break the caller)."""
    kind = kind if kind in KINDS else "test"
    ip, detail, jail, source = _clean_ip(ip), _clean(detail), _clean(jail, 60), _clean(source, 20)
    try:
        with M.db() as c:
            c.execute("INSERT INTO security_events (source, kind, ip, jail, detail, actor) VALUES (?,?,?,?,?,?)",
                      (source, kind, ip, jail, detail, _clean(actor, 80)))
            c.commit()
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: security event not stored: {e}", flush=True)
        return
    threading.Thread(target=_maybe_alert, args=(kind, ip, detail, source, jail, actor),
                     daemon=True, name="sec-alert").start()


def _alert_to():
    return (_kv("sec_alert_to") or "").strip()


def _maybe_alert(kind, ip, detail, source, jail, actor):
    try:
        group = ALERT_GROUP.get(kind, "")
        if group is None and kind != "test":
            return
        if kind not in ALERT_GROUP:  # unban etc.: feed only
            return
        if group and _kv(group, "1") != "1":
            return
        to = _alert_to()
        if not to:
            return
        now = time.time()
        key = f"{kind}:{ip}"
        if kind in ("kill_switch", "safety_lock", "e911_change", "recording_change"):
            key += ":" + detail[:10]  # engage and release each get an email
        if kind == "report":
            key += ":" + detail       # every report is its own email
        with _alert_lock:
            if kind != "test" and now - _sent_keys.get(key, 0) < 3600:
                return
            _sent_times[:] = [t for t in _sent_times if now - t < 3600]
            if len(_sent_times) >= ALERT_MAX_PER_HOUR:
                return
            _sent_keys[key] = now
            _sent_times.append(now)
            if len(_sent_keys) > 2000:
                for k in [k for k, t in _sent_keys.items() if now - t > 3600]:
                    _sent_keys.pop(k, None)
        with M.db() as c:
            cfg = mailer.settings(c)
        if not mailer.configured(cfg):
            return
        what = KINDS.get(kind, kind)
        where = {"edge": "phone server (edge box)", "panel": "web panel"}.get(source, source)
        lines = [f"{what} on your {where}.", ""]
        if ip:
            lines.append(f"IP address: {ip}")
        if jail:
            lines.append(f"Reason: {JAIL_NAMES.get(jail, jail)}")
        if actor:
            lines.append(f"By: {actor}")
        if detail:
            lines.append(f"Details: {detail}")
        lines += ["", f"Time: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", ""]
        if kind in ("ban", "flood"):
            lines.append("No action is needed - the address is already blocked. If it's one of your "
                         "own phones (e.g. a wrong password saved in it), fix the phone and unblock it "
                         "on the edge box: fail2ban-client unban <ip>")
        elif kind == "signin_lockout":
            lines.append("Someone kept entering wrong passwords on the web panel. The address is "
                         "blocked from signing in for 15 minutes.")
        url = (cfg.get("panel_url") or "").rstrip("/")
        if url:
            lines += ["", f"Dashboard: {url}/"]
        lines += ["", "You get at most one email per address per hour for the same kind of event."]
        subj = f"Security alert: {what}" + (f" ({ip})" if ip else "")
        mailer.send(cfg, to, subj, "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: security alert email failed: {e}", flush=True)


# ---------------------------------------------------------------- API

class EventIn(BaseModel):
    kind: str
    ip: str = ""
    jail: str = ""
    detail: str = ""
    source: str = "edge"


async def post_event(request: Request, body: EventIn):
    # API key only (the edge box). Session cookies can't post fake events.
    if not request.headers.get("authorization", "").lower().startswith("bearer "):
        raise HTTPException(401, "bearer token required")
    M._v1_admin(request)
    if body.kind not in KINDS:
        raise HTTPException(400, "unknown kind")
    if body.ip and not _clean_ip(body.ip):
        raise HTTPException(400, "bad ip")
    kind = body.kind
    if kind == "ban" and body.jail.endswith("-flood"):
        kind = "flood"
    record(kind, body.ip, body.detail, source=body.source if body.source in ("edge", "panel") else "edge",
           jail=body.jail)
    return {"ok": True}


def activity(request: Request, limit: int = 60):
    M._v1_admin(request)
    limit = max(1, min(int(limit), 200))
    with M.db() as c:
        purge()
        ev = c.execute("SELECT id, at, source, kind, ip, jail, detail, actor FROM security_events "
                       "WHERE at >= datetime('now', ?) ORDER BY id DESC LIMIT ?", (RETENTION, limit)).fetchall()
        # The change log itself is kept (it holds recording-consent records);
        # the feed only shows its last 24 hours.
        au = c.execute("SELECT id, at, actor, action, detail FROM audit WHERE at >= datetime('now', ?) "
                       "ORDER BY id DESC LIMIT ?", (RETENTION, limit)).fetchall()
        blocked = c.execute(
            "SELECT COUNT(DISTINCT ip) FROM security_events WHERE kind IN ('ban','flood') "
            "AND at >= datetime('now','-1 day')").fetchone()[0]
        lockouts = c.execute(
            "SELECT COUNT(*) FROM security_events WHERE kind='signin_lockout' "
            "AND at >= datetime('now','-1 day')").fetchone()[0]
    items = [{"at": r["at"], "type": "security", "kind": r["kind"], "title": KINDS.get(r["kind"], r["kind"]),
              "source": r["source"], "ip": r["ip"],
              "detail": " · ".join(x for x in (JAIL_NAMES.get(r["jail"], r["jail"]) if r["jail"] else "",
                                               r["detail"]) if x),
              "actor": r["actor"]} for r in ev]
    items += [{"at": r["at"], "type": "change", "kind": r["action"], "title": r["action"],
               "source": "panel", "ip": "", "detail": r["detail"], "actor": r["actor"]} for r in au]
    items.sort(key=lambda x: x["at"], reverse=True)
    return {"items": items[:limit], "blocked_24h": blocked, "lockouts_24h": lockouts,
            "alerts_to": bool(_alert_to())}


# ---------------------------------------------------------------- settings (Email page)

def settings_panel(csrf):
    to = _kv("sec_alert_to")
    chk = lambda k: "checked" if _kv(k, "1") == "1" else ""  # noqa: E731
    esc = M.esc
    return f"""<section class="panel">
<h3>Security alerts</h3>
<p class="muted">Email when the edge box blocks an attacker, someone is locked out of the web panel
for wrong passwords, or the kill switch / safety lock changes. At most one email per address per hour
for the same event, and {ALERT_MAX_PER_HOUR} per hour in total.</p>
<form method="post" action="/security/alerts">{csrf}
<label>Send alerts to<br><input name="sec_alert_to" type="email" value="{esc(to)}" placeholder="you@example.com"></label>
<label class="switch"><input type="checkbox" name="sec_alert_bans" value="1" {chk('sec_alert_bans')}> <span>Blocked IPs on the phone server (scanners, password guessing, floods)</span></label>
<label class="switch"><input type="checkbox" name="sec_alert_signin" value="1" {chk('sec_alert_signin')}> <span>Web panel sign-in lockouts</span></label>
<label class="switch"><input type="checkbox" name="sec_alert_safety" value="1" {chk('sec_alert_safety')}> <span>Kill switch, safety lock and E911 changes</span></label>
<label class="switch"><input type="checkbox" name="sec_alert_reports" value="1" {chk('sec_alert_reports')}> <span>Calls and texts users report</span></label>
<button class="btn">Save</button>
<button class="btn ghost" name="send_test" value="1">Save &amp; send test alert</button>
</form>
<p class="muted">Edge box alerts need a one-time setup on the edge box (an admin API key from the
API keys page):<br><code>pbx-edge-notify --setup http://&lt;this-pbx&gt;:8001 &lt;API key&gt;</code></p>
</section>"""


def _go(msg="", err=""):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/email" + ("?" + q if q else ""), status_code=303)


async def save_alerts(request: Request):
    s = M._sess(request)
    if not s or s["role"] != "admin":
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "email")
    f = await request.form()
    to = mailer.clean(f.get("sec_alert_to"))
    if to and ("@" not in to or " " in to):
        return _go(err="Alert address doesn't look like an email address.")
    _set("sec_alert_to", to)
    for k in ("sec_alert_bans", "sec_alert_signin", "sec_alert_safety", "sec_alert_reports"):
        _set(k, "1" if f.get(k) else "0")
    try:
        with M.db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)",
                      (s["username"], "security.alerts", f"to={to or '-'}"))
            c.commit()
    except Exception:
        pass
    if f.get("send_test"):
        if not to:
            return _go(err="Add an address to send alerts to.")
        with M.db() as c:
            cfg = mailer.settings(c)
        from starlette.concurrency import run_in_threadpool
        try:
            await run_in_threadpool(mailer.send, cfg, to, "Security alert: test",
                                    "This is a test security alert from your phone system.\n\n"
                                    "Real alerts look like this and arrive when an attacker is blocked.")
        except mailer.MailError as e:
            return _go(err=f"Test alert failed: {e}")
        return _go(msg=f"Saved. Test alert sent to {to}.")
    return _go(msg="Security alert settings saved.")


# ---------------------------------------------------------------- housekeeping

def purge():
    try:
        with M.db() as c:
            c.execute("DELETE FROM security_events WHERE at < datetime('now', ?)", (RETENTION,))
            c.commit()
    except Exception:
        pass


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.post("/api/v1/security/events")(post_event)
    app.get("/api/v1/activity")(activity)
    app.post("/security/alerts")(save_alerts)
