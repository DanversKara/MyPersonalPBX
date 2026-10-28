# SPDX-License-Identifier: GPL-2.0-or-later
"""Account deletion and data download requests.

User side (/ucp/delete, also the public "delete your account" link for app
store listings): a signed-in user can ask for
  - a copy of their data: a zip of their account details, call history,
    texts (SMS/MMS/internal), voicemail, recordings, faxes and contacts,
    emailed to them as a download link (valid LINK_DAYS days), and/or
  - their account to be deleted. The administrator does it within
    DELETE_DAYS days (the user can cancel until then).
Visitors who aren't signed in get the explanation and a sign-in button.

Admin side (/privacy): the open requests with their deadlines, buttons to
build + email the download, delete the account (same wipe as the Logins
page), or decline; history; the contact address and "build downloads
automatically" setting.

The download link (/data-export/<token>) needs no sign-in, so it still
works after the account is deleted. Only a hash of the token is stored;
expired zips are removed.
"""
import csv
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import time
import urllib.parse
import zipfile

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

import mailer

M = None
EXPORT_DIR = os.environ.get("PBX_EXPORT_DIR", "/var/lib/pbx/exports")
DELETE_DAYS = 90
EXPORT_DAYS = 30           # deadline for sending a data copy
LINK_DAYS = 7              # how long a download link works
KINDS = {"delete": "Delete account", "export": "Data download"}
EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")
SKIP_COLS = {"pwhash", "sip_secret", "token_hash", "pwsig", "push_endpoint", "sso_subject"}

FLASH = {
    "exp_ok": ("ok", "Got it. You'll get an email with a download link for your data."),
    "exp_sent": ("ok", "Your data is ready. We've emailed you a download link."),
    "del_ok": ("ok", "Your deletion request was sent to the administrator."),
    "del_cancel": ("ok", "Deletion request cancelled. Your account stays as it is."),
    "dup": ("bad", "You already have a request like that open."),
    "email": ("bad", "Enter a valid email address."),
    "confirm": ("bad", "Type DELETE and tick the box to confirm."),
    "noacct": ("bad", "Administrator accounts are removed on the Logins page, not here."),
}


def esc(x):
    return M.esc(x)


def _kv(key, default=""):
    with M.db() as c:
        r = c.execute("SELECT value FROM kv_settings WHERE key=?", (key,)).fetchone()
    return r[0] if r and r[0] is not None else default


def _set(key, value):
    with M.db() as c:
        c.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (key, value))
        c.commit()


def _now():
    return datetime.datetime.utcnow().replace(microsecond=0)


def _fmt(ts):
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _date(utc_text):
    """'YYYY-MM-DD' (shown dates are the deadline day; good enough)."""
    return (utc_text or "")[:10]


def _days_left(utc_text):
    try:
        due = datetime.datetime.strptime(utc_text[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (due - _now()).days


def _base_url(request):
    return M.public_base(request)


def _mail_cfg():
    with M.db() as c:
        return mailer.settings(c)


def _send(to, subject, text):
    """True if sent. Never raises (the request is saved either way)."""
    try:
        mailer.send(_mail_cfg(), to, subject, text)
        return True
    except Exception:
        return False


def _admin_to():
    return (_kv("privacy_contact") or _kv("store_notify") or _kv("sec_alert_to")).strip()


def _brand():
    return _kv("smtp_from_name") or "your phone service"


def _audit(actor, action, detail=""):
    M._audit_log(actor, action, detail)


# ------------------------------------------------------------ the data zip

def _safe_file(path, root):
    """Real path of a file inside root, else ''."""
    try:
        real = os.path.realpath(path or "")
        rr = os.path.realpath(root)
        return real if path and real.startswith(rr + os.sep) and os.path.isfile(real) else ""
    except Exception:
        return ""


def _slug(x, n=40):
    return re.sub(r"[^0-9A-Za-z+_-]", "_", str(x or ""))[:n] or "x"


def _csv(rows, cols):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if r.get(k) is None else r.get(k) for k in cols])
    return buf.getvalue()


def _clean_row(r):
    return {k: v for k, v in dict(r).items() if k not in SKIP_COLS}


def build_export(login_id):
    """Write the user's data zip; returns (path, bytes, counts)."""
    import ucp
    import fax_ui
    import fax_send
    with M.db() as c:
        me = c.execute("SELECT * FROM logins WHERE id=?", (login_id,)).fetchone()
        if not me:
            raise ValueError("That account no longer exists.")
        me = dict(me)
        prefs = c.execute("SELECT * FROM user_prefs WHERE login_id=?", (login_id,)).fetchone()
        plan = [_clean_row(r) for r in c.execute("SELECT * FROM user_entitlements WHERE login_id=?", (login_id,))]
        filters = [_clean_row(r) for r in c.execute("SELECT * FROM blocked_numbers WHERE login_id=?", (login_id,))]
        devices = [{"name": r["name"], "created_at": r["created_at"], "last_used_at": r["last_used_at"]}
                   for r in c.execute("SELECT * FROM app_devices WHERE login_id=?", (login_id,))]
        contacts = [_clean_row(r) for r in c.execute("SELECT * FROM contacts WHERE login_id=? ORDER BY name", (login_id,))]
        notes = [_clean_row(r) for r in c.execute("SELECT * FROM contact_notes WHERE login_id=? ORDER BY id", (login_id,))]
        box = c.execute("SELECT mailbox, greeting_path FROM voicemail_boxes WHERE login_id=?", (login_id,)).fetchone()
        vms = [dict(r) for r in c.execute("SELECT * FROM voicemail_messages WHERE mailbox=? ORDER BY id",
                                          (box["mailbox"],))] if box else []
        fax_in = [dict(r) for r in c.execute(
            "SELECT f.* FROM faxes f JOIN fax_recipients r ON r.fax_id=f.id WHERE r.login_id=? ORDER BY f.id",
            (login_id,))]
        fax_out = [dict(r) for r in c.execute(
            "SELECT * FROM fax_outbox WHERE login_id=? AND status!='deleted' ORDER BY id", (login_id,))]
    calls = ucp._my_calls(me, limit=10 ** 7)
    msgs = ucp._my_messages(me, limit=10 ** 7)
    recs = ucp._recordings_for(me, limit=10 ** 7)

    os.makedirs(EXPORT_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(EXPORT_DIR, f"{login_id}-{secrets.token_hex(8)}.zip")
    counts = {"calls": len(calls), "messages": len(msgs), "voicemail": 0,
              "recordings": 0, "faxes": 0, "contacts": len(contacts)}
    stamp = _fmt(_now())
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        def add_file(src, arc):
            z.write(src, arc, compress_type=zipfile.ZIP_STORED)

        account = {
            "exported_at_utc": stamp,
            "account": _clean_row(me),
            "preferences": _clean_row(prefs) if prefs else {},
            "plan": plan,
            "call_filtering": filters,
            "app_sign_ins": devices,
        }
        z.writestr("account.json", json.dumps(account, indent=2, default=str))
        z.writestr("calls.csv", _csv(
            [dict(r, contact=r.get("other_name", "")) for r in calls],
            ["started_at", "kind", "other", "contact", "src", "dst", "direction",
             "duration_sec", "bill_sec", "disposition"]))
        z.writestr("messages.csv", _csv(
            [dict(r, contact=r.get("peer_name", "")) for r in msgs],
            ["sent_at", "from_ext", "to_ext", "contact", "via_did", "body"]))
        z.writestr("contacts.csv", _csv(contacts, ["name", "number", "org", "email", "created_at", "updated_at"]))
        z.writestr("contact_notes.csv", _csv(notes, ["contact_id", "item_type", "item_at", "body", "created_at"]))

        vm_rows = []
        for v in vms:
            src = _safe_file(v["path"], ucp.VM_ROOT)
            v["file"] = ""
            if src:
                v["file"] = f"voicemail/{v['id']}_{_slug(v['received_at'], 19)}_{_slug(v['caller'])}{os.path.splitext(src)[1]}"
                add_file(src, v["file"])
                counts["voicemail"] += 1
            vm_rows.append(v)
        z.writestr("voicemail.csv", _csv(vm_rows, ["received_at", "caller", "duration_sec", "folder", "is_read", "file"]))
        if box and box["greeting_path"]:
            g = _safe_file(box["greeting_path"], ucp.VMGREET_DIR)
            if g:
                add_file(g, "voicemail/your-greeting" + os.path.splitext(g)[1])

        rec_rows = []
        for r in recs:
            src = _safe_file(r["path"], ucp.REC_ROOT)
            r["file"] = ""
            if src:
                r["file"] = f"recordings/{r['id']}_{_slug(r['started_at'], 19)}_{_slug(r['peer'])}{os.path.splitext(src)[1]}"
                add_file(src, r["file"])
                counts["recordings"] += 1
            rec_rows.append(r)
        z.writestr("recordings.csv", _csv(rec_rows, ["started_at", "direction", "peer", "duration_sec", "file"]))

        fin = []
        for f in fax_in:
            src = _safe_file(f["path"], fax_ui.FAX_DIR)
            f["file"] = ""
            if src:
                f["file"] = f"faxes/received/{f['id']}_{_slug(f['received_at'], 19)}_{_slug(f['caller'])}{os.path.splitext(src)[1]}"
                add_file(src, f["file"])
                counts["faxes"] += 1
            fin.append(f)
        z.writestr("faxes_received.csv", _csv(fin, ["received_at", "did", "caller", "remote_station", "pages", "status", "file"]))
        fout = []
        for f in fax_out:
            src = _safe_file(f.get("pdf_path"), fax_send.out_dir())
            f["file"] = ""
            if src:
                f["file"] = f"faxes/sent/{f['id']}_{_slug(f['created_at'], 19)}_{_slug(f['to_number'])}.pdf"
                add_file(src, f["file"])
                counts["faxes"] += 1
            fout.append(f)
        z.writestr("faxes_sent.csv", _csv(fout, ["created_at", "from_did", "to_number", "to_name", "subject",
                                                  "pages", "status", "sent_at", "file"]))
        z.writestr("README.txt", f"""Your data from {_brand()}
Exported {stamp} UTC for {me['username']} (extension {me['exten'] or 'none'}).

account.json         your account details, settings, plan and call filtering
calls.csv            call history ({counts['calls']} calls)
messages.csv         text messages, SMS/MMS and internal ({counts['messages']}); picture
                     messages show their media links in the body
voicemail.csv        voicemail list; audio files in voicemail/ ({counts['voicemail']})
recordings.csv       call recordings you made; audio in recordings/ ({counts['recordings']})
faxes_received.csv   faxes you received, faxes_sent.csv faxes you sent; files in faxes/
contacts.csv         your contacts ({counts['contacts']}), contact_notes.csv your notes

CSV files open in any spreadsheet app. Times are as stored by the phone system.
Passwords and security keys are not included.
""")
    return path, os.path.getsize(path), counts


def _cleanup():
    """Remove downloads whose link has expired."""
    now = _fmt(_now())
    with M.db() as c:
        rows = c.execute("SELECT id, export_path FROM privacy_requests WHERE export_path!=''"
                         " AND token_expires IS NOT NULL AND token_expires < ?", (now,)).fetchall()
        for r in rows:
            p = _safe_file(r["export_path"], EXPORT_DIR)
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass
            c.execute("UPDATE privacy_requests SET export_path='' WHERE id=?", (r["id"],))
        c.commit()


def _fulfil_export(req_id, base_url, actor):
    """Build the zip, make a new link, email it. Returns (ok, message)."""
    _cleanup()
    with M.db() as c:
        r = c.execute("SELECT * FROM privacy_requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["kind"] != "export":
        return False, "Request not found."
    if not r["login_id"]:
        return False, "That account has been deleted, so there's no data left to send."
    try:
        path, size, counts = build_export(r["login_id"])
    except Exception as e:
        return False, f"Couldn't build the download: {e}"
    old = _safe_file(r["export_path"], EXPORT_DIR)
    if old and old != path:
        try:
            os.remove(old)
        except OSError:
            pass
    tok = secrets.token_urlsafe(32)
    expires = _fmt(_now() + datetime.timedelta(days=LINK_DAYS))
    with M.db() as c:
        c.execute("UPDATE privacy_requests SET export_path=?, export_bytes=?, token_hash=?, token_expires=?,"
                  " status='done', done_at=COALESCE(done_at, datetime('now')), done_by=? WHERE id=?",
                  (path, size, hashlib.sha256(tok.encode()).hexdigest(), expires, actor, req_id))
        c.commit()
    link = f"{base_url}/data-export/{tok}"
    sent = _send(r["email"], f"Your data from {_brand()} is ready",
                 f"Hello,\n\nThe copy of your account data you asked for is ready:\n\n{link}\n\n"
                 f"The link works until {_date(expires)} (UTC) and needs no sign-in, so don't share it.\n"
                 f"It contains: {counts['calls']} calls, {counts['messages']} messages, "
                 f"{counts['voicemail']} voicemails, {counts['recordings']} recordings, "
                 f"{counts['faxes']} faxes and {counts['contacts']} contacts.\n\n"
                 "If you didn't ask for this, contact us.\n")
    _audit(actor, "privacy.export", f"request={req_id} user={r['username']} bytes={size} emailed={int(sent)}")
    if not sent:
        return True, (f"Download built, but the email to {r['email']} didn't send (check the Email page). "
                      "You can download it here and send it yourself.")
    return True, f"Download link emailed to {r['email']}."


# ------------------------------------------------------------ user side

def _open(login_id, kind):
    with M.db() as c:
        return c.execute("SELECT * FROM privacy_requests WHERE login_id=? AND kind=? AND status='pending'"
                         " ORDER BY id DESC LIMIT 1", (login_id, kind)).fetchone()


def _public_page(request):
    contact = _kv("privacy_contact")
    alt = (f" Can't sign in? Email <a href=\"mailto:{esc(contact)}\">{esc(contact)}</a>"
           " from the address on your account." if contact else "")
    body = f"""<h2>Delete your account or get a copy of your data</h2>
{_what_text()}
<p><a class="btn" href="/login?next=/ucp/delete">Sign in to make a request</a></p>
<p class="muted">Signing in proves the account is yours.{alt}</p>"""
    return HTMLResponse(M.page("Delete account", f'<div class="store">{body}</div>'))


def _what_text():
    return f"""<p>You can ask for:</p>
<ul>
<li><b>A copy of your data</b>: we email you a download link (a zip file) with your account details,
call history, text and picture messages, voicemails, call recordings, faxes and contacts.
The link works for {LINK_DAYS} days.</li>
<li><b>Your account to be deleted</b>: the administrator deletes it within {DELETE_DAYS} days.
That removes your login, phone extension, call history, messages, voicemails, recordings, faxes,
contacts and settings. It can't be undone, and your phone number and extension may be given to someone else.
We keep a short record that the request was made and done (your username, email address and dates),
and payment records the law makes us keep.</li>
</ul>"""


def user_page(request: Request):
    import ucp
    s, me = ucp._me(request)
    if not s:
        return _public_page(request)
    _cleanup()
    flash = ""
    k = request.query_params.get("msg")
    if k in FLASH:
        cls, text = FLASH[k]
        flash = f'<div class="flash {cls}">{esc(text)}</div>'
    csrf = M._csrf_field(s)
    email = me.get("vm_email") or ""
    with M.db() as c:
        hist = c.execute("SELECT * FROM privacy_requests WHERE login_id=? ORDER BY id DESC LIMIT 10",
                         (me["id"],)).fetchall()
    pend_exp, pend_del = _open(me["id"], "export"), _open(me["id"], "delete")

    if pend_exp:
        exp_box = (f'<p>You asked for a copy of your data on {_date(pend_exp["created_at"])}. '
                   f'We\'ll email <b>{esc(pend_exp["email"])}</b> a download link by '
                   f'{_date(pend_exp["due_at"])}.</p>')
    else:
        exp_box = f"""<form method="post" action="/ucp/delete/export">{csrf}
<label>Email the download link to<br><input name="email" type="email" required value="{esc(email)}"
 placeholder="you@example.com"></label>
<button class="btn">Request my data</button></form>"""

    if me.get("role") == "admin":
        del_box = '<p class="muted">You\'re an administrator: delete logins on the Logins page.</p>'
    elif pend_del:
        del_box = (f'<p class="warn">You asked for your account to be deleted on {_date(pend_del["created_at"])}. '
                   f'It will be deleted by <b>{_date(pend_del["due_at"])}</b>. We\'ll email '
                   f'{esc(pend_del["email"])} when it\'s done.</p>'
                   f'<form method="post" action="/ucp/delete/cancel">{csrf}'
                   '<button class="btn ghost">Cancel my deletion request</button></form>')
    else:
        del_box = f"""<form method="post" action="/ucp/delete/account">{csrf}
<label>Email address for the confirmation<br><input name="email" type="email" required value="{esc(email)}"
 placeholder="you@example.com"></label>
<label>Why are you leaving? (optional)<br><textarea name="reason" rows="2" maxlength="500"></textarea></label>
<label class="inline-chk"><input type="checkbox" name="with_export" value="1" checked>
 Send me a copy of my data first</label>
<label class="inline-chk"><input type="checkbox" name="ack" value="1" required>
 I understand everything on my account will be permanently deleted</label>
<label>Type <b>DELETE</b> to confirm<br><input name="confirm" autocomplete="off" required
 pattern="[Dd][Ee][Ll][Ee][Tt][Ee]" style="width:160px"></label>
<button class="btn danger">Request account deletion</button></form>"""

    rows = "".join(
        f"<tr><td>{esc(KINDS.get(r['kind'], r['kind']))}</td><td>{_date(r['created_at'])}</td>"
        f"<td>{esc(r['status'].capitalize())}</td><td>{_date(r['done_at']) or '-'}</td></tr>" for r in hist)
    body = f"""{flash}<h2>Delete account or download data</h2>
{_what_text()}
<div class="grid2">
<section class="panel"><h3>Get a copy of your data</h3>{exp_box}</section>
<section class="panel"><h3>Delete my account</h3>{del_box}</section>
</div>
{f'<h3>Your requests</h3><table><tr><th>Request</th><th>Asked</th><th>Status</th><th>Done</th></tr>{rows}</table>' if rows else ''}"""
    return ucp._render(request, s, me, "ucp-set", "Delete account", body)


def _back(msg):
    return RedirectResponse(f"/ucp/delete?msg={msg}", status_code=303)


def _new_request(me, kind, email, reason=""):
    days = DELETE_DAYS if kind == "delete" else EXPORT_DAYS
    with M.db() as c:
        cur = c.execute("INSERT INTO privacy_requests (login_id, username, exten, email, kind, reason, due_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (me["id"], me["username"], me["exten"] or "", email, kind, reason[:500],
                         _fmt(_now() + datetime.timedelta(days=days))))
        c.commit()
        return cur.lastrowid


async def _user_post(request):
    import ucp
    s, me = ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    return s, me, await request.form()


async def user_export(request: Request):
    s, me, f = await _user_post(request)
    email = mailer.clean(f.get("email"))[:200]
    if not EMAIL_RE.match(email):
        return _back("email")
    if _open(me["id"], "export"):
        return _back("dup")
    rid = _new_request(me, "export", email)
    _audit(me["username"], "privacy.request", f"export id={rid}")
    if _kv("privacy_auto_export") == "1" and mailer.configured(_mail_cfg()):
        ok, _ = await run_in_threadpool(_fulfil_export, rid, _base_url(request), "auto")
        if ok:
            return _back("exp_sent")
    await run_in_threadpool(_notify_new, me, "export", email, "")
    return _back("exp_ok")


async def user_delete(request: Request):
    s, me, f = await _user_post(request)
    if me.get("role") == "admin":
        return _back("noacct")
    email = mailer.clean(f.get("email"))[:200]
    if not EMAIL_RE.match(email):
        return _back("email")
    if (f.get("confirm") or "").strip().upper() != "DELETE" or f.get("ack") != "1":
        return _back("confirm")
    if _open(me["id"], "delete"):
        return _back("dup")
    reason = mailer.clean(f.get("reason"))[:500]
    if f.get("with_export") == "1" and not _open(me["id"], "export"):
        eid = _new_request(me, "export", email)
        if _kv("privacy_auto_export") == "1" and mailer.configured(_mail_cfg()):
            await run_in_threadpool(_fulfil_export, eid, _base_url(request), "auto")
    rid = _new_request(me, "delete", email, reason)
    _audit(me["username"], "privacy.request", f"delete id={rid}")
    due = _date(_fmt(_now() + datetime.timedelta(days=DELETE_DAYS)))
    await run_in_threadpool(
        _send, email, f"We got your account deletion request",
        f"Hello,\n\nWe received a request to delete the account {me['username']}"
        f"{' (extension ' + me['exten'] + ')' if me['exten'] else ''} at {_brand()}.\n"
        f"It will be deleted by {due}. Until then you can cancel by signing in and opening "
        f"{_base_url(request)}/ucp/delete\n\nIf you didn't ask for this, sign in and cancel it, "
        "and change your password.\n")
    await run_in_threadpool(_notify_new, me, "delete", email, reason)
    return _back("del_ok")


async def user_cancel(request: Request):
    s, me, f = await _user_post(request)
    r = _open(me["id"], "delete")
    if r:
        with M.db() as c:
            c.execute("UPDATE privacy_requests SET status='cancelled', done_at=datetime('now'), done_by=?"
                      " WHERE id=?", (me["username"], r["id"]))
            c.commit()
        _audit(me["username"], "privacy.cancel", f"delete id={r['id']}")
    return _back("del_cancel")


def _notify_new(me, kind, email, reason):
    to = _admin_to()
    if not to:
        return
    days = DELETE_DAYS if kind == "delete" else EXPORT_DAYS
    _send(to, f"{KINDS[kind]} request from {me['username']}",
          f"{me['username']} (extension {me['exten'] or 'none'}, {email}) asked for: {KINDS[kind].lower()}.\n"
          f"{'Reason: ' + reason + chr(10) if reason else ''}"
          f"Deadline: {days} days. Handle it on the admin Privacy page.\n")


# ------------------------------------------------------------ admin side

def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _aback(msg="", err=""):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/privacy" + ("?" + q if q else ""), status_code=303)


def _size(n):
    n = int(n or 0)
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"


def admin_page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    _cleanup()
    csrf = M._csrf_field(s)
    flash = ""
    if request.query_params.get("msg"):
        flash += f'<div class="flash ok">{esc(request.query_params["msg"])}</div>'
    if request.query_params.get("err"):
        flash += f'<div class="flash bad">{esc(request.query_params["err"])}</div>'
    with M.db() as c:
        pend = c.execute("SELECT p.*, l.role FROM privacy_requests p LEFT JOIN logins l ON l.id=p.login_id"
                         " WHERE p.status='pending' ORDER BY p.due_at").fetchall()
        hist = c.execute("SELECT * FROM privacy_requests WHERE status!='pending' ORDER BY id DESC LIMIT 100").fetchall()
        open_exports = {r["login_id"] for r in pend if r["kind"] == "export"}
    smtp_ok = mailer.configured(_mail_cfg())

    def due_cell(r):
        d = _days_left(r["due_at"])
        if d is None:
            return "-"
        if d < 0:
            return f'<span class="pill bad">Overdue {-d} day(s)</span>'
        return f'{_date(r["due_at"])}<br><span class="{"bad" if d < 7 else "muted"}">{d} day(s) left</span>'

    prow = ""
    for r in pend:
        who = (f"{esc(r['username'])}<br><span class='muted'>ext {esc(r['exten'] or '-')} · "
               f"{esc(r['email'])}</span>" + (f"<br><i>“{esc(r['reason'])}”</i>" if r["reason"] else ""))
        acts = []
        if r["kind"] == "export":
            acts.append(f"<form method='post' action='/privacy/{r['id']}/export' style='display:inline'>{csrf}"
                        f"<button class='btn'>Build &amp; email download</button></form>")
        elif r["login_id"]:
            warn = (" They also asked for their data and it hasn't been sent yet. Send that first?"
                    if r["login_id"] in open_exports else "")
            acts.append(f"<form method='post' action='/privacy/{r['id']}/delete' style='display:inline' "
                        f"onsubmit=\"return confirm('Permanently delete {esc(r['username'])} and all their "
                        f"data?{warn} This cannot be undone.')\">{csrf}"
                        f"<button class='btn danger'>Delete account now</button></form>")
        acts.append(f"<form method='post' action='/privacy/{r['id']}/decline' style='display:inline'>{csrf}"
                    f"<input name='note' placeholder='Reason (emailed to them)' maxlength='300' style='width:200px'>"
                    f"<button class='btn ghost'>Decline</button></form>")
        prow += (f"<tr><td>{esc(KINDS.get(r['kind'], r['kind']))}</td><td>{who}</td>"
                 f"<td>{_date(r['created_at'])}</td><td>{due_cell(r)}</td><td>{' '.join(acts)}</td></tr>")
    pend_html = (f"<table><tr><th>Request</th><th>User</th><th>Asked</th><th>Due</th><th></th></tr>{prow}</table>"
                 if prow else "<p class='muted'>No open requests.</p>")

    hrow = ""
    for r in hist:
        extra = ""
        if r["kind"] == "export" and r["status"] == "done":
            if r["export_path"]:
                extra = (f"{_size(r['export_bytes'])}, link until {_date(r['token_expires'])}, "
                         f"downloaded {r['downloads']}× "
                         f"<a href='/privacy/{r['id']}/file'>Download</a>")
            else:
                extra = "link expired"
            if r["login_id"]:
                extra += (f" <form method='post' action='/privacy/{r['id']}/export' style='display:inline'>{csrf}"
                          f"<button class='btn ghost'>Rebuild &amp; email again</button></form>")
        elif r["admin_note"]:
            extra = esc(r["admin_note"])
        hrow += (f"<tr><td>{esc(KINDS.get(r['kind'], r['kind']))}</td><td>{esc(r['username'])}"
                 f"<br><span class='muted'>{esc(r['email'])}</span></td><td>{_date(r['created_at'])}</td>"
                 f"<td>{esc(r['status'].capitalize())}{' by ' + esc(r['done_by']) if r['done_by'] else ''}"
                 f"<br><span class='muted'>{_date(r['done_at'])}</span></td><td>{extra}</td></tr>")
    hist_html = (f"<table><tr><th>Request</th><th>User</th><th>Asked</th><th>Result</th><th></th></tr>{hrow}</table>"
                 if hrow else "<p class='muted'>Nothing yet.</p>")

    link = f"{_base_url(request)}/ucp/delete"
    smtp_warn = ("" if smtp_ok else '<div class="flash bad">Email isn&#39;t set up, so download links and '
                 'confirmations can&#39;t be sent. Set it up on the <a href="/email">Email page</a>.</div>')
    body = f"""{flash}<h2>Privacy requests</h2>
<p class="muted">Users ask here: <a href="/ucp/delete">{esc(link)}</a> (also the account deletion link
for app store listings). Account deletions are due within {DELETE_DAYS} days, data copies within
{EXPORT_DAYS}. Download links last {LINK_DAYS} days.</p>
{smtp_warn}
<h3>Open</h3>{pend_html}
<h3>Done</h3>{hist_html}
<section class="panel" style="margin-top:20px"><h3>Settings</h3>
<form method="post" action="/privacy/settings">{csrf}
<label>Contact email (shown to people who can't sign in; gets new-request alerts)<br>
<input name="contact" type="email" value="{esc(_kv('privacy_contact'))}" placeholder="privacy@example.com"></label>
<label class="switch"><input type="checkbox" name="auto" value="1" {'checked' if _kv('privacy_auto_export') == '1' else ''}>
<span>Build and email data downloads straight away when someone asks (deletions always wait for you)</span></label>
<button class="btn">Save</button></form></section>"""
    return HTMLResponse(M.page("Privacy", body, s["username"], s["role"], "privacy"))


async def _admin_post(request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    return s, await request.form()


async def admin_export(request: Request, rid: int):
    s, _ = await _admin_post(request)
    ok, msg = await run_in_threadpool(_fulfil_export, rid, _base_url(request), s["username"])
    return _aback(msg=msg) if ok else _aback(err=msg)


async def admin_delete(request: Request, rid: int):
    s, _ = await _admin_post(request)
    if M._admin_locked():
        return M._panel_locked(s, "privacy")
    with M.db() as c:
        r = c.execute("SELECT * FROM privacy_requests WHERE id=? AND kind='delete' AND status='pending'",
                      (rid,)).fetchone()
        t = c.execute("SELECT * FROM logins WHERE id=?", (r["login_id"],)).fetchone() if r and r["login_id"] else None
    if not r:
        return _aback(err="Request not found.")
    if t and (t["role"] == "admin" or int(t["id"]) == int(s.get("id") or -1)):
        return _aback(err="That's an administrator login. Delete it on the Logins page.")
    exten, purged = ("", {})
    if t:
        lid = int(t["id"])
        exten, purged = M.wipe_login(lid)
        M.drop_sessions(lid)
        M._best_effort_apply()
    with M.db() as c:
        c.execute("UPDATE privacy_requests SET status='done', done_at=datetime('now'), done_by=? WHERE id=?",
                  (s["username"], rid))
        # Any other open request for the account is moot now.
        c.execute("UPDATE privacy_requests SET status='cancelled', done_at=datetime('now'), done_by=?,"
                  " admin_note='account deleted' WHERE status='pending' AND login_id IS NULL AND username=?",
                  (s["username"], r["username"]))
        c.commit()
    _audit(s["username"], "privacy.delete",
           f"request={rid} user={r['username']} exten={exten or '-'} purged=" +
           ", ".join(f"{k}={v}" for k, v in purged.items()))
    sent = await run_in_threadpool(
        _send, r["email"], "Your account has been deleted",
        f"Hello,\n\nAs you asked, the account {r['username']} at {_brand()} and its data have been deleted.\n\n"
        "Thanks for using it.\n")
    return _aback(msg=f"Deleted {r['username']}." + ("" if sent else " (The confirmation email didn't send.)"))


async def admin_decline(request: Request, rid: int):
    s, f = await _admin_post(request)
    note = mailer.clean(f.get("note"))[:300]
    with M.db() as c:
        r = c.execute("SELECT * FROM privacy_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
        if not r:
            return _aback(err="Request not found.")
        c.execute("UPDATE privacy_requests SET status='declined', admin_note=?, done_at=datetime('now'), done_by=?"
                  " WHERE id=?", (note, s["username"], rid))
        c.commit()
    _audit(s["username"], "privacy.decline", f"request={rid} user={r['username']}")
    await run_in_threadpool(
        _send, r["email"], f"About your {KINDS[r['kind']].lower()} request",
        f"Hello,\n\nWe couldn't complete your {KINDS[r['kind']].lower()} request for {r['username']}."
        f"{chr(10) + chr(10) + 'Reason: ' + note if note else ''}\n\nReply to this email if you have questions.\n")
    return _aback(msg="Declined.")


async def admin_settings(request: Request):
    s, f = await _admin_post(request)
    contact = mailer.clean(f.get("contact"))[:200]
    if contact and not EMAIL_RE.match(contact):
        return _aback(err="The contact email doesn't look valid.")
    _set("privacy_contact", contact)
    _set("privacy_auto_export", "1" if f.get("auto") else "0")
    _audit(s["username"], "privacy.settings", f"contact={contact} auto={int(bool(f.get('auto')))}")
    return _aback(msg="Saved.")


def admin_file(request: Request, rid: int):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    with M.db() as c:
        r = c.execute("SELECT * FROM privacy_requests WHERE id=?", (rid,)).fetchone()
    p = _safe_file(r["export_path"], EXPORT_DIR) if r else ""
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type="application/zip",
                        filename=f"data-{_slug(r['username'])}-{_date(r['done_at'])}.zip")


# ------------------------------------------------------------ download link

_hits = {}


def download(request: Request, token: str):
    ip = M.client_ip(request) if hasattr(M, "client_ip") else "?"
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < 3600]
    if len(hits) >= 30:
        raise HTTPException(429)
    _hits[ip] = hits + [now]
    _cleanup()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token or ""):
        raise HTTPException(404)
    h = hashlib.sha256(token.encode()).hexdigest()
    with M.db() as c:
        r = c.execute("SELECT * FROM privacy_requests WHERE token_hash=? AND kind='export'", (h,)).fetchone()
        p = _safe_file(r["export_path"], EXPORT_DIR) if r else ""
        if r and p and (r["token_expires"] or "") > _fmt(_now()):
            c.execute("UPDATE privacy_requests SET downloads=downloads+1 WHERE id=?", (r["id"],))
            c.commit()
        else:
            p = ""
    if not p:
        return HTMLResponse(M.page("Link expired",
                                   '<div class="store"><h2>This download link has expired</h2>'
                                   '<p class="muted">Links work for a limited time. Sign in and ask again at '
                                   '<a href="/ucp/delete">/ucp/delete</a>.</p></div>'), status_code=404)
    resp = FileResponse(p, media_type="application/zip",
                        filename=f"my-data-{_slug(r['username'])}-{_date(r['done_at'])}.zip")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def install(app_module):
    global M
    M = app_module
    app = M.app
    html = dict(response_class=HTMLResponse)
    app.get("/ucp/delete", **html)(user_page)
    app.post("/ucp/delete/export")(user_export)
    app.post("/ucp/delete/account")(user_delete)
    app.post("/ucp/delete/cancel")(user_cancel)
    app.get("/privacy", **html)(admin_page)
    app.post("/privacy/settings")(admin_settings)
    app.post("/privacy/{rid}/export")(admin_export)
    app.post("/privacy/{rid}/delete")(admin_delete)
    app.post("/privacy/{rid}/decline")(admin_decline)
    app.get("/privacy/{rid}/file")(admin_file)
    app.get("/data-export/{token}")(download)
