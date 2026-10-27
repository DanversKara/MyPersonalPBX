# SPDX-License-Identifier: GPL-2.0-or-later
"""Fax pages. Receiving happens in Asterisk + pbx-brain (pbx-brain/fax.py).

Admin   Routes -> a number -> "Detect faxes" settings (route_fields / parse)
        /faxes                   every fax: view, download, delete
User    /ucp/faxes               faxes on numbers that ring them
Both    /api/fax/{id}            the PDF (admin, or a recipient of that fax)
"""
import os
import re
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

M = None
FAX_DIR = os.environ.get("PBX_FAX_DIR", "/var/spool/pbx/fax")
WAIT_CHOICES = (3, 4, 5, 6, 8)
EMAIL_RE = re.compile(r"^[^@\s,;]{1,64}@[^@\s,;]+\.[A-Za-z]{2,}$")


def esc(x):
    return M.esc(x)


def fax_ready():
    """True / False: Asterisk has res_fax_spandsp; None if unknown."""
    try:
        return M._asterisk_status().get("fax_ready")
    except Exception:
        return None


# ------------------------------------------------------------------ route settings

def route_fields(t):
    """The 'Detect faxes' part of the inbound route form."""
    t = dict(t) if t else {}
    on = bool(t.get("fax_detect"))
    wait = int(t.get("fax_wait") or 4)
    users = t.get("fax_email_users", 1)
    ready = fax_ready()
    warn = ""
    if ready is False:
        warn = ('<p class="bad" style="margin:6px 0">This Asterisk was built without fax support yet. '
                'Run <code>pbx-update --with-fax</code> on the PBX when the phones are quiet '
                '(about 15 minutes, then a short restart). Until then this setting does nothing.</p>')
    opts = "".join(f'<option value="{w}" {"selected" if w == wait else ""}>{w} seconds</option>' for w in WAIT_CHOICES)
    return f"""<fieldset class="panel" style="margin:12px 0"><legend><b>Fax</b></legend>
<label><input type="checkbox" name="fax_detect" value="1" {'checked' if on else ''}> Detect faxes on this number</label>
<p class="muted" style="margin:4px 0">Calls to this number are answered right away and the caller hears ringing while the PBX
listens for a fax machine's tone. A fax is received and saved as a PDF; anything else carries on exactly as below
(ring, menu, voicemail). No separate fax number needed.</p>
{warn}
<label>Listen for<br><select name="fax_wait">{opts}</select></label>
<p class="muted" style="margin:4px 0">How long callers wait before it rings through. 4 seconds catches nearly every fax machine.</p>
<label>Also email faxes to (optional)<br><input name="fax_email" value="{esc(t.get('fax_email') or '')}" placeholder="office@example.com, me@example.com" size="40"></label>
<label><input type="checkbox" name="fax_email_users" value="1" {'checked' if users else ''}> Email them to the people this number rings (their notification email)</label>
<p class="muted" style="margin:4px 0">Those people also see the faxes in My Phone &rarr; Faxes; every fax is under <a href="/faxes">Faxes</a>.</p>
</fieldset>"""


def parse_route_fields(form):
    """(dict of route columns, error)."""
    on = 1 if form.get("fax_detect") else 0
    try:
        wait = int(form.get("fax_wait") or 4)
    except ValueError:
        wait = 4
    if wait not in WAIT_CHOICES:
        wait = 4
    emails = [e.strip() for e in re.split(r"[,;\s]+", form.get("fax_email") or "") if e.strip()]
    bad = [e for e in emails if not EMAIL_RE.match(e)]
    if bad:
        return None, f"Fax email: \"{bad[0]}\" doesn't look like an email address."
    return {"fax_detect": on, "fax_wait": wait, "fax_email": ", ".join(emails[:5]),
            "fax_email_users": 1 if form.get("fax_email_users") else 0}, ""


def save_route_fields(c, route_id, vals):
    c.execute("UPDATE inbound_routes SET fax_detect=?, fax_wait=?, fax_email=?, fax_email_users=? WHERE id=?",
              (vals["fax_detect"], vals["fax_wait"], vals["fax_email"], vals["fax_email_users"], route_id))


# ------------------------------------------------------------------ access

def _fax(fid):
    with M.db() as c:
        r = c.execute("SELECT * FROM faxes WHERE id=?", (fid,)).fetchone()
    return dict(r) if r else None


def _is_recipient(login_id, fid):
    with M.db() as c:
        return c.execute("SELECT 1 FROM fax_recipients WHERE fax_id=? AND login_id=?",
                         (fid, login_id)).fetchone() is not None


def show_tab(me):
    """Faxes tab (web and app): they have faxes, a fax number rings them, or
    they have a number to send from. Shown even before the PBX is ready to
    send, so the page can say what's missing instead of the tab being absent."""
    try:
        return bool(user_has_faxes(me) or M.fax_send.allowed_numbers(me))
    except Exception:
        return False


def user_has_faxes(me):
    """Show My Phone -> Faxes: they have faxes, or a fax number rings them."""
    try:
        with M.db() as c:
            if c.execute("SELECT 1 FROM fax_recipients WHERE login_id=? LIMIT 1", (me["id"],)).fetchone():
                return True
            ext = me["exten"] or ""
            if not ext:
                return False
            for r in c.execute("SELECT ring_extens, group_id FROM inbound_routes WHERE fax_detect=1 AND enabled=1"):
                if f'"{ext}"' in (r["ring_extens"] or ""):
                    return True
                if r["group_id"]:
                    g = c.execute("SELECT members FROM ring_groups WHERE id=?", (r["group_id"],)).fetchone()
                    if g and f'"{ext}"' in (g["members"] or ""):
                        return True
    except Exception:
        return False
    return False


def unseen_count(me):
    try:
        with M.db() as c:
            return c.execute("SELECT COUNT(*) FROM fax_recipients r JOIN faxes f ON f.id=r.fax_id"
                             " WHERE r.login_id=? AND r.seen=0 AND f.status='ok'", (me["id"],)).fetchone()[0]
    except Exception:
        return 0


def _safe_path(p):
    real = os.path.realpath(p or "")
    return real if p and real.startswith(os.path.realpath(FAX_DIR) + os.sep) and os.path.isfile(real) else ""


def serve(request: Request, fid: int):
    """The fax PDF, inline (or ?download=1). Web session or the app's token."""
    s, _me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    f = _fax(fid)
    if not f:
        raise HTTPException(404)
    if s["role"] != "admin" and not _is_recipient(s["id"], fid):
        raise HTTPException(404)
    path = _safe_path(f["path"])
    if not path:
        raise HTTPException(404, "file missing")
    if s["role"] != "admin" or _is_recipient(s["id"], fid):
        with M.db() as c:
            c.execute("UPDATE fax_recipients SET seen=1 WHERE fax_id=? AND login_id=?", (fid, s["id"]))
            c.commit()
    pdf = path.endswith(".pdf")
    name = f"fax-{f['received_at'][:10]}-{fid}.{'pdf' if pdf else 'tif'}"
    disp = "attachment" if request.query_params.get("download") else "inline"
    return FileResponse(path, media_type="application/pdf" if pdf else "image/tiff",
                        headers={"Content-Disposition": f'{disp}; filename="{name}"'})


def _delete_files(rows):
    for r in rows:
        p = _safe_path(r["path"])
        if p:
            try:
                os.remove(p)
            except OSError:
                pass


# ------------------------------------------------------------------ rendering

def _rows(faxes, admin, csrf_form):
    out = []
    for f in faxes:
        who = esc(f.get("caller_name") or f["caller"] or "Unknown")
        if f.get("caller_name") and f["caller"]:
            who += f'<br><span class="muted">{esc(f["caller"])}</span>'
        if f["remote_station"]:
            who += f'<br><span class="muted">Fax ID: {esc(f["remote_station"])}</span>'
        if f["status"] == "ok" and f["path"]:
            st = (f'<a class="btn ghost" href="/api/fax/{f["id"]}" target="_blank">View</a> '
                  f'<a class="btn ghost" href="/api/fax/{f["id"]}?download=1">Download</a>')
        elif f["status"] == "ok":
            st = '<span class="muted">Deleted</span>'
        else:
            st = f'<span class="pill bad">Not received</span><br><span class="muted">{esc(f["error"] or "")}</span>'
        dot = '<span class="dot"></span>' if f.get("seen") == 0 and f["status"] == "ok" else ""
        extra = f'<td>{esc(f.get("for") or "-")}</td>' if admin else ""
        out.append(f"<tr><td><input type='checkbox' class='sel' name='ids' value='{f['id']}' form='{csrf_form}'"
                   f" aria-label='Select'></td><td>{dot}{esc(M.fmt_ts(f['received_at']))}</td>"
                   f"<td>{esc(f['did'])}</td><td>{who}</td><td>{f['pages'] or '-'}</td>{extra}<td>{st}</td></tr>")
    return "".join(out)


def admin_page(request: Request):
    s = M._sess(request)
    if not s or s["role"] != "admin":
        return RedirectResponse("/login")
    with M.db() as c:
        faxes = [dict(r) for r in c.execute("SELECT * FROM faxes ORDER BY id DESC LIMIT 500")]
        total = c.execute("SELECT COUNT(*) FROM faxes").fetchone()[0]
        names = {}
        for r in c.execute("SELECT r.fax_id, COALESCE(NULLIF(l.display_name,''), l.username) AS n, l.exten"
                           " FROM fax_recipients r JOIN logins l ON l.id=r.login_id"):
            names.setdefault(r["fax_id"], []).append(f'{r["n"]} ({r["exten"]})' if r["exten"] else r["n"])
        on = c.execute("SELECT did FROM inbound_routes WHERE fax_detect=1 AND enabled=1 ORDER BY did").fetchall()
    for f in faxes:
        f["for"] = ", ".join(names.get(f["id"], []))
    csrf = M._csrf_field(s)
    ready = fax_ready()
    notes = []
    if ready is False:
        notes.append('<div class="flash bad">This Asterisk can\'t receive faxes yet: run <code>pbx-update --with-fax</code> '
                     'on the PBX when the phones are quiet (about 15 minutes, then a short restart).</div>')
    nums = ", ".join(esc(r["did"]) for r in on) or "none yet"
    msg = request.query_params.get("deleted")
    if msg and msg.isdigit():
        notes.append(f'<div class="flash ok">Deleted {int(msg)} fax{"es" if int(msg) != 1 else ""}.</div>')
    body = f"""{''.join(notes)}<h2>Faxes</h2>
<p class="muted">Faxes received on your ordinary numbers. Numbers listening for faxes: <b>{nums}</b>
(turn it on per number under <a href="/routes">Routes</a> &rarr; Edit &rarr; Detect faxes). Faxes are kept until deleted.</p>
<form id="faxdel" method="post" action="/faxes/delete">{csrf}</form>
<div class="bulkbar"><button class="btn ghost" form="faxdel">Delete selected</button>
<button class="btn ghost danger" form="faxdel" name="all" value="1" onclick="return confirm('Delete all {total} faxes?')">Delete all</button></div>
<div class="scrollbox"><table><tr><th><input type="checkbox" id="faxdel-all" aria-label="Select all"></th><th>Received</th><th>Number</th><th>From</th><th>Pages</th><th>For</th><th></th></tr>
{_rows(faxes, True, "faxdel") or '<tr><td colspan="7" class="muted">No faxes yet.</td></tr>'}</table></div>
{M.ucp._bulk_js("faxdel")}{_admin_sent()}"""
    return HTMLResponse(M.page("Faxes", body, s["username"], s["role"], "faxes"))


def _admin_sent():
    with M.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT f.*, f.pdf_path != '' AS has_pdf, COALESCE(NULLIF(l.display_name,''), l.username) AS who, l.exten"
            " FROM fax_outbox f JOIN logins l ON l.id=f.login_id WHERE f.status != 'deleted' ORDER BY f.id DESC LIMIT 200")]
    tr = "".join(
        f"<tr><td>{esc(M.fmt_ts(r['created_at']))}</td><td>{esc(r['who'])} ({esc(r['exten'])})</td>"
        f"<td>{esc(r['to_number'])}<br><span class='muted'>from {esc(r['from_did'])}</span></td><td>{r['pages']}</td>"
        f"<td>{_sent_status(r)}</td><td>{('<a class=btn href=/ucp/faxes/sent/' + str(r['id']) + ' target=_blank>View</a>') if r['has_pdf'] else ''}</td></tr>"
        for r in rows)
    return (f'<h3 style="margin-top:22px">Sent</h3><div class="scrollbox"><table><tr><th>Queued</th><th>By</th><th>To</th>'
            f'<th>Pages</th><th>Status</th><th></th></tr>{tr or "<tr><td colspan=6 class=muted>Nothing sent yet.</td></tr>"}</table></div>')


async def admin_delete(request: Request):
    s = M._sess(request)
    if not s or s["role"] != "admin":
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "faxes")
    f = await request.form()
    with M.db() as c:
        if f.get("all"):
            rows = c.execute("SELECT id, path FROM faxes").fetchall()
        else:
            ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
            rows = [r for i in ids for r in c.execute("SELECT id, path FROM faxes WHERE id=?", (i,))]
        _delete_files(rows)
        for r in rows:
            c.execute("DELETE FROM fax_recipients WHERE fax_id=?", (r["id"],))
            c.execute("DELETE FROM faxes WHERE id=?", (r["id"],))
        c.commit()
    M._audit_log(s["username"], "faxes.delete", "all" if f.get("all") else f"{len(rows)} faxes")
    return RedirectResponse(f"/faxes?deleted={len(rows)}", status_code=303)


def user_page(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        return RedirectResponse("/login")
    with M.db() as c:
        faxes = [dict(r) for r in c.execute(
            "SELECT f.*, r.seen FROM faxes f JOIN fax_recipients r ON r.fax_id=f.id"
            " WHERE r.login_id=? ORDER BY f.id DESC LIMIT 300", (me["id"],))]
    try:
        names = M.contacts.names_for(me["id"])
        for f in faxes:
            f["caller_name"] = names.get(M.contacts.norm_number(f["caller"] or ""), "")
    except Exception:
        pass
    csrf = M._csrf_field(s)
    can, why = M.fax_send.send_status(me)
    nums = M.fax_send.allowed_numbers(me)
    flash = ""
    if request.query_params.get("sent"):
        flash = '<div class="flash ok">Fax queued. It\'s sent in a moment; the status is below (busy lines are retried).</div>'
    if request.query_params.get("err"):
        flash = f'<div class="flash bad">{esc(request.query_params["err"])}</div>'
    if can:
        opts = "".join(f'<option value="{esc(n)}">{esc(n)}</option>' for n in nums)
        send = f"""<details class="panel" {'open' if request.query_params.get("err") else ''}><summary><b>Send a fax</b></summary>
<form method="post" action="/ucp/faxes/send" enctype="multipart/form-data">{csrf}
<label>File (PDF or picture)<br><input type="file" name="file" accept=".pdf,image/*" required></label>
<label>Fax number<br><input name="to_number" inputmode="tel" placeholder="e.g. 3105550100" required></label>
<label>Send from<br><select name="from_did">{opts}</select></label>
<label><input type="checkbox" name="cover" value="1" checked> Add a cover page</label>
<label>To (name)<br><input name="to_name" maxlength="60"></label>
<label>Subject<br><input name="subject" maxlength="80"></label>
<label>Message on the cover<br><textarea name="note" rows="3" maxlength="2000"></textarea></label>
<button class="btn">Send fax</button>
<p class="muted">Uses outside-call minutes from your plan. Busy or unanswered lines are tried again (3 tries).</p>
</form></details>"""
    else:
        send = f'<p class="muted">Sending: {esc(why)}</p>'
    sent = M.fax_send.sent_for(me)
    srows = "".join(
        f"<tr><td><input type='checkbox' class='sel' name='ids' value='{r['id']}' form='usent' aria-label='Select'></td>"
        f"<td>{esc(M.fmt_ts(r['created_at']))}</td><td>{esc(r['to_name'] + ' ' if r['to_name'] else '')}{esc(r['to_number'])}"
        f"<br><span class='muted'>from {esc(r['from_did'])}</span></td><td>{r['pages']}</td>"
        f"<td>{_sent_status(r)}</td><td>{('<a class=\'btn ghost\' href=\'/ucp/faxes/sent/' + str(r['id']) + '\' target=\'_blank\'>View</a>') if r['has_pdf'] else ''}</td></tr>"
        for r in sent)
    sent_html = f"""<h3 style="margin-top:20px">Sent</h3>
<form id="usent" method="post" action="/ucp/faxes/sent/delete">{csrf}</form>
<div class="bulkbar"><button class="btn ghost" form="usent">Cancel / delete selected</button></div>
<div class="scrollbox"><table><tr><th><input type="checkbox" id="usent-all" aria-label="Select all"></th><th>Queued</th><th>To</th><th>Pages</th><th>Status</th><th></th></tr>
{srows or '<tr><td colspan="6" class="muted">Nothing sent yet.</td></tr>'}</table></div>{M.ucp._bulk_js("usent")}"""
    body = f"""{flash}{send}<h3 style="margin-top:16px">Received</h3>
<p class="muted">Faxes sent to the phone numbers that ring you. Tap View to open one; they're kept until you delete them.</p>
<form id="ufax" method="post" action="/ucp/faxes/delete">{csrf}</form>
<div class="bulkbar"><button class="btn ghost" form="ufax">Delete selected</button></div>
<div class="scrollbox"><table><tr><th><input type="checkbox" id="ufax-all" aria-label="Select all"></th><th>Received</th><th>Number</th><th>From</th><th>Pages</th><th></th></tr>
{_rows(faxes, False, "ufax") or '<tr><td colspan="6" class="muted">No faxes yet.</td></tr>'}</table></div>
{M.ucp._bulk_js("ufax")}{sent_html}"""
    return M.ucp._render(request, s, me, "ucp-fax", "Faxes", body)


SENT_LABEL = {"queued": ("Waiting to send", "warn"), "sending": ("Sending…", "warn"),
              "retry": ("Will try again", "warn"), "sent": ("Sent", "ok"), "failed": ("Failed", "bad")}


def _sent_status(r):
    label, cls = SENT_LABEL.get(r["status"], (r["status"], ""))
    extra = ""
    if r["status"] in ("retry", "failed") and r["last_error"]:
        extra = f'<br><span class="muted">{esc(r["last_error"])}</span>'
    if r["status"] == "retry":
        extra += f'<br><span class="muted">try {r["attempts"]} of {M.fax_send.MAX_ATTEMPTS_HINT}</span>'
    return f'<span class="pill {cls}">{esc(label)}</span>{extra}'


async def user_send(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return RedirectResponse("/ucp/faxes?err=" + urllib.parse.quote("Changes are disabled right now."), status_code=303)
    f = await request.form()
    up = f.get("file")
    data = await up.read(M.fax_send.MAX_UPLOAD + 1) if hasattr(up, "read") else b""
    fid, err = M.fax_send.queue(me, data, getattr(up, "filename", "") or "", f.get("to_number") or "",
                                f.get("from_did") or "", to_name=f.get("to_name") or "",
                                subject=f.get("subject") or "", note=f.get("note") or "",
                                cover=bool(f.get("cover")))
    if err:
        return RedirectResponse("/ucp/faxes?err=" + urllib.parse.quote(err), status_code=303)
    M.ucp._audit(me, "fax.send", f"id={fid} to={f.get('to_number')}")
    return RedirectResponse("/ucp/faxes?sent=1", status_code=303)


async def user_sent_delete(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    f = await request.form()
    ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
    M.fax_send.cancel_or_delete(me, ids)
    return RedirectResponse("/ucp/faxes", status_code=303)


def sent_view(request: Request, fid: int):
    """The PDF of a fax this login sent (admins: any)."""
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    p = M.fax_send.sent_pdf(me, fid, admin=s["role"] == "admin")
    if not p:
        raise HTTPException(404)
    disp = "attachment" if request.query_params.get("download") else "inline"
    return FileResponse(p, media_type="application/pdf",
                        headers={"Content-Disposition": f'{disp}; filename="fax-sent-{fid}.pdf"'})


# ------------------------------------------------------------------ the app (JSON)

def _received_json(me):
    with M.db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT f.id, f.did, f.caller, f.remote_station, f.pages, f.status, f.error, f.received_at,"
            " f.path != '' AS has_pdf, r.seen FROM faxes f JOIN fax_recipients r ON r.fax_id=f.id"
            " WHERE r.login_id=? ORDER BY f.id DESC LIMIT 300", (me["id"],))]
    try:
        names = M.contacts.names_for(me["id"])
        for r in rows:
            r["caller_name"] = names.get(M.contacts.norm_number(r["caller"] or ""), "")
    except Exception:
        pass
    for r in rows:
        r["has_pdf"] = bool(r["has_pdf"])
        r["seen"] = bool(r["seen"])
    return rows


def latest_received_id(me):
    try:
        with M.db() as c:
            return c.execute("SELECT COALESCE(MAX(r.fax_id), 0) FROM fax_recipients r JOIN faxes f ON f.id=r.fax_id"
                             " WHERE r.login_id=? AND f.status='ok'", (me["id"],)).fetchone()[0]
    except Exception:
        return 0


def api_list(request: Request):
    me = M.app_api._need(request)
    can, why = M.fax_send.send_status(me)
    sent = M.fax_send.sent_for(me)
    for r in sent:
        r["has_pdf"] = bool(r["has_pdf"])
    return {"received": _received_json(me), "sent": sent, "numbers": M.fax_send.allowed_numbers(me),
            "can_send": can, "why_not": why, "max_mb": M.fax_send.MAX_UPLOAD // (1024 * 1024)}


async def api_send(request: Request):
    me = M.app_api._need(request)
    if M.ucp._locked():
        return {"ok": False, "code": "locked", "message": "Changes are disabled right now."}
    f = await request.form()
    up = f.get("file")
    data = await up.read(M.fax_send.MAX_UPLOAD + 1) if hasattr(up, "read") else b""
    fid, err = M.fax_send.queue(me, data, getattr(up, "filename", "") or "", f.get("to_number") or "",
                                f.get("from_did") or "", to_name=f.get("to_name") or "",
                                subject=f.get("subject") or "", note=f.get("note") or "",
                                cover=(f.get("cover") or "1") == "1")
    if err:
        return {"ok": False, "code": "invalid", "message": err}
    M.ucp._audit(me, "fax.send", f"id={fid} to={f.get('to_number')} (app)")
    return {"ok": True, "code": "ok", "message": "Fax queued. Busy lines are tried again.", "id": fid}


async def api_delete(request: Request):
    me = M.app_api._need(request)
    f = await request.form()
    ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
    _remove_received(me, ids)
    return {"ok": True, "code": "ok", "message": f"{len(ids)} fax(es) deleted."}


async def api_sent_delete(request: Request):
    me = M.app_api._need(request)
    f = await request.form()
    ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
    n = M.fax_send.cancel_or_delete(me, ids)
    return {"ok": True, "code": "ok", "message": f"{n} fax(es) removed."}


async def user_delete(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return RedirectResponse("/ucp/faxes", status_code=303)
    f = await request.form()
    ids = [int(x) for x in f.getlist("ids") if str(x).isdigit()]
    _remove_received(me, ids)
    M.ucp._audit(me, "fax.delete", f"{len(ids)} faxes")
    return RedirectResponse("/ucp/faxes", status_code=303)


def _remove_received(me, ids):
    with M.db() as c:
        for i in ids:
            c.execute("DELETE FROM fax_recipients WHERE fax_id=? AND login_id=?", (i, me["id"]))
        # Nobody has it any more: the file goes too (the admin list keeps the record).
        rows = [r for i in ids for r in c.execute(
            "SELECT id, path FROM faxes WHERE id=? AND NOT EXISTS (SELECT 1 FROM fax_recipients WHERE fax_id=?)", (i, i))]
        _delete_files(rows)
        for r in rows:
            c.execute("UPDATE faxes SET path='', bytes=0, error='Deleted by the recipient' WHERE id=?", (r["id"],))
        c.commit()


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.get("/faxes", response_class=HTMLResponse)(admin_page)
    app.post("/faxes/delete")(admin_delete)
    app.get("/ucp/faxes", response_class=HTMLResponse)(user_page)
    app.post("/ucp/faxes/delete")(user_delete)
    app.get("/api/fax/{fid}")(serve)
    app.post("/ucp/faxes/send")(user_send)
    app.post("/ucp/faxes/sent/delete")(user_sent_delete)
    app.get("/ucp/faxes/sent/{fid}")(sent_view)
    app.get("/api/v1/me/faxes")(api_list)
    app.post("/api/v1/me/faxes/send")(api_send)
    app.post("/api/v1/me/faxes/delete")(api_delete)
    app.post("/api/v1/me/faxes/sent/delete")(api_sent_delete)
    app.get("/api/v1/me/faxes/{fid}/pdf")(serve)
    app.get("/api/v1/me/faxes/sent/{fid}/pdf")(sent_view)
