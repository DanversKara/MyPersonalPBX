# SPDX-License-Identifier: GPL-2.0-or-later
"""User safety tools: block list, reporting calls/texts, text on/off switches.

User side (My Phone):
  POST /ucp/block            add / remove a blocked extension or number, or
                             change what it gets (call filtering: "not in
                             service", straight to voicemail, or forward)
  POST /ucp/settings/messaging  receive texts on/off, send texts on/off
  POST /ucp/report           report a call or a text to the admin
Admin side:
  GET  /reports              reports from users (open / closed)
  POST /reports/{id}         close / reopen / delete

Blocking and reporting keep working while the safety lock is on (they
protect users); the on/off switches are settings and follow the lock.
Enforcement for calls and phone texts is in pbx-brain (block_rule /
apply_block, agi_server msg-route); texts sent from My Phone are checked in
ucp.msg_send. Users can also block a caller mid-call with *86 (pbx-brain
midcall_block); it adds a row here with the default "not in service".
"""
import time
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import security

M = None
MAX_BLOCKS = 200
# Call filtering: what a blocked caller gets (pbx-brain apply_block).
BLOCK_ACTIONS = (("disconnected", "Number disconnected (\"not in service\")"),
                 ("voicemail", "Straight to my voicemail"),
                 ("forward", "Forward to another number"),
                 ("ring_novm", "Ring me, but no voicemail (hang up if I don't answer)"),
                 ("vip", "Always ring me, even on Do Not Disturb"))
_report_times: dict[int, list[float]] = {}


def esc(x):
    return M.esc(x)


try:
    from phone import norm_number as num_norm
except ImportError:  # app.py puts ../shared on sys.path; standalone tests may not
    def num_norm(v):
        d = "".join(ch for ch in str(v or "") if ch.isdigit())
        return d[1:] if len(d) == 11 and d.startswith("1") else d


def blocked_list(login_id):
    with M.db() as c:
        return [r[0] for r in c.execute("SELECT number FROM blocked_numbers WHERE login_id=?"
                                        " AND action NOT IN ('ring_novm', 'vip') ORDER BY created_at DESC",
                                        (login_id,))]


def block_rules(login_id):
    """[{number, action, target, created_at}] for this user's block list."""
    with M.db() as c:
        rows = c.execute("SELECT * FROM blocked_numbers WHERE login_id=? ORDER BY created_at DESC",
                         (login_id,)).fetchall()
    return [{"number": r["number"], "created_at": r["created_at"],
             "action": (dict(r).get("action") or "disconnected"),
             "target": (dict(r).get("target") or "")} for r in rows]


def is_blocked(login_id, number):
    n = num_norm(number)
    if not n:
        return False
    with M.db() as c:
        return c.execute("SELECT 1 FROM blocked_numbers WHERE login_id=? AND number=?"
                         " AND action NOT IN ('ring_novm', 'vip')", (login_id, n)).fetchone() is not None


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

def _mode_fields(mode, target):
    """The "what they get" select + forward-to box for one filter rule."""
    opts = "".join(f'<option value="{v}" {"selected" if v == mode else ""}>{esc(label)}</option>'
                   for v, label in BLOCK_ACTIONS)
    hide = "" if mode == "forward" else ' style="display:none"'
    return (f'<select name="mode" onchange="this.nextElementSibling.style.display='
            f'this.value==\'forward\'?\'\':\'none\'">{opts}</select>'
            f'<input name="target" value="{esc(target)}" placeholder="Extension or phone number"'
            f' inputmode="tel" maxlength="20"{hide}>')


def settings_section(me, csrf):
    p = msg_prefs(me["id"])
    names = _names()
    rules = block_rules(me["id"])
    rows = "".join(
        f'<tr><td>{esc(r["number"])}{(" <span class=muted>" + esc(names[r["number"]]) + "</span>") if r["number"] in names else ""}'
        f'<br><span class="muted">since {esc(r["created_at"])}</span></td>'
        f'<td><form method="post" action="/ucp/block" class="inline">{csrf}<input type="hidden" name="number" value="{esc(r["number"])}">'
        f'<input type="hidden" name="action" value="update"><input type="hidden" name="back" value="settings">'
        f'{_mode_fields(r["action"], r["target"])} <button class="btn ghost">Save</button></form></td>'
        f'<td><form method="post" action="/ucp/block" class="inline">{csrf}<input type="hidden" name="number" value="{esc(r["number"])}">'
        f'<input type="hidden" name="action" value="remove"><input type="hidden" name="back" value="settings">'
        f'<button class="link-btn">Remove</button></form></td></tr>' for r in rules) \
        or '<tr><td colspan="3" class="muted">No numbers yet.</td></tr>'
    return f"""<section class="panel">
<h3>Texts &amp; blocking</h3>
<form method="post" action="/ucp/settings/messaging">{csrf}
<label class="switch"><input type="checkbox" name="msg_in" value="1" {"checked" if p["msg_in"] else ""}> <span>Receive text messages</span></label>
<label class="switch"><input type="checkbox" name="msg_out" value="1" {"checked" if p["msg_out"] else ""}> <span>Send text messages</span></label>
<button class="btn">Save</button></form>
<h3 style="margin-top:16px">Call filtering</h3>
<p class="muted">Decide what happens when a particular extension or number calls you:
<b>"not in service"</b>, <b>straight to your voicemail</b>, or <b>forwarded</b> to another extension or
number (these also stop their texts); or let them <b>ring you but not leave a voicemail</b>, or
<b>always ring you</b>, even when Do Not Disturb is on. Only you can see your list.</p>
<p class="muted">During a call someone made to you, press <b>*86</b> on your phone to hang up on them
and block them right away (they hear "not in service"). Only your own phone can do this; the caller
pressing the same keys does nothing.</p>
<form method="post" action="/ucp/block">{csrf}<input type="hidden" name="action" value="add"><input type="hidden" name="back" value="settings">
<input name="number" placeholder="Extension or phone number" inputmode="tel" required>
{_mode_fields("disconnected", "")} <button class="btn ghost">Add</button></form>
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
    act = f.get("action")
    mode = f.get("mode") or "disconnected"
    if mode not in {v for v, _ in BLOCK_ACTIONS}:
        mode = "disconnected"
    target = ""
    if mode == "forward" and act != "remove":
        target = num_norm(f.get("target"))
        if not target or len(target) > 15:
            return _go(back, "blockfwdbad")
        if target == n:
            return _go(back, "blockfwdloop")
    with M.db() as c:
        if act == "remove":
            c.execute("DELETE FROM blocked_numbers WHERE login_id=? AND number=?", (me["id"], n))
            key = "unblocked"
        elif act == "update":
            cur = c.execute("UPDATE blocked_numbers SET action=?, target=? WHERE login_id=? AND number=?",
                            (mode, target, me["id"], n))
            if not cur.rowcount:
                return _go(back, "blockbad")
            key = "blockupdated"
        else:
            exists = c.execute("SELECT 1 FROM blocked_numbers WHERE login_id=? AND number=?", (me["id"], n)).fetchone()
            if not exists and c.execute("SELECT COUNT(*) FROM blocked_numbers WHERE login_id=?",
                                        (me["id"],)).fetchone()[0] >= MAX_BLOCKS:
                return _go(back, "blockmax")
            c.execute("INSERT INTO blocked_numbers (login_id, number, action, target) VALUES (?,?,?,?)"
                      " ON CONFLICT(login_id, number) DO UPDATE SET action=excluded.action, target=excluded.target",
                      (me["id"], n, mode, target))
            key = "blocked"
        c.commit()
    M.ucp._audit(me, "ucp.block." + {"unblocked": "remove", "blockupdated": "update"}.get(key, "add"),
                 n + ("" if act == "remove" else f" {mode}{(' ' + target) if target else ''}"))
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
