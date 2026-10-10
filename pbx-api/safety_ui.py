# SPDX-License-Identifier: GPL-2.0-or-later
"""User safety tools: block list, reporting calls/texts, text on/off switches.

User side (My Phone):
  POST /ucp/block            add / remove a blocked extension or number
  POST /ucp/settings/messaging  receive texts on/off, send texts on/off
  POST /ucp/report           report a call or a text to the admin
Admin side:
  GET  /reports              reports from users (open / closed)
  POST /reports/{id}         close / reopen / delete

Blocking and reporting keep working while the safety lock is on (they
protect users); the on/off switches are settings and follow the lock.
Enforcement for calls and phone texts is in pbx-brain (blocked_by,
agi_server msg-route); texts sent from My Phone are checked in ucp.msg_send.
"""
import time
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import security

M = None
MAX_BLOCKS = 200
_report_times: dict[int, list[float]] = {}


def esc(x):
    return M.esc(x)


def num_norm(v):
    d = "".join(ch for ch in str(v or "") if ch.isdigit())
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def blocked_list(login_id):
    with M.db() as c:
        return [r[0] for r in c.execute("SELECT number FROM blocked_numbers WHERE login_id=? ORDER BY created_at DESC",
                                        (login_id,))]


def is_blocked(login_id, number):
    n = num_norm(number)
    if not n:
        return False
    with M.db() as c:
        return c.execute("SELECT 1 FROM blocked_numbers WHERE login_id=? AND number=?", (login_id, n)).fetchone() is not None


def msg_prefs(login_id):
    with M.db() as c:
        try:
            r = c.execute("SELECT msg_in, msg_out FROM user_prefs WHERE login_id=?", (login_id,)).fetchone()
        except Exception:
            r = None
    return {"msg_in": 1 if r is None else int(r["msg_in"]), "msg_out": 1 if r is None else int(r["msg_out"])}


def _names():
    with M.db() as c:
        return {r["exten"]: (r["display_name"] or r["username"]) for r in
                c.execute("SELECT exten, display_name, username FROM logins WHERE exten != ''")}


# ---------------------------------------------------------------- UI pieces (used by ucp.py)

def settings_section(me, csrf):
    p = msg_prefs(me["id"])
    names = _names()
    blocked = blocked_list(me["id"])
    rows = "".join(
        f'<tr><td>{esc(n)}{(" <span class=muted>" + esc(names[n]) + "</span>") if n in names else ""}</td>'
        f'<td><form method="post" action="/ucp/block" class="inline">{csrf}<input type="hidden" name="number" value="{esc(n)}">'
        f'<input type="hidden" name="action" value="remove"><input type="hidden" name="back" value="settings">'
        f'<button class="link-btn">Unblock</button></form></td></tr>' for n in blocked) \
        or '<tr><td colspan="2" class="muted">Nobody blocked.</td></tr>'
    return f"""<section class="panel">
<h3>Texts &amp; blocking</h3>
<form method="post" action="/ucp/settings/messaging">{csrf}
<label class="switch"><input type="checkbox" name="msg_in" value="1" {"checked" if p["msg_in"] else ""}> <span>Receive text messages</span></label>
<label class="switch"><input type="checkbox" name="msg_out" value="1" {"checked" if p["msg_out"] else ""}> <span>Send text messages</span></label>
<button class="btn">Save</button></form>
<h3 style="margin-top:16px">Blocked</h3>
<p class="muted">Blocked extensions or numbers can't call or text you.</p>
<form method="post" action="/ucp/block">{csrf}<input type="hidden" name="action" value="add"><input type="hidden" name="back" value="settings">
<input name="number" placeholder="Extension or phone number" inputmode="tel" required> <button class="btn ghost">Block</button></form>
<table>{rows}</table>
</section>"""


def block_button(csrf, number, back, blocked):
    if not num_norm(number):
        return ""
    act, label = ("remove", "Unblock") if blocked else ("add", "Block")
    confirm = "" if blocked else f' onsubmit="return confirm(\'Block {esc(number)}? They won\\\'t be able to call or text you.\')"'
    return (f'<form method="post" action="/ucp/block" class="inline"{confirm}>{csrf}'
            f'<input type="hidden" name="number" value="{esc(number)}"><input type="hidden" name="action" value="{act}">'
            f'<input type="hidden" name="back" value="{esc(back)}"><button class="link-btn">{label}</button></form>')


def report_button(csrf, kind, ref_id, back):
    return (f'<form method="post" action="/ucp/report" class="inline" '
            f'onsubmit="var n=prompt(\'Report this {kind} to the administrator. Add a note (optional):\',\'\');'
            f'if(n===null)return false;this.note.value=n;return true;">{csrf}'
            f'<input type="hidden" name="kind" value="{kind}"><input type="hidden" name="ref" value="{int(ref_id)}">'
            f'<input type="hidden" name="note" value=""><input type="hidden" name="back" value="{esc(back)}">'
            f'<button class="link-btn bad">Report</button></form>')


# ---------------------------------------------------------------- user actions

def _safe_back(v):
    v = v or ""
    if v == "settings":
        return "/ucp/settings"
    if v == "calls":
        return "/ucp/calls"
    if v.startswith("messages"):
        w = v.split(":", 1)[1] if ":" in v else ""
        return "/ucp/messages" + (f"?with={urllib.parse.quote(w)}" if w else "")
    return "/ucp"


def _go(back, key):
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}msg={key}", status_code=303)


async def _user(request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    return s, me


async def block(request: Request):
    s, me = await _user(request)
    f = await request.form()
    back = _safe_back(f.get("back"))
    n = num_norm(f.get("number"))
    if not n or len(n) > 15:
        return _go(back, "blockbad")
    if n == num_norm(me["exten"]):
        return _go(back, "blockself")
    with M.db() as c:
        if f.get("action") == "remove":
            c.execute("DELETE FROM blocked_numbers WHERE login_id=? AND number=?", (me["id"], n))
            key = "unblocked"
        else:
            if c.execute("SELECT COUNT(*) FROM blocked_numbers WHERE login_id=?", (me["id"],)).fetchone()[0] >= MAX_BLOCKS:
                return _go(back, "blockmax")
            c.execute("INSERT OR IGNORE INTO blocked_numbers (login_id, number) VALUES (?,?)", (me["id"], n))
            key = "blocked"
        c.commit()
    M.ucp._audit(me, "ucp.block." + ("remove" if key == "unblocked" else "add"), n)
    return _go(back, key)


async def save_messaging(request: Request):
    s, me = await _user(request)
    if M.ucp._locked():
        return _go("/ucp/settings", "locked")
    f = await request.form()
    mi, mo = (1 if f.get("msg_in") else 0), (1 if f.get("msg_out") else 0)
    with M.db() as c:
        c.execute("INSERT INTO user_prefs (login_id, msg_in, msg_out) VALUES (?,?,?) ON CONFLICT(login_id) DO UPDATE"
                  " SET msg_in=excluded.msg_in, msg_out=excluded.msg_out", (me["id"], mi, mo))
        c.commit()
    M.ucp._audit(me, "ucp.messaging", f"in={mi} out={mo}")
    return _go("/ucp/settings", "saved")


async def report(request: Request):
    s, me = await _user(request)
    f = await request.form()
    back = _safe_back(f.get("back"))
    kind = f.get("kind")
    try:
        ref = int(f.get("ref") or 0)
    except ValueError:
        ref = 0
    note = "".join(ch for ch in (f.get("note") or "") if ch.isprintable())[:500]
    now = time.time()
    recent = [t for t in _report_times.get(me["id"], []) if now - t < 3600]
    if len(recent) >= 20:
        return _go(back, "reportrate")
    other, detail = "", ""
    if kind == "message":
        m = next((x for x in M.ucp._my_messages(me, limit=100000) if x["id"] == ref), None)
        if not m:
            return _go(back, "reportbad")
        other = m["to_ext"] if m["from_ext"] == me["exten"] else m["from_ext"]
        detail = f'text from {m["from_ext"]} to {m["to_ext"]} at {m["sent_at"]}: {m["body"][:500]}'
    elif kind == "call":
        cr = next((x for x in M.ucp._my_calls(me, limit=2000) if x["id"] == ref), None)
        if not cr:
            return _go(back, "reportbad")
        other = cr["other"]
        detail = (f'{cr["kind"]} call {cr["src"]} -> {cr["dst"]} at {cr["started_at"]}, '
                  f'{cr["bill_sec"]}s, {cr["disposition"]}')
    else:
        return _go(back, "reportbad")
    _report_times[me["id"]] = recent + [now]
    with M.db() as c:
        c.execute("INSERT INTO reports (reporter_id, kind, ref_id, other, detail, note) VALUES (?,?,?,?,?,?)",
                  (me["id"], kind, ref, str(other)[:40], detail, note))
        c.commit()
    security.record("report", "", f"{me['username']} (ext {me['exten']}) reported a {kind} with {other}"
                    + (f' - "{note}"' if note else "") + f". {detail[:200]}", actor=me["username"])
    M.ucp._audit(me, "ucp.report", f"{kind} {ref} other={other}")
    return _go(back, "reported")


# ---------------------------------------------------------------- admin

def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def reports_page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    show = request.query_params.get("show", "open")
    csrf = M._csrf_field(s)
    names = _names()
    with M.db() as c:
        rows = c.execute(
            "SELECT r.*, l.username AS reporter, l.exten AS reporter_ext FROM reports r"
            " LEFT JOIN logins l ON l.id=r.reporter_id" + ("" if show == "all" else " WHERE r.status='open'")
            + " ORDER BY r.id DESC LIMIT 500").fetchall()
        n_open = c.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0]

    def actions(r):
        out = ""
        for act, label in ((("close", "Mark resolved"),) if r["status"] == "open" else (("reopen", "Reopen"),)) + (("delete", "Delete"),):
            out += (f'<form method="post" action="/reports/{r["id"]}" class="inline">{csrf}'
                    f'<input type="hidden" name="action" value="{act}"><button class="link-btn">{label}</button></form> ')
        return out
    tr = "".join(
        f"<tr><td class='muted'>{esc(r['at'])}</td><td>{esc(r['kind'])}</td>"
        f"<td>{esc(r['reporter'] or '-')} <span class='muted'>{esc(r['reporter_ext'] or '')}</span></td>"
        f"<td><b>{esc(r['other'])}</b>{(' <span class=muted>' + esc(names[r['other']]) + '</span>') if r['other'] in names else ''}</td>"
        f"<td>{esc(r['note']) or '<span class=muted>-</span>'}</td><td>{esc(r['detail'])}</td>"
        f"<td>{'<span class=\"pill warn\">open</span>' if r['status'] == 'open' else '<span class=\"pill\">resolved</span>'}</td>"
        f"<td>{actions(r)}</td></tr>" for r in rows) \
        or '<tr><td colspan="8" class="muted">No reports.</td></tr>'
    body = f"""<h2>Reports <span class="muted" style="font-weight:400">{n_open} open</span></h2>
<p class="muted">Calls and texts users reported from My Phone. To stop someone, disable their login on the
<a href="/logins">Logins</a> page; users can also block numbers themselves.</p>
<div class="filters"><a href="/reports" class="{'on' if show != 'all' else ''}">Open</a>
<a href="/reports?show=all" class="{'on' if show == 'all' else ''}">All</a></div>
<div class="scrollbox"><table><tr><th>When</th><th>Type</th><th>Reported by</th><th>About</th><th>Note</th><th>Details</th><th>Status</th><th></th></tr>{tr}</table></div>"""
    return HTMLResponse(M.page("Reports", body, s["username"], s["role"], "reports"))


async def report_action(request: Request, rid: int):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "reports")
    f = await request.form()
    act = f.get("action")
    with M.db() as c:
        if act == "close":
            c.execute("UPDATE reports SET status='closed' WHERE id=?", (rid,))
        elif act == "reopen":
            c.execute("UPDATE reports SET status='open' WHERE id=?", (rid,))
        elif act == "delete":
            c.execute("DELETE FROM reports WHERE id=?", (rid,))
        c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)", (s["username"], "report." + str(act), str(rid)))
        c.commit()
    return RedirectResponse("/reports", status_code=303)


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.post("/ucp/block")(block)
    app.post("/ucp/settings/messaging")(save_messaging)
    app.post("/ucp/report")(report)
    app.get("/reports", response_class=HTMLResponse)(reports_page)
    app.post("/reports/{rid}")(report_action)
