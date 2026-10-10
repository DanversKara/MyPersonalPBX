# SPDX-License-Identifier: GPL-2.0-or-later
"""User Control Panel (/ucp): each user's self-service portal.

Overview, call history, voicemail, recordings, messages and settings
(do-not-disturb, call forwarding, call recording, voicemail email,
password, phone setup / SIP password rotation).

Installed onto the main FastAPI app by app.py (`ucp.install(app_module)`),
reusing its DB, session, CSRF and config-apply helpers.

Security rules:
- Every page acts only on the signed-in login (session id), never on an id
  taken from the request.
- Voicemail: only messages in the user's own mailbox.
- Recordings: only "user"-system recordings of calls the user was on.
  Admin-system recordings are never visible here.
- Every interpolated value goes through esc().
- Every POST checks the CSRF token and the safety lock.
"""
import json
import os
import urllib.parse
import re
import secrets
import time
import voipms_sms

import bcrypt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from panel_templates import fmt_ts

M = None  # the app module, set by install()

VM_ROOT = "/var/spool/pbx/voicemail"
VMGREET_DIR = os.environ.get("PBX_VMGREET_DIR", "/var/lib/pbx/vmgreet")
VM_GREETING_MAX_SEC = 120
REC_ROOT = "/var/spool/pbx/monitor"
FWD_RE = re.compile(r"^\+?[0-9*#]{2,20}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")

# Fixed flash messages (keys in the query string, never reflected text).
FLASH = {
    "saved": ("ok", "Saved."),
    "pw": ("ok", "Password changed."),
    "vmdel": ("ok", "Voicemail deleted."),
    "recdel": ("ok", "Recording deleted."),
    "locked": ("bad", "Changes are disabled right now (the administrator has the safety lock on)."),
    "pwbad": ("bad", "Current password is wrong."),
    "pwshort": ("bad", "New password must be at least 8 characters."),
    "pwmatch": ("bad", "New passwords don't match."),
    "fwdbad": ("bad", "Forward targets must be an extension or phone number (digits, optional leading +)."),
    "fwdself": ("bad", "You can't forward calls to your own extension."),
    "emailbad": ("bad", "That email address doesn't look valid."),
    "noext": ("bad", "Your login has no extension, so there's nothing to set here."),
    "ringbad": ("bad", "Pick a ring time from the list."),
    "greetsaved": ("ok", "Voicemail greeting saved. Callers hear it from the next call."),
    "greetremoved": ("ok", "Greeting removed. Callers hear the standard prompt."),
    "greetnone": ("bad", "Choose an audio file to upload."),
    "deleted": ("ok", "Deleted."),
    "msgsent": ("ok", "Message sent."),
    "msgoff": ("bad", "Sending texts is turned off in your Settings."),
    "msgnotaccepting": ("bad", "That person isn't accepting text messages."),
    "blocked": ("ok", "Blocked. They can't call or text you."),
    "unblocked": ("ok", "Unblocked."),
    "blockbad": ("bad", "Enter an extension or phone number."),
    "blockself": ("bad", "You can't block yourself."),
    "blockmax": ("bad", "Your block list is full - remove some first."),
    "reported": ("ok", "Reported. The administrator has been notified."),
    "reportbad": ("bad", "Couldn't find that call or message."),
    "reportrate": ("bad", "Too many reports in an hour - try again later."),
    "msgempty": ("bad", "Type a message first."),
    "msgto": ("bad", "That isn't another user's extension number."),
    "msgquota": ("bad", "You have no texts left on your plan this month."),
    "msgrate": ("bad", "Too many messages in a minute - wait a moment."),
    "msgnoext": ("bad", "Your login has no extension, so it can't send texts."),
    "recack": ("bad", "To turn on call recording, tick the box to confirm you've read the recording-law notice."),
    "recplan": ("bad", "Call recording isn't included in your plan."),
    "ivrbad": ("bad", "Pick one of your own IVR menus."),
}

SUBTABS = [("ucp", "/ucp", "Overview"), ("ucp-calls", "/ucp/calls", "Calls"),
           ("ucp-vm", "/ucp/voicemail", "Voicemail"),
           ("ucp-rec", "/ucp/recordings", "Recordings"),
           ("ucp-msg", "/ucp/messages", "Messages"),
           ("ucp-ivr", "/ucp/ivr", "IVR"),
           ("ucp-bill", "/ucp/billing", "Billing"),
           ("ucp-set", "/ucp/settings", "Settings")]
RING_CHOICES = (10, 15, 20, 25, 30, 45, 60, 90, 120)   # mirrors pbx-brain
DEFAULT_RING = 30


# ---------------------------------------------------------------- helpers

def esc(x):
    return M.esc(x)


def _me(request: Request):
    """(session, login row dict) or (None, None)."""
    s = M._sess(request)
    if not s:
        return None, None
    with M.db() as c:
        row = c.execute("SELECT * FROM logins WHERE id=? AND enabled=1",
                        (s["id"],)).fetchone()
    return (s, dict(row)) if row else (None, None)


def _prefs(login_id):
    with M.db() as c:
        r = c.execute("SELECT * FROM user_prefs WHERE login_id=?",
                      (login_id,)).fetchone()
    d = dict(r) if r else {}
    d.setdefault("dnd", 0)
    d.setdefault("forward_always", "")
    d.setdefault("forward_noanswer", "")
    d["ring_seconds"] = d.get("ring_seconds") or 0
    d.setdefault("answer_ivr_id", None)
    d["text_email"] = 1 if d.get("text_email", 1) is None else int(d.get("text_email", 1))
    return d


def _mailbox(login_id):
    with M.db() as c:
        r = c.execute("SELECT mailbox FROM voicemail_boxes WHERE login_id=?",
                      (login_id,)).fetchone()
    return r["mailbox"] if r else None


def _ensure_mailbox(me):
    """Mailbox name for a login, created if missing (same naming as pbx-brain)."""
    box = _mailbox(me["id"])
    if box:
        return box
    base = "".join(ch for ch in me["username"] if ch.isalnum())[:32] or f"box{me['id']}"
    box, i = base, 1
    with M.db() as c:
        while c.execute("SELECT 1 FROM voicemail_boxes WHERE mailbox=?", (box,)).fetchone():
            i += 1
            box = f"{base}{i}"
        c.execute("INSERT INTO voicemail_boxes (login_id, mailbox) VALUES (?,?)", (me["id"], box))
        c.commit()
    return box


def _greeting_path(login_id):
    with M.db() as c:
        r = c.execute("SELECT greeting_path FROM voicemail_boxes WHERE login_id=?", (login_id,)).fetchone()
    p = r[0] if r else ""
    return p if p and os.path.isfile(p) else ""


def _bulk_js(form_id):
    """Select-all / count / enable for checkboxes that belong to form_id."""
    return f"""<script>(function(){{
const f = '{form_id}', all = document.getElementById(f + '-all'), btn = document.getElementById(f + '-sel');
const boxes = () => [...document.querySelectorAll('input.sel[form="' + f + '"]')];
function sync() {{ const n = boxes().filter(b => b.checked).length;
  if (btn) {{ btn.disabled = !n; btn.textContent = n ? 'Delete selected (' + n + ')' : 'Delete selected'; }}
  if (all) {{ all.checked = n > 0 && n === boxes().length; all.indeterminate = n > 0 && n < boxes().length; }} }}
if (all) all.addEventListener('change', () => {{ boxes().forEach(b => b.checked = all.checked); sync(); }});
boxes().forEach(b => b.addEventListener('change', sync)); sync();
}})();</script>"""


def _bulkbar(s, form_id, action, total, what, extra_hidden=""):
    """Delete selected / Delete all bar (a form the row checkboxes join)."""
    return (f'<form id="{form_id}" method="post" action="{action}" class="bulkbar">{M._csrf_field(s)}{extra_hidden}'
            f'<button class="btn ghost" id="{form_id}-sel" name="mode" value="selected" disabled '
            f'onclick="return confirm(\'Delete the selected {what}?\')">Delete selected</button>'
            f'<button class="btn ghost danger" name="mode" value="all" {"disabled" if not total else ""} '
            f'onclick="return confirm(\'Delete all {total} {what}? This cannot be undone.\')">Delete all</button></form>')


def _quota(me, feature):
    """Plan allowance for a feature (-1 unlimited). Admins: unlimited."""
    import entitlements as ent
    with M.db() as c:
        if ent.is_admin(c, me["id"]):
            return ent.UNLIMITED
        return ent.quota(c, me["id"], feature)


def _left(me, feature):
    """Remaining this month for monthly features (None = unlimited)."""
    import entitlements as ent
    q = _quota(me, feature)
    if q == ent.UNLIMITED:
        return None
    with M.db() as c:
        return max(0, q - ent.usage(c, me["id"], feature))


def _plan_note(text):
    return (f'<div class="flash warn-note">{esc(text)} <a href="/ucp/billing">See plans</a></div>')


def _smtp_ready():
    with M.db() as c:
        r = dict(c.execute("SELECT key, value FROM kv_settings WHERE key IN ('smtp_host','smtp_from')").fetchall())
    return bool(r.get("smtp_host") and r.get("smtp_from"))


def _consent(me):
    """(accepted_at or None, version) of the recording-law acknowledgement."""
    with M.db() as c:
        r = c.execute("SELECT recording_consent_at, recording_consent_version FROM user_prefs WHERE login_id=?",
                      (me["id"],)).fetchone()
    return (r[0], int(r[1] or 0)) if r else (None, 0)


def _locked():
    # Only the full safety lock freezes My Phone; the admin-only lock leaves it usable.
    return M._ucp_locked()


def _audit(me, action, detail=""):
    try:
        with M.db() as c:
            c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)",
                      (me["username"], action, detail))
            c.commit()
    except Exception:
        pass


def _back(path, msg):
    return RedirectResponse(f"{path}?msg={msg}", status_code=303)


def _flash(request: Request):
    key = request.query_params.get("msg", "")
    if key not in FLASH:
        return ""
    cls, text = FLASH[key]
    return f'<div class="flash {cls}">{esc(text)}</div>'


def _ago(ts, from_utc=True):
    """Stored timestamp -> 12-hour brand-timezone form (e.g. 2026-10-08 7:29 PM)."""
    return fmt_ts(ts, from_utc=from_utc)


def _dur(sec):
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return "-"
    return f"{sec // 60}:{sec % 60:02d}"


def _render(request, s, me, tab, title, body):
    sub = ""
    if s["role"] == "admin":
        # Admins reach /ucp via the "My Phone" admin tab; show the sub-nav.
        sub = '<div class="subtabs">' + "".join(
            f'<a href="{href}" class="{"on" if key == tab else ""}">{esc(label)}</a>'
            for key, href, label in SUBTABS) + "</div>"
    head = (f'<div class="ucp-head"><div><h2>{esc(title)}</h2>'
            f'<div class="muted">{esc(me["display_name"] or me["username"])}'
            f'{" · ext " + esc(me["exten"]) if me["exten"] else ""}</div></div></div>')
    return HTMLResponse(M.page(title, sub + _flash(request) + head + body,
                               s["username"], s["role"], tab))


def _device_status(me):
    """List of this user's registered contacts, or None if unknown."""
    if not me["sip_username"]:
        return []
    try:
        st = M._asterisk_status()
    except Exception:
        return None
    return [ct for ct in st.get("contacts", []) if ct.get("aor") == me["sip_username"]]


def _exten_since(me):
    """When this login got its current extension (UTC), or None = no limit."""
    try:
        return me["exten_since"]
    except (KeyError, IndexError):
        with M.db() as c:
            r = c.execute("SELECT exten_since FROM logins WHERE id=?", (me["id"],)).fetchone()
        return r[0] if r else None


def _my_calls(me, limit=200, missed_only=False):
    ex = me["exten"]
    if not ex:
        return []
    box = _mailbox(me["id"])
    since = _exten_since(me)
    q = ("SELECT * FROM cdr WHERE (src=? OR dst=? OR (','||dst||',') LIKE ?"
         " OR dst=?) AND (login_id=? OR ? IS NULL OR started_at >= datetime(?, 'localtime'))")
    args = [ex, ex, f"%,{ex},%", f"voicemail-{box}" if box else "\x00", me["id"], since, since]
    q += " ORDER BY id DESC LIMIT ?"
    with M.db() as c:
        rows = [dict(r) for r in c.execute(q, args + [limit * 2]).fetchall()]
    out = []
    for r in rows:
        # Call history can't be removed by users (kept 90 days, then purged).
        outgoing = r["src"] == ex
        answered = r["disposition"] == "ANSWERED"
        if outgoing:
            kind, other = "out", r["dst"]
        elif answered:
            kind, other = "in", r["src"]
        else:
            kind, other = "missed", r["src"]
        if str(r["dst"]).startswith("voicemail-") and not outgoing:
            kind = "vm"
        if missed_only and kind not in ("missed", "vm"):
            continue
        r["kind"], r["other"] = kind, other
        out.append(r)
        if len(out) >= limit:
            break
    return out


KIND_LABEL = {"out": ("Outgoing", ""), "in": ("Incoming", "ok"),
              "missed": ("Missed", "bad"), "vm": ("Voicemail", "bad")}


def _recordings_for(me, limit=200):
    """Recordings the user started. Recordings made by other users are never
    listed here, even if this user was on the call."""
    if not me["exten"]:
        return []
    with M.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM recordings WHERE system='user' AND login_id=?"
            " ORDER BY id DESC LIMIT ?",
            (me["id"], limit)).fetchall()]


def can_play_voicemail(s, msg_id) -> bool:
    """Owner check used by the shared audio endpoint."""
    box = _mailbox(s["id"])
    if not box:
        return False
    with M.db() as c:
        r = c.execute("SELECT mailbox FROM voicemail_messages WHERE id=?",
                      (msg_id,)).fetchone()
    return bool(r) and r["mailbox"] == box


def can_play_recording(s, rec_id) -> bool:
    with M.db() as c:
        me = c.execute("SELECT * FROM logins WHERE id=?", (s["id"],)).fetchone()
        r = c.execute("SELECT system, login_id FROM recordings WHERE id=?",
                      (rec_id,)).fetchone()
    if not me or not r or r["system"] != "user":
        return False
    # Only the user who started the recording may play it.
    return r["login_id"] == me["id"]


def purge_extension_data(exts, moving_login_id=None):
    """Privacy clean slate for (re)assigned extensions.

    Deletes every user-data row tied to any of the given extensions, plus
    the audio files on disk:
      - recordings where exten or peer matches (DB + files)
      - cdr where src or dst matches
      - messages where from_ext or to_ext matches (internal + SMS/MMS)
      - voicemail boxes named vm-<ext>, plus the moving login's own box:
        all messages (DB + files) and the greeting file
    message_hidden rows cascade from messages. The logins.exten_since
    trigger already stamps the reassignment time. Returns {table: count}.
    """
    exts = sorted({str(e).strip() for e in (exts or []) if str(e).strip()})
    counts = {"recordings": 0, "cdr": 0, "messages": 0, "voicemail": 0}
    if not exts:
        return counts
    with M.db() as c:
        for e in exts:
            rows = c.execute("SELECT id, path FROM recordings WHERE exten=? OR peer=?",
                             (e, e)).fetchall()
            for r in rows:
                _safe_unlink(r["path"], REC_ROOT)
            counts["recordings"] += len(rows)
            c.execute("DELETE FROM recordings WHERE exten=? OR peer=?", (e, e))

            r = c.execute("DELETE FROM cdr WHERE src=? OR dst=?", (e, e))
            counts["cdr"] += r.rowcount

            r = c.execute("DELETE FROM messages WHERE from_ext=? OR to_ext=?", (e, e))
            counts["messages"] += r.rowcount

        # Voicemail: boxes named vm-<ext>, plus the moving login's own box
        # (its name may still be the old vm-<old ext>).
        boxes = {}
        for r in c.execute(
                "SELECT mailbox, login_id, greeting_path FROM voicemail_boxes WHERE mailbox IN (%s)"
                % ",".join("?" * len(exts)),
                [f"vm-{e}" for e in exts]).fetchall():
            boxes[r["mailbox"]] = r
        if moving_login_id:
            r = c.execute("SELECT mailbox, login_id, greeting_path FROM voicemail_boxes WHERE login_id=?",
                          (moving_login_id,)).fetchone()
            if r:
                boxes[r["mailbox"]] = r
        for box in boxes.values():
            msgs = c.execute("SELECT id, path FROM voicemail_messages WHERE mailbox=?",
                             (box["mailbox"],)).fetchall()
            for m in msgs:
                _safe_unlink(m["path"], VM_ROOT)
            counts["voicemail"] += len(msgs)
            c.execute("DELETE FROM voicemail_messages WHERE mailbox=?", (box["mailbox"],))
            if box["greeting_path"]:
                _safe_unlink(box["greeting_path"], VMGREET_DIR)
                c.execute("UPDATE voicemail_boxes SET greeting_path='' WHERE mailbox=?",
                          (box["mailbox"],))
        c.commit()
    return counts


def _safe_unlink(path, root):
    try:
        if os.path.commonpath([os.path.abspath(path), root]) == root and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


# ---------------------------------------------------------------- pages

def overview(request: Request):
    s, me = _me(request)
    if not s:
        return RedirectResponse("/login")
    p = _prefs(me["id"])
    devs = _device_status(me)
    if devs is None:
        dev_html = '<span class="muted">Status unavailable</span>'
        dev_ok = None
    elif devs:
        dev_ok = True
        dev_html = "".join(
            f'<div><span class="pill ok">Online</span> {esc(d["ip"])}:{esc(d["port"])}'
            f'{" · " + esc(d["transport"]) if d.get("transport") else ""}'
            f'{" · " + format(d["rtt_ms"], ".0f") + " ms" if isinstance(d.get("rtt_ms"), (int, float)) else ""}</div>'
            for d in devs)
    else:
        dev_ok = False
        dev_html = '<span class="pill bad">Offline</span> <span class="muted">No phone signed in with your account</span>'
    box = _mailbox(me["id"])
    new_vm = 0
    if box:
        with M.db() as c:
            new_vm = c.execute("SELECT COUNT(*) FROM voicemail_messages WHERE mailbox=? AND folder='INBOX' AND is_read=0",
                               (box,)).fetchone()[0]
    calls = _my_calls(me, limit=6)
    missed = sum(1 for x in _my_calls(me, limit=50) if x["kind"] in ("missed", "vm"))
    csrf = M._csrf_field(s)
    if p["dnd"]:
        status = '<span class="pill bad">Do not disturb</span> <span class="muted">Calls go straight to voicemail</span>'
    elif p["forward_always"]:
        status = f'<span class="pill warn">Forwarding</span> <span class="muted">All calls go to {esc(p["forward_always"])}</span>'
    else:
        status = '<span class="pill ok">Available</span>'
        if p["answer_ivr_id"]:
            with M.db() as c:
                mn = c.execute("SELECT name FROM ivr_menus WHERE id=? AND owner_login_id=?",
                               (p["answer_ivr_id"], me["id"])).fetchone()
            if mn:
                status += f' <span class="muted">Callers hear your menu "{esc(mn[0])}" first.</span>'
        status += f' <span class="muted">Rings {p["ring_seconds"] or DEFAULT_RING}s.</span>'
        if p["forward_noanswer"]:
            status += f' <span class="muted">Unanswered calls go to {esc(p["forward_noanswer"])}</span>'
    dnd_btn = (f'<form method="post" action="/ucp/dnd" class="inline">{csrf}'
               f'<input type="hidden" name="on" value="{0 if p["dnd"] else 1}">'
               f'<button class="btn {"" if p["dnd"] else "ghost"}">'
               f'{"Turn off do not disturb" if p["dnd"] else "Turn on do not disturb"}</button></form>')
    rows = "".join(
        f'<tr><td><span class="pill {KIND_LABEL[r["kind"]][1]}">{KIND_LABEL[r["kind"]][0]}</span></td>'
        f'<td>{esc(r["other"])}</td><td>{_dur(r["bill_sec"])}</td><td class="muted">{_ago(r["started_at"], from_utc=False)}</td></tr>'
        for r in calls) or '<tr><td colspan="4" class="muted">No calls yet</td></tr>'
    body = f"""
<div class="tiles">
  <div class="tile"><span class="lbl">My phone</span>{dev_html}</div>
  <div class="tile"><span class="lbl">Status</span><div>{status}</div><div style="margin-top:10px">{dnd_btn}</div></div>
  <a class="tile link" href="/ucp/voicemail"><span class="lbl">New voicemail</span><b>{new_vm}</b></a>
  <a class="tile link" href="/ucp/calls?missed=1"><span class="lbl">Missed calls (recent)</span><b>{missed}</b></a>
</div>
<h3>911</h3>
{M.e911_ui.user_notice(me["id"])}
<h3>Your plan this month</h3>
{M.billing.usage_panel(me)}
<h3>Recent calls</h3>
<table><tr><th>Type</th><th>Number</th><th>Length</th><th>When</th></tr>{rows}</table>
<p><a href="/ucp/calls">All calls →</a></p>
"""
    if not me["exten"]:
        body = '<p class="muted">Your login has no extension attached, so there is no phone to manage. Ask the administrator to give you one.</p>'
    return _render(request, s, me, "ucp", "My Phone", body)


def calls_page(request: Request):
    s, me = _me(request)
    if not s:
        return RedirectResponse("/login")
    missed = request.query_params.get("missed") == "1"
    calls = _my_calls(me, limit=200, missed_only=missed)
    recs = {}
    for r in _recordings_for(me, limit=500):
        recs.setdefault(r["call_id"], r)
    csrf = M._csrf_field(s)

    def rec_cell(r):
        rec = recs.get(r["call_id"])
        if not rec:
            return ""
        cell = f'<audio controls preload="none" src="/api/recording-audio/{int(rec["id"])}"></audio>'
        if rec["login_id"] == me["id"]:
            cell += (f'<form method="post" action="/ucp/recordings/delete" class="inline" '
                     f'onsubmit="return confirm(\'Delete this recording? The call stays in your history.\')">{csrf}'
                     f'<input type="hidden" name="ids" value="{int(rec["id"])}"><input type="hidden" name="mode" value="selected">'
                     f'<input type="hidden" name="back" value="calls"><button class="link-btn bad">Delete recording</button></form>')
        return cell
    blocked_set = set(M.safety_ui.blocked_list(me["id"]))

    def _call_actions(r):
        other = str(r["other"] or "")
        if other.startswith("voicemail-") or not M.safety_ui.num_norm(other):
            return ""
        return (M.safety_ui.block_button(csrf, other, "calls", M.safety_ui.num_norm(other) in blocked_set)
                + " " + M.safety_ui.report_button(csrf, "call", r["id"], "calls"))
    rows = "".join(
        f'<tr><td><span class="pill {KIND_LABEL[r["kind"]][1]}">{KIND_LABEL[r["kind"]][0]}</span></td>'
        f'<td>{esc(r["other"])}</td><td>{_dur(r["bill_sec"])}</td><td class="muted">{_ago(r["started_at"], from_utc=False)}</td>'
        f'<td>{rec_cell(r)}</td><td class="actions">{_call_actions(r)}</td></tr>'
        for r in calls) or '<tr><td colspan="6" class="muted">No calls</td></tr>'
    left = _left(me, "call_minutes")
    note = ""
    if _quota(me, "call_minutes") == 0:
        note = _plan_note("Your account can call other extensions only. Outside calls need a plan.")
    elif left == 0:
        note = _plan_note("You've used all your outside call minutes this month. Calls between extensions still work.")
    body = f"""{note}
<div class="filters"><a href="/ucp/calls" class="{'' if missed else 'on'}">All</a>
<a href="/ucp/calls?missed=1" class="{'on' if missed else ''}">Missed</a></div>
<table><tr><th>Type</th><th>Number</th><th>Length</th><th>When</th><th>Recording</th><th></th></tr>{rows}</table>
<p class="muted">Call history is kept for 90 days and then removed automatically. You can delete call recordings you made, but not the call history itself.</p>"""
    return _render(request, s, me, "ucp-calls", "Call history", body)


def voicemail_page(request: Request):
    s, me = _me(request)
    if not s:
        return RedirectResponse("/login")
    box = _mailbox(me["id"])
    folder = "Old" if request.query_params.get("folder") == "Old" else "INBOX"
    msgs = []
    counts = {"INBOX": 0, "Old": 0}
    if box:
        with M.db() as c:
            msgs = [dict(r) for r in c.execute(
                "SELECT * FROM voicemail_messages WHERE mailbox=? AND folder=? ORDER BY id DESC LIMIT 200",
                (box, folder)).fetchall()]
            for f, n in c.execute("SELECT folder, COUNT(*) FROM voicemail_messages WHERE mailbox=? GROUP BY folder", (box,)):
                counts[f] = n
    csrf = M._csrf_field(s)
    move_to = "Old" if folder == "INBOX" else "INBOX"
    rows = "".join(
        f'<tr class="{"" if m["is_read"] else "unread"}" data-id="{int(m["id"])}">'
        f'<td><input type="checkbox" class="sel" name="ids" value="{int(m["id"])}" form="vmdel" aria-label="Select"></td>'
        f'<td>{"" if m["is_read"] else "<span class=dot></span>"}{esc(m["caller"])}</td>'
        f'<td>{_dur(m["duration_sec"])}</td><td class="muted">{_ago(m["received_at"])}</td>'
        f'<td><audio controls preload="none" src="/api/voicemail-audio/{int(m["id"])}" data-vm="{int(m["id"])}"></audio></td>'
        f'<td class="actions"><a href="/api/voicemail-audio/{int(m["id"])}?download=1">Download</a>'
        f'<form method="post" action="/ucp/voicemail/{int(m["id"])}/move" class="inline">{csrf}'
        f'<input type="hidden" name="to" value="{move_to}"><button class="link-btn">'
        f'{"Archive" if folder == "INBOX" else "Move to inbox"}</button></form>'
        f'<form method="post" action="/ucp/voicemail/{int(m["id"])}/delete" class="inline" onsubmit="return confirm(\'Delete this voicemail?\')">{csrf}'
        f'<button class="link-btn bad">Delete</button></form></td></tr>'
        for m in msgs) or f'<tr><td colspan="6" class="muted">{"No voicemail box yet — one is created the first time someone leaves you a message." if not box else "No messages"}</td></tr>'
    gp = _greeting_path(me["id"])
    gplayer = (f'<audio controls preload="none" src="/api/vm-greeting?v={int(os.path.getmtime(gp))}"></audio>'
               f'<form method="post" action="/ucp/voicemail/greeting" class="inline" onsubmit="return confirm(\'Remove your greeting?\')">{csrf}'
               f'<input type="hidden" name="remove" value="1"><button class="link-btn bad">Remove</button></form>'
               if gp else '<span class="muted">Using the standard greeting: "Please leave your message after the tone."</span>')
    greeting = f"""<section class="panel vm-greeting">
<h3>Your greeting</h3>
<div class="greet">{gplayer}</div>
<form method="post" action="/ucp/voicemail/greeting" enctype="multipart/form-data" class="greet-up">{csrf}
<input type="file" name="greeting" accept="audio/*,.wav,.mp3" required>
<button class="btn">{"Replace greeting" if gp else "Upload greeting"}</button>
</form>
<p class="muted">WAV or MP3, up to 2 minutes. Or record it from your phone: dial <b>*98</b>, speak after the tone,
then press <b>#</b>. Something like: "Hi, you've reached {esc(me['display_name'] or me['username'])}. Please leave a message."</p>
</section>"""
    vm_note = (_plan_note("Voicemail isn't included in your plan, so callers can't leave you messages right now.")
               if _quota(me, "voicemail") == 0 else "")
    body = f"""{vm_note}{greeting}
<div class="filters"><a href="/ucp/voicemail" class="{'on' if folder == 'INBOX' else ''}">Inbox ({counts['INBOX']})</a>
<a href="/ucp/voicemail?folder=Old" class="{'on' if folder == 'Old' else ''}">Archived ({counts['Old']})</a></div>
{_bulkbar(s, "vmdel", "/ucp/voicemail/delete", counts[folder], "voicemails in " + ("Inbox" if folder == "INBOX" else "Archived"), f'<input type="hidden" name="folder" value="{folder}">')}
<table><tr><th><input type="checkbox" id="vmdel-all" aria-label="Select all"></th><th>From</th><th>Length</th><th>Received</th><th>Listen</th><th></th></tr>{rows}</table>
{_bulk_js("vmdel")}
<p class="muted">From your phone: dial <b>*97</b> to hear new messages (press <b>7</b> to delete, <b>#</b> for the next one,
<b>0</b> to record your greeting). Callers can press any key to skip your greeting, and <b>#</b> to finish their message.</p>
<script>
const CSRF = "{M._csrf_token(s)}";
document.querySelectorAll('audio[data-vm]').forEach(a => a.addEventListener('play', () => {{
  const id = a.dataset.vm, tr = a.closest('tr');
  if (!tr.classList.contains('unread')) return;
  fetch('/ucp/voicemail/' + id + '/heard', {{method: 'POST', credentials: 'same-origin',
    headers: {{'X-CSRF-Token': CSRF}}}}).then(r => {{ if (r.ok) {{
      tr.classList.remove('unread'); const d = tr.querySelector('.dot'); if (d) d.remove(); }} }});
}}, {{once: true}}));
</script>"""
    return _render(request, s, me, "ucp-vm", "Voicemail", body)


def _own_vm(me, msg_id):
    box = _mailbox(me["id"])
    if not box:
        return None
    with M.db() as c:
        r = c.execute("SELECT * FROM voicemail_messages WHERE id=? AND mailbox=?",
                      (msg_id, box)).fetchone()
    return dict(r) if r else None


async def vm_heard(request: Request, msg_id: int):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if not _own_vm(me, msg_id):
        raise HTTPException(404)
    with M.db() as c:
        c.execute("UPDATE voicemail_messages SET is_read=1 WHERE id=?", (msg_id,))
        c.commit()
    return {"ok": True}


async def vm_move(request: Request, msg_id: int):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    form = await request.form()
    to = "Old" if form.get("to") == "Old" else "INBOX"
    back = "/ucp/voicemail" + ("" if to == "Old" else "?folder=Old")
    if _locked():
        return _back("/ucp/voicemail", "locked")
    if not _own_vm(me, msg_id):
        raise HTTPException(404)
    with M.db() as c:
        c.execute("UPDATE voicemail_messages SET folder=?, is_read=1 WHERE id=?", (to, msg_id))
        c.commit()
    return RedirectResponse(back, status_code=303)


async def vm_bulk_delete(request: Request):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    form = await request.form()
    folder = "Old" if form.get("folder") == "Old" else "INBOX"
    back = "/ucp/voicemail" + ("?folder=Old&" if folder == "Old" else "?")
    if _locked():
        return RedirectResponse(back + "msg=locked", status_code=303)
    box = _mailbox(me["id"])
    rows = []
    if box:
        with M.db() as c:
            if form.get("mode") == "all":
                rows = c.execute("SELECT id, path FROM voicemail_messages WHERE mailbox=? AND folder=?",
                                 (box, folder)).fetchall()
            else:
                ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()][:2000]
                rows = [r for i in ids for r in c.execute(
                    "SELECT id, path FROM voicemail_messages WHERE id=? AND mailbox=?", (i, box)).fetchall()]
            c.executemany("DELETE FROM voicemail_messages WHERE id=?", [(r["id"],) for r in rows])
            c.commit()
    for r in rows:
        _safe_unlink(r["path"], VM_ROOT)
    _audit(me, "ucp.voicemail.delete", f"{len(rows)} rows")
    return RedirectResponse(back + "msg=vmdel", status_code=303)


async def vm_delete(request: Request, msg_id: int):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if _locked():
        return _back("/ucp/voicemail", "locked")
    m = _own_vm(me, msg_id)
    if not m:
        raise HTTPException(404)
    with M.db() as c:
        c.execute("DELETE FROM voicemail_messages WHERE id=?", (msg_id,))
        c.commit()
    _safe_unlink(m["path"], VM_ROOT)
    _audit(me, "ucp.voicemail.delete", f"id={msg_id}")
    return _back("/ucp/voicemail", "vmdel")


async def vm_greeting_save(request: Request):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if _locked():
        return _back("/ucp/voicemail", "locked")
    if not me["exten"]:
        return _back("/ucp/voicemail", "noext")
    form = await request.form()
    old = _greeting_path(me["id"])
    box = _ensure_mailbox(me)
    if form.get("remove"):
        with M.db() as c:
            c.execute("UPDATE voicemail_boxes SET greeting_path='' WHERE mailbox=?", (box,))
            c.commit()
        _safe_unlink(old, VMGREET_DIR)
        _audit(me, "ucp.vm_greeting.remove")
        return _back("/ucp/voicemail", "greetremoved")
    up = form.get("greeting")
    if up is None or not getattr(up, "filename", ""):
        return _back("/ucp/voicemail", "greetnone")
    data = await up.read()
    try:
        path = M.ivr_ui._convert_greeting(data, up.filename, box, directory=VMGREET_DIR,
                                          prefix="vmgreet-up", max_sec=VM_GREETING_MAX_SEC)
    except ValueError as e:
        s2, me2 = _me(request)
        request.scope["query_string"] = b""
        return _render(request, s2, me2, "ucp-vm", "Voicemail",
                       f'<div class="flash bad">{esc(str(e))}</div><p><a href="/ucp/voicemail">Back to voicemail</a></p>')
    with M.db() as c:
        c.execute("UPDATE voicemail_boxes SET greeting_path=? WHERE mailbox=?", (path, box))
        c.commit()
    if old and old != path:
        _safe_unlink(old, VMGREET_DIR)
    _audit(me, "ucp.vm_greeting.upload")
    return _back("/ucp/voicemail", "greetsaved")


def vm_greeting_audio(request: Request):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    p = _greeting_path(me["id"])
    if not p or os.path.commonpath([os.path.abspath(p), VMGREET_DIR]) != VMGREET_DIR:
        raise HTTPException(404)
    from fastapi.responses import FileResponse
    return FileResponse(p, media_type="audio/wav", headers={"Cache-Control": "no-store"})


def recordings_page(request: Request):
    s, me = _me(request)
    if not s:
        return RedirectResponse("/login")
    recs = _recordings_for(me)
    csrf = M._csrf_field(s)
    own = sum(1 for r in recs if r["login_id"] == me["id"])
    rows = "".join(
        f'<tr><td>{("<input type=checkbox class=sel name=ids value=" + str(int(r["id"])) + " form=recdel aria-label=Select>") if r["login_id"] == me["id"] else ""}</td>'
        f'<td>{esc(r["peer"] if r["exten"] == me["exten"] else r["exten"])}</td>'
        f'<td>{esc("Outgoing" if r["exten"] == me["exten"] else "Incoming")}</td>'
        f'<td>{_dur(r["duration_sec"])}</td><td class="muted">{_ago(r["started_at"])}</td>'
        f'<td><audio controls preload="none" src="/api/recording-audio/{int(r["id"])}"></audio></td>'
        f'<td class="actions"><a href="/api/recording-audio/{int(r["id"])}?download=1">Download</a>'
        + (f'<form method="post" action="/ucp/recordings/delete" class="inline" onsubmit="return confirm(\'Delete this recording?\')">{csrf}'
           f'<input type="hidden" name="ids" value="{int(r["id"])}"><input type="hidden" name="mode" value="selected">'
           f'<button class="link-btn bad">Delete</button></form>' if r["login_id"] == me["id"] else "")
        + '</td></tr>'
        for r in recs) or '<tr><td colspan="7" class="muted">No recordings. Turn on "Record my calls" in Settings to start recording.</td></tr>'
    rec_note = (_plan_note("Call recording isn't included in your plan.") if _quota(me, "recording") == 0 else
                '<p class="muted legal-mini">Recording laws vary; some states require everyone on the call to agree. '
                'Make sure you have consent where required.</p>')
    body = f"""{rec_note}
{(_bulkbar(s, "recdel", "/ucp/recordings/delete", own, "recordings you made") + _bulk_js("recdel")) if own else ""}
<table><tr><th>{'<input type="checkbox" id="recdel-all" aria-label="Select all">' if own else ""}</th><th>With</th><th>Direction</th><th>Length</th><th>When</th><th>Listen</th><th></th></tr>{rows}</table>
{_bulk_js("recdel") if own else ""}
<p class="muted">Only recordings you started are shown here.</p>"""
    return _render(request, s, me, "ucp-rec", "Recordings", body)


async def rec_bulk_delete(request: Request):
    """Delete selected / all recordings the user started (never the other
    party's, never admin recordings). The call history row stays."""
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    form = await request.form()
    back = "/ucp/calls" if form.get("back") == "calls" else "/ucp/recordings"
    if _locked():
        return _back(back, "locked")
    with M.db() as c:
        if form.get("mode") == "all":
            rows = c.execute("SELECT id, path FROM recordings WHERE system='user' AND login_id=?", (me["id"],)).fetchall()
        else:
            ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()][:2000]
            rows = [r for i in ids for r in c.execute(
                "SELECT id, path FROM recordings WHERE id=? AND system='user' AND login_id=?", (i, me["id"])).fetchall()]
        for r in rows:
            c.execute("UPDATE cdr SET recording_id=NULL WHERE recording_id=?", (r["id"],))
            c.execute("DELETE FROM recordings WHERE id=?", (r["id"],))
        c.commit()
    for r in rows:
        _safe_unlink(r["path"], REC_ROOT)
    _audit(me, "ucp.recording.delete", f"{len(rows)} rows")
    return _back(back, "recdel")


def _my_messages(me, limit=500):
    ex = me["exten"]
    if not ex:
        return []
    with M.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM messages WHERE (from_ext=? OR to_ext=?)"
            " AND (login_id=? OR ? IS NULL OR sent_at >= ?) AND id NOT IN"
            " (SELECT message_id FROM message_hidden WHERE login_id=?) ORDER BY id DESC LIMIT ?",
            (ex, ex, me["id"], _exten_since(me), _exten_since(me), me["id"], limit)).fetchall()]


def messages_page(request: Request):
    s, me = _me(request)
    if not s:
        return RedirectResponse("/login")
    ex = me["exten"]
    msgs = _my_messages(me)
    # Group into conversations by the other party.
    convs = {}
    for m in msgs:
        other = m["to_ext"] if m["from_ext"] == ex else m["from_ext"]
        convs.setdefault(other, []).append(m)
    sel = request.query_params.get("with", "")
    if sel not in convs:
        sel = next(iter(convs), "")
    side = "".join(
        f'<div class="conv-row"><input type="checkbox" class="sel" name="convs" value="{esc(o)}" form="msgdel" aria-label="Select conversation">'
        f'<a href="/ucp/messages?with={urllib.parse.quote(o)}" class="conv {"on" if o == sel else ""}">'
        f'<b>{esc(o)}</b><span class="muted">{esc(ms[0]["body"][:40])} · {len(ms)}</span></a></div>'
        for o, ms in convs.items()) or '<p class="muted">No messages yet.</p>'
    csrf_m = M._csrf_field(s)
    back_m = "messages:" + sel
    thread = "".join(
        f'<div class="bubble {"mine" if m["from_ext"] == ex else ""}">'
        f'<input type="checkbox" class="sel" name="ids" value="{int(m["id"])}" form="msgdel" aria-label="Select message">'
        f'{esc(m["body"])}<span class="muted">{_ago(m["sent_at"])}'
        f'{"" if m["from_ext"] == ex else " · " + M.safety_ui.report_button(csrf_m, "message", m["id"], back_m)}</span></div>'
        for m in reversed(convs.get(sel, [])))
    if sel:
        thread = (f'<div class="muted" style="margin-bottom:8px">Conversation with <b>{esc(sel)}</b> · '
                  f'{M.safety_ui.block_button(csrf_m, sel, back_m, M.safety_ui.is_blocked(me["id"], sel))}</div>') + thread
    mleft = _left(me, "messages")
    can_send = _quota(me, "messages") != 0 and mleft != 0
    csrf = M._csrf_field(s)
    dis = "" if can_send else "disabled"
    left_txt = ("Unlimited texts" if mleft is None else f"{mleft} text{'s' if mleft != 1 else ''} left this month")
    compose = f"""<form method="post" action="/ucp/messages/send" class="panel msg-compose">{csrf}
<b>New message</b> <span class="muted">· {esc(left_txt) if can_send else 'Sending isn\'t available on your plan right now'}</span><br>
<input name="to" value="{esc(sel)}" placeholder="Extension (e.g. 8801)" inputmode="numeric" size="12" required {dis}>
<textarea name="body" rows="2" maxlength="1000" placeholder="Type a message" required {dis} style="width:100%;margin-top:6px"></textarea>
<button class="btn" {dis}>Send</button></form>"""
    mnote = ""
    if _quota(me, "messages") == 0:
        mnote = _plan_note("Sending texts isn't included in your plan. You can still read messages you receive.")
    elif mleft == 0:
        mnote = _plan_note("You've used all your texts this month.")
    body = f"""{mnote}
{compose}
{_bulkbar(s, "msgdel", "/ucp/messages/delete", len(msgs), "messages", f'<input type="hidden" name="with" value="{esc(sel)}">')}
<label class="inline-chk muted" style="margin-bottom:8px!important"><input type="checkbox" id="msgdel-all"> Select everything shown</label>
<div class="msgs"><div class="convs">{side}</div><div class="thread">{thread or '<p class="muted">Pick a conversation.</p>'}</div></div>
{_bulk_js("msgdel")}
<div id="msg-new" class="flash ok" style="display:none;position:sticky;bottom:10px;cursor:pointer">New message - <u>show</u></div>
<script>
(function () {{
  var latest = {max([m["id"] for m in msgs], default=0)};
  var box = document.getElementById('msg-new');
  var th = document.querySelector('.thread'); if (th) th.scrollTop = th.scrollHeight;
  function busy() {{
    var t = document.querySelector('.msg-compose textarea');
    return (t && t.value.trim() !== '') || document.querySelector('input.sel:checked');
  }}
  box.addEventListener('click', function () {{ location.reload(); }});
  setInterval(function () {{
    if (document.hidden) return;
    fetch('/ucp/messages/latest', {{credentials: 'same-origin'}}).then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (j) {{
        if (!j || !(j.latest > latest)) return;
        if (busy()) {{ box.style.display = ''; }} else {{ location.reload(); }}
      }}).catch(function () {{}});
  }}, 10000);
}})();
</script>
<p class="muted">Tick a conversation on the left to delete all of it, or tick single messages. Deleting only removes
messages from your view; the other person keeps their copy. You can also text from your phone app; both count
toward your plan's monthly texts.</p>"""
    return _render(request, s, me, "ucp-msg", "Messages", body)


_send_times: dict[int, list[float]] = {}
MSG_RATE = 20  # per minute per user (stops spam / runaway scripts)


async def msg_send(request: Request):
    """Send a text from My Phone. Internal exten or external PSTN via voip.ms.
    Counts against the plan's monthly texts exactly like texts sent from a phone."""
    import time as _t
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    f = await request.form()
    to = (f.get("to") or "").strip()
    body = "".join(ch for ch in (f.get("body") or "").replace("\r", "") if ch == "\n" or ch.isprintable()).strip()[:1000]
    back = "/ucp/messages" + (f"?with={urllib.parse.quote(to)}&" if to else "?")
    if not me["exten"]:
        return RedirectResponse(back + "msg=msgnoext", status_code=303)
    if not body:
        return RedirectResponse(back + "msg=msgempty", status_code=303)
    is_external = voipms_sms.is_external_number(to)
    with M.db() as c:
        if is_external:
            # External SMS via voip.ms - validate sender only
            if not M.safety_ui.msg_prefs(me["id"])["msg_out"]:
                return RedirectResponse(back + "msg=msgoff", status_code=303)
            if _quota(me, "messages") == 0 or _left(me, "messages") == 0:
                return RedirectResponse(back + "msg=msgquota", status_code=303)
            sent_month = c.execute("SELECT COUNT(*) FROM messages WHERE login_id=? AND sent_at >= "
                                   "datetime('now','localtime','start of month','utc')", (me["id"],)).fetchone()[0]
            if sent_month >= int(me["max_messages"] or 0):
                return RedirectResponse(back + "msg=msgquota", status_code=303)
            cfg = voipms_sms.get_voipms_config(c)
            sender_did = voipms_sms.lookup_sender_did(c, me["exten"])
            if not sender_did or not cfg.get("voipms_api_username") or not cfg.get("voipms_api_password"):
                return RedirectResponse(back + "msg=msgto", status_code=303)
            dest_e164 = voipms_sms.e164(to)
            now = _t.time()
            recent = [x for x in _send_times.get(me["id"], []) if now - x < 60]
            if len(recent) >= MSG_RATE:
                return RedirectResponse(back + "msg=msgrate", status_code=303)
            _send_times[me["id"]] = recent + [now]
            c.execute("INSERT INTO messages (from_ext, to_ext, body, login_id) VALUES (?,?,?,?)",
                      (me["exten"], dest_e164, body, me["id"]))
            c.commit()
            # Send via voip.ms API (outside DB lock)
            try:
                voipms_sms.send_sms_via_voipms(cfg["voipms_api_username"], cfg["voipms_api_password"],
                                              sender_did, dest_e164, body)
            except Exception:
                pass
            return RedirectResponse(back + "msg=msgsent", status_code=303)
        dest = c.execute("SELECT id, exten FROM logins WHERE exten=? AND enabled=1", (to,)).fetchone()
        if not dest or not re.fullmatch(r"\d{2,8}", to) or to == me["exten"]:
            return RedirectResponse(back + "msg=msgto", status_code=303)
        if not M.safety_ui.msg_prefs(me["id"])["msg_out"]:
            return RedirectResponse(back + "msg=msgoff", status_code=303)
        if not M.safety_ui.msg_prefs(dest["id"])["msg_in"] or M.safety_ui.is_blocked(dest["id"], me["exten"]):
            # Same answer for "turned off" and "blocked" (doesn't reveal a block).
            return RedirectResponse(back + "msg=msgnotaccepting", status_code=303)
        if _quota(me, "messages") == 0 or _left(me, "messages") == 0:
            return RedirectResponse(back + "msg=msgquota", status_code=303)
        sent_month = c.execute("SELECT COUNT(*) FROM messages WHERE login_id=? AND sent_at >= "
                               "datetime('now','localtime','start of month','utc')", (me["id"],)).fetchone()[0]
        if sent_month >= int(me["max_messages"] or 0):
            return RedirectResponse(back + "msg=msgquota", status_code=303)
        now = _t.time()
        recent = [x for x in _send_times.get(me["id"], []) if now - x < 60]
        if len(recent) >= MSG_RATE:
            return RedirectResponse(back + "msg=msgrate", status_code=303)
        _send_times[me["id"]] = recent + [now]
        c.execute("INSERT INTO messages (from_ext, to_ext, body, login_id) VALUES (?,?,?,?)",
                  (me["exten"], to, body, me["id"]))
        c.commit()
    # Push to the recipient's phone (best effort; it's stored either way).
    try:
        import json as _json
        import urllib.request as _ur
        req = _ur.Request(M.BRAIN_STATUS_URL + "/messages/send",
                          data=_json.dumps({"to": to, "from": me["exten"], "body": body}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        _ur.urlopen(req, timeout=5).read()
    except Exception:
        pass
    return RedirectResponse(back + "msg=msgsent", status_code=303)


def msg_latest(request: Request):
    """Newest message id in this user's view (the Messages page polls it)."""
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    if not me["exten"]:
        return JSONResponse({"latest": 0})
    msgs = _my_messages(me, limit=1)
    return JSONResponse({"latest": msgs[0]["id"] if msgs else 0})


async def msg_delete(request: Request):
    """Hide messages from this user's view (selected messages, whole
    conversations, or everything). The other party keeps theirs."""
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if _locked():
        return _back("/ucp/messages", "locked")
    form = await request.form()
    mine = _my_messages(me, limit=100000)
    ex = me["exten"]
    if form.get("mode") == "all":
        ids = [m["id"] for m in mine]
    else:
        want_ids = {int(x) for x in form.getlist("ids") if str(x).isdigit()}
        want_convs = set(form.getlist("convs"))
        ids = [m["id"] for m in mine
               if m["id"] in want_ids or (m["to_ext"] if m["from_ext"] == ex else m["from_ext"]) in want_convs]
    with M.db() as c:
        c.executemany("INSERT OR IGNORE INTO message_hidden (message_id, login_id) VALUES (?,?)",
                      [(i, me["id"]) for i in ids])
        c.commit()
    _audit(me, "ucp.messages.delete", f"{len(ids)} rows")
    w = form.get("with") or ""
    return RedirectResponse("/ucp/messages?msg=deleted" + (f"&with={urllib.parse.quote(w)}" if w else ""), status_code=303)


def _settings_body(request, s, me, extra_top=""):
    p = _prefs(me["id"])
    csrf = M._csrf_field(s)
    host = request.url.hostname or "this server"
    rs = M.network_ui.remote_settings()
    host = rs.get("lan_host") or host
    if rs["domain"]:
        remote_html = (f'<table class="kv">'
                       f'<tr><td>Domain</td><td><code>{esc(rs["domain"])}</code></td></tr>'
                       f'<tr><td>Port</td><td><code>{esc(rs["port"])}</code></td></tr>'
                       f'<tr><td>Transport</td><td>TLS</td></tr>'
                       f'<tr><td>Outbound proxy</td><td><code>{esc(rs["domain"])}:{esc(rs["port"])}</code></td></tr>'
                       f'<tr><td>Encryption</td><td>SRTP on (encrypts call audio)</td></tr>'
                       f'<tr><td>Re-register</td><td>every 60–120 seconds</td></tr></table>'
                       f'<p class="muted">Use the same username and password. The short re-register time lets your phone '
                       f'reconnect quickly if the office internet address changes.</p>')
    else:
        remote_html = '<p class="muted">Remote access isn&#8217;t set up yet. Ask your administrator.</p>'

    chk = lambda v: "checked" if v else ""
    import entitlements as ent
    rec_ok = _quota(me, "recording") != 0
    acc_at, acc_ver = _consent(me)
    consented = acc_ver >= ent.RECORDING_CONSENT_VERSION
    if not rec_ok:
        rec_block = ('<label class="switch"><input type="checkbox" disabled> <span>Record my calls</span></label>'
                     '<p class="muted">Call recording isn\'t included in your plan. <a href="/ucp/billing">See plans</a></p>')
    else:
        ack = (f'<p class="muted ok">You accepted the notice on {esc(acc_at)}.</p>' if consented else
               f'<label class="ack"><input type="checkbox" name="record_ack" value="1"> {esc(ent.RECORDING_ACK)}</label>')
        admin_ok = bool(me["user_record"])
        wish = bool(me.get("user_wants_record", 0))
        if not admin_ok:
            rec_status = ('<p class="warn">⚠️ <b>Recording is turned off by your admin</b> for this extension. '
                          'Your preference below is saved, but calls will not be recorded until your admin allows it.</p>')
        elif wish and consented:
            rec_status = ('<p class="ok">✅ <b>Recording is on</b> — your answered calls are being recorded '
                          'and appear under Recordings.</p>')
        else:
            rec_status = ('<p class="muted">Recording is allowed by your admin. '
                          'Check "Record my calls" below to start recording your calls.</p>')
        rec_block = (f'{rec_status}'
                     f'<label class="switch"><input type="checkbox" name="user_record" value="1" {chk(wish and consented)}> '
                     f'<span>Record my calls</span></label>'
                     f'<p class="muted">Answered calls are recorded and appear under Recordings.</p>'
                     f'<div class="legal"><b>Before you record calls:</b> {esc(ent.RECORDING_NOTICE)}</div>{ack}')
    cur_ring = p["ring_seconds"] or DEFAULT_RING
    ring_opts = "".join(
        f'<option value="{v}" {"selected" if v == cur_ring else ""}>{v} seconds{" (default)" if v == DEFAULT_RING else ""}</option>'
        for v in RING_CHOICES)
    with M.db() as c:
        my_menus = [dict(r) for r in c.execute(
            "SELECT id, name FROM ivr_menus WHERE owner_login_id=? ORDER BY id", (me["id"],))]
    ivr_opts = '<option value="">My phone (no menu)</option>' + "".join(
        f'<option value="{m["id"]}" {"selected" if m["id"] == p["answer_ivr_id"] else ""}>IVR menu: {esc(m["name"])}</option>'
        for m in my_menus)
    return extra_top + f"""
<div class="grid2">
<section class="panel">
<h3>Call handling</h3>
<form method="post" action="/ucp/settings/calls">{csrf}
<label class="switch"><input type="checkbox" name="dnd" value="1" {chk(p["dnd"])}> <span>Do not disturb</span></label>
<p class="muted">Every call goes straight to your voicemail.</p>
<label>Forward all calls to<br><input name="forward_always" value="{esc(p["forward_always"])}" placeholder="extension or number — leave empty for off" inputmode="tel"></label>
<label>Ring my phone for<br><select name="ring_seconds">{ring_opts}</select></label>
<p class="muted">How long your phone rings before the call goes to voicemail (or your no-answer forward).</p>
<label>If I don't answer, forward to<br><input name="forward_noanswer" value="{esc(p["forward_noanswer"])}" placeholder="leave empty to go to voicemail" inputmode="tel"></label>
<label>Answer my calls with<br><select name="answer_ivr_id">{ivr_opts}</select></label>
<p class="muted">Pick one of your IVR menus to greet callers before your phone rings. Build menus on the IVR tab.</p>
<p class="muted">Outside numbers use the company's phone lines. Forwarding on a shared inbound number that rings a group applies only to other extensions, not outside numbers.</p>
<button class="btn">Save call handling</button>
</form>
</section>
<section class="panel">
<h3>Recording, voicemail &amp; email</h3>
<form method="post" action="/ucp/settings/prefs">{csrf}
{rec_block}
<label>Email for notifications<br><input name="vm_email" type="email" value="{esc(me["vm_email"])}" placeholder="you@example.com"></label>
<p class="muted">New voicemails are emailed here. Leave it empty for no emails.</p>
<label class="switch"><input type="checkbox" name="text_email" value="1" {"checked" if p.get("text_email", 1) else ""}> <span>Also email me when I get a text message</span></label>
{'' if _smtp_ready() else '<p class="muted">Email isn&#8217;t switched on by the administrator yet; your address is saved for when it is.</p>'}
<button class="btn">Save</button>
</form>
</section>
<section class="panel">
<h3>Change password</h3>
<form method="post" action="/ucp/settings/password">{csrf}
<label>Current password<br><input name="current" type="password" required autocomplete="current-password"></label>
<label>New password<br><input name="new" type="password" required minlength="8" autocomplete="new-password"></label>
<label>Confirm new password<br><input name="confirm" type="password" required minlength="8" autocomplete="new-password"></label>
<button class="btn">Change password</button>
</form>
</section>
<section class="panel">
<h3>911</h3>
{M.e911_ui.user_notice(me["id"])}
<h3>Phone setup</h3>
<div class="setup-grid">
<div><h4>In the office</h4>
<table class="kv">
<tr><td>Server</td><td><code>{esc(host)}</code></td></tr>
<tr><td>Port</td><td><code>5060</code></td></tr>
<tr><td>Transport</td><td>UDP</td></tr>
</table></div>
<div><h4>Away from the office</h4>
{remote_html}
</div>
</div>
<table class="kv" style="margin-top:10px">
<tr><td>Username</td><td><code>{esc(me["sip_username"] or "-")}</code></td></tr>
<tr><td>Extension</td><td><code>{esc(me["exten"] or "-")}</code></td></tr>
<tr><td>Password</td><td class="muted">Hidden. Generate a new one below if you need to set up a phone.</td></tr>
</table>
<form method="post" action="/ucp/settings/sip-secret" onsubmit="return confirm('Generate a new SIP password? Phones using the old one will disconnect until you update them.')">{csrf}
<button class="btn ghost">Generate new SIP password</button>
</form>
</section>
{M.safety_ui.settings_section(me, csrf)}
</div>"""


def settings_page(request: Request):
    s, me = _me(request)
    if not s:
        return RedirectResponse("/login")
    if not me["exten"]:
        return _render(request, s, me, "ucp-set", "Settings",
                       '<p class="muted">Your login has no extension, so there are no phone settings. '
                       'You can still change your password on the Logins page (admins) or ask the administrator.</p>')
    return _render(request, s, me, "ucp-set", "Settings", _settings_body(request, s, me))


async def settings_calls(request: Request):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if _locked():
        return _back("/ucp/settings", "locked")
    if not me["exten"]:
        return _back("/ucp/settings", "noext")
    f = await request.form()
    dnd = 1 if f.get("dnd") == "1" else 0
    fa = (f.get("forward_always") or "").strip().replace(" ", "").replace("-", "")
    fn = (f.get("forward_noanswer") or "").strip().replace(" ", "").replace("-", "")
    for v in (fa, fn):
        if v and not FWD_RE.match(v):
            return _back("/ucp/settings", "fwdbad")
        if v and v == me["exten"]:
            return _back("/ucp/settings", "fwdself")
    try:
        ring = int(f.get("ring_seconds") or DEFAULT_RING)
    except ValueError:
        ring = -1
    if ring not in RING_CHOICES:
        return _back("/ucp/settings", "ringbad")
    ring_store = 0 if ring == DEFAULT_RING else ring
    ivr_raw = (f.get("answer_ivr_id") or "").strip()
    ivr_id = None
    if ivr_raw:
        with M.db() as c:
            ok = ivr_raw.isdigit() and c.execute(
                "SELECT 1 FROM ivr_menus WHERE id=? AND owner_login_id=?",
                (int(ivr_raw), me["id"])).fetchone()
        if not ok:
            return _back("/ucp/settings", "ivrbad")
        ivr_id = int(ivr_raw)
    with M.db() as c:
        c.execute("INSERT INTO user_prefs (login_id, dnd, forward_always, forward_noanswer,"
                  " ring_seconds, answer_ivr_id, updated_at)"
                  " VALUES (?,?,?,?,?,?,datetime('now'))"
                  " ON CONFLICT(login_id) DO UPDATE SET dnd=excluded.dnd,"
                  " forward_always=excluded.forward_always,"
                  " forward_noanswer=excluded.forward_noanswer,"
                  " ring_seconds=excluded.ring_seconds, answer_ivr_id=excluded.answer_ivr_id,"
                  " updated_at=excluded.updated_at",
                  (me["id"], dnd, fa, fn, ring_store, ivr_id))
        c.commit()
    _audit(me, "ucp.calls", f"dnd={dnd} fwd_always={fa} fwd_noanswer={fn} ring={ring} ivr={ivr_id}")
    return _back("/ucp/settings", "saved")


async def dnd_toggle(request: Request):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if _locked():
        return _back("/ucp", "locked")
    if not me["exten"]:
        return _back("/ucp", "noext")
    f = await request.form()
    on = 1 if f.get("on") == "1" else 0
    with M.db() as c:
        c.execute("INSERT INTO user_prefs (login_id, dnd) VALUES (?,?)"
                  " ON CONFLICT(login_id) DO UPDATE SET dnd=excluded.dnd, updated_at=datetime('now')",
                  (me["id"], on))
        c.commit()
    _audit(me, "ucp.dnd", str(on))
    return _back("/ucp", "saved")


async def settings_prefs(request: Request):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if _locked():
        return _back("/ucp/settings", "locked")
    import entitlements as ent
    f = await request.form()
    rec = 1 if f.get("user_record") == "1" else 0
    email = (f.get("vm_email") or "").strip()
    if email and (len(email) > 254 or not EMAIL_RE.match(email)):
        return _back("/ucp/settings", "emailbad")
    if rec:
        if _quota(me, "recording") == 0:
            return _back("/ucp/settings", "recplan")
        _, ver = _consent(me)
        if ver < ent.RECORDING_CONSENT_VERSION:
            if f.get("record_ack") != "1":
                return _back("/ucp/settings", "recack")
            with M.db() as c:
                c.execute("INSERT INTO user_prefs (login_id, recording_consent_at, recording_consent_version)"
                          " VALUES (?, datetime('now','localtime'), ?) ON CONFLICT(login_id) DO UPDATE SET"
                          " recording_consent_at=excluded.recording_consent_at,"
                          " recording_consent_version=excluded.recording_consent_version",
                          (me["id"], ent.RECORDING_CONSENT_VERSION))
                c.commit()
            ip = M.client_ip(request)
            _audit(me, "ucp.recording_consent", f"version={ent.RECORDING_CONSENT_VERSION} ip={ip}")
    txt = 1 if f.get("text_email") == "1" else 0
    with M.db() as c:
        c.execute("UPDATE logins SET user_wants_record=?, vm_email=? WHERE id=?", (rec, email, me["id"]))
        c.execute("INSERT INTO user_prefs (login_id, text_email) VALUES (?,?)"
                  " ON CONFLICT(login_id) DO UPDATE SET text_email=excluded.text_email", (me["id"], txt))
        c.commit()
    _audit(me, "ucp.prefs", f"user_wants_record={rec} text_email={txt}")
    return _back("/ucp/settings", "saved")


async def settings_password(request: Request):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if _locked():
        return _back("/ucp/settings", "locked")
    f = await request.form()
    cur, new, conf = f.get("current") or "", f.get("new") or "", f.get("confirm") or ""
    if not bcrypt.checkpw(cur.encode(), me["pwhash"].encode()):
        _audit(me, "ucp.password.fail")
        return _back("/ucp/settings", "pwbad")
    if len(new) < 8:
        return _back("/ucp/settings", "pwshort")
    if new != conf:
        return _back("/ucp/settings", "pwmatch")
    pwh = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
    with M.db() as c:
        c.execute("UPDATE logins SET pwhash=? WHERE id=?", (pwh, me["id"]))
        c.commit()
    # Stay signed in here; sign out every other session of this login.
    s["pwsig"] = M._pw_sig(pwh)
    M.drop_sessions(me["id"], keep=request.cookies.get("pbx_session") or "")
    _audit(me, "ucp.password")
    return _back("/ucp/settings", "pw")


async def settings_sip_secret(request: Request):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if _locked():
        return _back("/ucp/settings", "locked")
    if not me["exten"] or not me["sip_username"]:
        return _back("/ucp/settings", "noext")
    new_secret = secrets.token_urlsafe(24)
    with M.db() as c:
        c.execute("UPDATE logins SET sip_secret=? WHERE id=?", (new_secret, me["id"]))
        c.commit()
    applied, err = M._apply_guarded()
    _audit(me, "ucp.sip_secret", "applied" if applied else f"apply failed: {err}")
    me["sip_secret"] = new_secret
    note = ("" if applied else
            '<p class="bad">Saved, but the phone system did not reload. Ask the administrator to reload, '
            'or the new password won\'t work yet.</p>')
    top = f"""<div class="flash ok secret"><b>New SIP password</b> — copy it now, it won't be shown again:
<code id="sec">{esc(new_secret)}</code> <button class="btn ghost" type="button"
onclick="navigator.clipboard.writeText(document.getElementById('sec').textContent)">Copy</button>{note}</div>"""
    resp = _render(request, s, me, "ucp-set", "Settings", _settings_body(request, s, me, top))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- install

def install(app_module):
    """Register the /ucp routes on app_module.app."""
    global M
    M = app_module
    app = M.app
    html = dict(response_class=HTMLResponse)
    app.get("/ucp", **html)(overview)
    app.get("/ucp/calls", **html)(calls_page)
    app.get("/ucp/voicemail", **html)(voicemail_page)
    app.post("/ucp/voicemail/greeting")(vm_greeting_save)
    app.post("/ucp/voicemail/delete")(vm_bulk_delete)
    app.get("/api/vm-greeting")(vm_greeting_audio)
    app.post("/ucp/voicemail/{msg_id}/heard")(vm_heard)
    app.post("/ucp/voicemail/{msg_id}/move")(vm_move)
    app.post("/ucp/voicemail/{msg_id}/delete")(vm_delete)
    app.get("/ucp/recordings", **html)(recordings_page)
    app.post("/ucp/recordings/delete")(rec_bulk_delete)
    app.get("/ucp/messages", **html)(messages_page)
    app.post("/ucp/messages/delete")(msg_delete)
    app.post("/ucp/messages/send")(msg_send)
    app.get("/ucp/messages/latest")(msg_latest)
    app.get("/ucp/settings", **html)(settings_page)
    app.post("/ucp/settings/calls")(settings_calls)
    app.post("/ucp/settings/prefs")(settings_prefs)
    app.post("/ucp/settings/password")(settings_password)
    app.post("/ucp/settings/sip-secret")(settings_sip_secret)
    app.post("/ucp/dnd")(dnd_toggle)
