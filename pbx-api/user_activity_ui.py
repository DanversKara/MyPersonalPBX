# SPDX-License-Identifier: GPL-2.0-or-later
"""Per-user activity page (/logins/<id>/activity).

Shows one login's Activity-feed rows (audit changes + security events)
for the last 24 hours, or 7 / 30 / 90 days (?days=), since these records
are kept 90 days in the database while the dashboard shows only 24 hours, in the same plain-language layout as a standalone
activity report, with a download link for the standalone HTML file.
Sign-in rows also show the IP from the panel sign-in log.

Admin-only: both the page and the download require the admin role.
A user's activity is keyed by their session username, which is what the
audit and security tables record as actor.
"""

import re
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

M = None

LIMIT = 5000
RANGES = {90: "last 90 days", 30: "last 30 days", 7: "last 7 days", 1: "last 24 hours"}


def _days(request):
    try:
        d = int(request.query_params.get("days") or 90)
    except ValueError:
        d = 90
    return d if d in RANGES else 90

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    _TZ = None

_MEANS = {
    "app.signin": "Signed into the app",
    "app.pair_code": "Browser sign-in pairing",
    "app.signout": "Signed out of the app",
    "app.sip_qr": "Viewed SIP QR code",
    "ucp.calls": "Changed call settings",
    "ucp.prefs": "Changed preferences",
    "ucp.password": "Changed password",
    "ucp.sip_secret": "New SIP password",
    "ucp.recording_consent": "Accepted recording-law notice",
    "privacy.request": "Privacy request",
    "privacy.cancel": "Cancelled deletion request",
    "ucp.block.add": "Blocked a contact",
    "ucp.block.remove": "Unblocked a contact",
    "ucp.contact.save": "Saved a contact",
    "ucp.contact.delete": "Deleted a contact",
    "ucp.contact.delete_all": "Deleted all contacts",
    "ucp.note.save": "Saved a note",
    "ucp.note.delete": "Deleted a note",
    "ucp.messages.delete": "Deleted messages",
    "ucp.report": "Reported a message",
    "ringgroup.save": "Saved a ring group",
    "routes.blocked_prompt": "Changed blocked-call prompt",
    "logins.guest_change": "Changed guest settings",
    "logins.delete": "Deleted a login",
    "logins.exten_purge": "Extension purged",
    "billing.access.user": "Viewed billing",
    "billing.app_store_mode": "Changed store mode",
    "fax.branding": "Saved fax branding",
    "faxes.delete": "Deleted faxes",
    "cdr.delete": "Deleted call logs",
    "signins.delete": "Deleted sign-ins",
    "signins.clear": "Cleared sign-ins from the panel",
    "voicemail.delete": "Deleted voicemail",
    "email.settings": "Changed email settings",
    "network.settings": "Changed network settings",
    "network.ddns_check": "Checked DDNS",
}


def esc(x):
    return M.esc(x)


def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _mean(code):
    if code in _MEANS:
        return _MEANS[code]
    return (code or "").replace(".", " ").replace("_", " ").title() or code


def _parse(at):
    try:
        return datetime.strptime(at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fmt(at):
    dt = _parse(at)
    if dt is None:
        return at or ""
    if _TZ is not None:
        dt = dt.astimezone(_TZ)
    return dt.strftime("%b %d, %H:%M:%S %Z")


def _login(lid):
    try:
        lid = int(lid)
    except (TypeError, ValueError):
        return None
    with M.db() as c:
        r = c.execute("SELECT id, username, display_name, exten FROM logins WHERE id=?", (lid,)).fetchone()
    return dict(r) if r else None


def _load(username, days=1):
    window = f"-{int(days)} days"
    with M.db() as c:
        au = c.execute(
            "SELECT at, action, detail FROM audit WHERE actor=? AND at >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT ?", (username, window, LIMIT)).fetchall()
        ev = c.execute(
            "SELECT at, kind, ip, jail, detail FROM security_events WHERE actor=? AND at >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT ?", (username, window, LIMIT)).fetchall()
        # Every sign-in, including ones cleared from the dashboard list
        # (clearing only hides them there; they're kept 90 days).
        si_all = c.execute(
            "SELECT at, ip, ok FROM signins WHERE login=? AND at >= datetime('now', ?) "
            "ORDER BY at DESC LIMIT ?", (username, window, LIMIT)).fetchall()
        si = [r for r in si_all if r["ok"]]
    sec = getattr(M, "security", None)
    kinds = getattr(sec, "KINDS", {}) if sec is not None else {}
    jails = getattr(sec, "JAIL_NAMES", {}) if sec is not None else {}
    signins = []
    for r in si:
        dt = _parse(r["at"])
        if dt is not None and r["ip"]:
            signins.append((dt, r["ip"]))

    def _signin_ip(at):
        dt = _parse(at)
        if dt is None:
            return ""
        best = None
        for sdt, ip in signins:
            diff = (sdt - dt).total_seconds()
            if -600 <= diff <= 120 and (best is None or abs(diff) < abs(best[0])):
                best = (diff, ip)
        return best[1] if best else ""

    rows = []
    for r in au:
        a = r["action"] or ""
        ip = _signin_ip(r["at"]) if a in ("app.signin", "app.pair_code") else ""
        rows.append({"at": r["at"], "code": a, "title": _mean(a),
                     "detail": r["detail"] or "", "ip": ip})
    for r in ev:
        k = r["kind"] or ""
        detail = " · ".join(x for x in ((jails.get(r["jail"], r["jail"]) if r["jail"] else ""),
                                        r["detail"] or "") if x)
        rows.append({"at": r["at"], "code": k, "title": kinds.get(k, k),
                     "detail": detail, "ip": r["ip"] or ""})
    for r in si_all:
        rows.append({"at": r["at"], "code": "signin" if r["ok"] else "signin.failed",
                     "title": "Signed in (web panel)" if r["ok"] else "Failed sign-in (wrong password)",
                     "detail": "", "ip": r["ip"] or ""})
    rows.sort(key=lambda x: x["at"] or "", reverse=True)
    return rows[:LIMIT]


def _summary(rows):
    counts = {}
    first = {}
    for i, r in enumerate(rows):
        t = r["title"]
        if t not in counts:
            counts[t] = 0
            first[t] = i
        counts[t] += 1
    titles = sorted(counts, key=lambda t: (-counts[t], first[t]))
    parts = []
    for j, t in enumerate(titles):
        n = counts[t]
        label = t if j == 0 else (t[:1].lower() + t[1:] if t else t)
        parts.append(f"{label} {n} time" + ("s" if n != 1 else ""))
    return "; ".join(parts) + "." if parts else ""


def _page_rows(rows, days=1):
    out = []
    for r in rows:
        out.append(
            f"<tr><td class='muted' style='white-space:nowrap'>{esc(_fmt(r['at']))}</td>"
            f"<td>{esc(r['title'])}<span class='muted' style='display:block;font-size:12.5px'>{esc(r['code'])}</span></td>"
            f"<td style='font-family:monospace;font-size:13.5px;word-break:break-word'>{esc(r['detail'])}</td>"
            f"<td class='muted'>{esc(r['ip']) if r['ip'] else '—'}</td></tr>")
    return "".join(out) or f"<tr><td colspan='4' class='muted'>No activity in the {RANGES[days]}.</td></tr>"


def activity_page(request: Request, lid: str):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    user = _login(lid)
    if not user:
        return RedirectResponse("/logins")
    days = _days(request)
    rows = _load(user["username"], days)
    who = esc(user["display_name"] or user["username"])
    ext = esc(user["exten"] or "-")
    summary = _summary(rows)
    picks = " ".join(
        f"<a class='btn{'' if d == days else ' ghost'}' href='/logins/{user['id']}/activity?days={d}'>{esc(t.replace('last ', '').capitalize())}</a>"
        for d, t in RANGES.items())
    summary_html = ("<p style='background:#1E293B;border:1px solid #334155;border-left:4px solid #FF8A3D;"
                    "border-radius:8px;padding:12px 16px;line-height:1.6'>"
                    f"What {who} did: {esc(summary)}</p>") if summary else ""
    body = f"""<h2>Activity: {who} <span class='muted' style='font-weight:400'>({esc(user['username'])} · ext {ext})</span></h2>
<p>{picks}</p>
<p><a class='btn' href='/logins/{user['id']}/activity/download?days={days}'>Download report ({RANGES[days]})</a> <a class='btn ghost' href='/logins'>Back to Logins</a></p>
<p><span class='pill'>{len(rows)} events</span> <span class='pill'>{RANGES[days]}</span></p>
{summary_html}
<p class='muted'>Sign-in IPs come from the panel sign-in log; other change rows do not record an IP. Records are kept 90 days, then deleted automatically.</p>
<table><tr><th>When</th><th>What happened</th><th>Details</th><th>IP</th></tr>{_page_rows(rows, days)}</table>"""
    return HTMLResponse(M.page("User activity", body, s["username"], s["role"], "logins"))


def _standalone_html(user, rows, days=1):
    span = RANGES[days]
    who = esc(user["display_name"] or user["username"])
    uname = esc(user["username"])
    ext = esc(user["exten"] or "-")
    summary = _summary(rows)
    summary_div = f'<div class="summary">What {who} did: {esc(summary)}</div>' if summary else ""
    trs = []
    for r in rows:
        trs.append(
            f"<tr><td class='when'>{esc(_fmt(r['at']))}</td>"
            f"<td>{esc(r['title'])}<span class='code'>{esc(r['code'])}</span></td>"
            f"<td class='detail'>{esc(r['detail'])}</td>"
            f"<td class='when'>{esc(r['ip']) if r['ip'] else '—'}</td></tr>")
    rows_html = "".join(trs) or f"<tr><td colspan='4' class='muted'>No activity in the {span}.</td></tr>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Activity — {uname} — own-pbx</title>
<style>
  :root {{ --navy: #0F172A; --panel: #1E293B; --line: #334155; --orange: #FF8A3D; --text: #E2E8F0; --muted: #94A3B8; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--navy); color: var(--text); font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
  header {{ padding: 28px 24px 8px; max-width: 1060px; margin: 0 auto; }}
  h1 {{ margin: 0; font-size: 28px; }}
  h1 span {{ color: var(--orange); }}
  .sub {{ color: var(--muted); margin: 8px 0 0; line-height: 1.5; }}
  .stats {{ display: flex; flex-wrap: wrap; gap: 10px; max-width: 1060px; margin: 18px auto 0; padding: 0 24px; }}
  .pill {{ background: var(--panel); border: 1px solid var(--line); border-radius: 999px; padding: 7px 14px; font-size: 14px; }}
  .pill b {{ color: var(--orange); }}
  .summary {{ max-width: 1060px; margin: 18px auto 0; padding: 16px 20px; background: var(--panel); border: 1px solid var(--line); border-left: 4px solid var(--orange); border-radius: 8px; line-height: 1.6; }}
  main {{ max-width: 1060px; margin: 18px auto 0; padding: 0 24px 32px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--panel); border-radius: 8px; overflow: hidden; }}
  th {{ text-align: left; padding: 12px 14px; background: #0B1220; color: var(--muted); font-size: 13px; text-transform: uppercase; letter-spacing: 0.04em; }}
  td {{ padding: 11px 14px; border-top: 1px solid var(--line); vertical-align: top; font-size: 15px; }}
  tr:nth-child(even) td {{ background: rgba(255,255,255,0.02); }}
  .when {{ white-space: nowrap; color: var(--muted); font-variant-numeric: tabular-nums; }}
  .code {{ display: block; color: var(--muted); font-size: 12.5px; margin-top: 2px; }}
  .detail {{ font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 13.5px; line-height: 1.5; word-break: break-word; }}
  .muted {{ color: var(--muted); }}
  footer {{ max-width: 1060px; margin: 0 auto; padding: 0 24px 32px; color: var(--muted); font-size: 13.5px; line-height: 1.6; }}
</style>
</head>
<body>
<header>
  <h1>Activity <span>— {who}</span></h1>
  <p class="sub">User <b>{uname}</b> · ext {ext} · own-pbx Activity feed, {span}.<br>
  Times converted from stored UTC to America/Los_Angeles.</p>
</header>
<div class="stats">
  <span class="pill"><b>{len(rows)}</b> events</span>
  <span class="pill">{span}</span>
</div>
{summary_div}
<main>
<table>
  <tr><th>When</th><th>What happened</th><th>Details</th><th>IP</th></tr>
  {rows_html}
</table>
</main>
<footer>Exported from the own-pbx admin panel (Logins → Activity → Download report). Security-event rows carry their own IPs; sign-in rows show the IP from the panel sign-in log; other change rows do not record an IP.</footer>
</body>
</html>"""


def activity_download(request: Request, lid: str):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    user = _login(lid)
    if not user:
        return RedirectResponse("/logins")
    days = _days(request)
    rows = _load(user["username"], days)
    fname = re.sub(r"[^A-Za-z0-9_.-]", "_", user["username"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(content=_standalone_html(user, rows, days), media_type="text/html",
                    headers={"Content-Disposition": f'attachment; filename="activity-{fname}-{days}d-{stamp}.html"'})


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.get("/logins/{lid}/activity", response_class=HTMLResponse)(activity_page)
    app.get("/logins/{lid}/activity/download")(activity_download)
