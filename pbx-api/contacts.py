# SPDX-License-Identifier: GPL-2.0-or-later
"""Contacts, caller-name matching, notes and the unified timeline (/ucp/contacts).

Private per login (like blocked_numbers): every row is keyed by login_id
with ON DELETE CASCADE, and names resolve per user, so two users can name
the same number differently. Numbers are matched on the shared canonical
form (shared/phone.py: digits, 11-digit NANP -> 10 digits), the same form
the block list uses.

Notes survive the 90-day CDR purge: they attach to the contact (and number),
not to the cdr row, and carry a snapshot (item_type / item_at) of whatever
they were written about. When a voicemail or recording is deleted, notes
pointing at it go with it (delete hooks in ucp.py).

The JSON API (app_api.py) and these web handlers share every helper here.
"""
import csv
import io
import re
import time

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from phone import norm_number

M = None  # the app module, set by install()

# Display limits: keep pages and payloads small.
CONTACT_PAGE_LIMIT = 2000
TIMELINE_LIMIT = 500

NOTE_TYPES = ("call", "message", "voicemail", "recording")


# ---------------------------------------------------------------- name lookup

def names_for(login_id) -> dict:
    """{number_norm: display name} for this login's contacts."""
    with M.db() as c:
        return {r["number_norm"]: r["name"] for r in c.execute(
            "SELECT number_norm, name FROM contacts WHERE login_id=? AND name<>''",
            (login_id,)) if r["number_norm"]}


def contact_name(login_id, number) -> str:
    """Display name for `number` (any raw form: 'PSTN <num>' works too)."""
    n = norm_number(number)
    if not n:
        return ""
    with M.db() as c:
        r = c.execute("SELECT name FROM contacts WHERE login_id=? AND number_norm=?",
                      (login_id, n)).fetchone()
    return r["name"] if r else ""


def with_names(login_id, rows, *keys):
    """Copy of `rows` with a '<key>_name' added for each raw-number key."""
    names = names_for(login_id)
    out = []
    for r in rows:
        r = dict(r)
        for k in keys:
            n = norm_number(r.get(k) or "")
            r[k + "_name"] = names.get(n, "")
        out.append(r)
    return out


# ---------------------------------------------------------------- CRUD

CONTACT_COLS = ("id", "name", "number", "number_norm", "org", "email",
                "created_at", "updated_at")


def contacts_for(login_id, limit=CONTACT_PAGE_LIMIT, q=""):
    with M.db() as c:
        if q:
            like = "%" + q.replace("%", "").replace("_", "") + "%"
            rows = c.execute(
                "SELECT * FROM contacts WHERE login_id=? AND (name LIKE ? OR number LIKE ?)"
                " ORDER BY name COLLATE NOCASE, number LIMIT ?",
                (login_id, like, like, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM contacts WHERE login_id=? "
                             "ORDER BY name COLLATE NOCASE, number LIMIT ?",
                             (login_id, limit)).fetchall()
        return [dict(r) for r in rows]


def contact_get(login_id, cid):
    with M.db() as c:
        r = c.execute("SELECT * FROM contacts WHERE id=? AND login_id=?",
                      (cid, login_id)).fetchone()
        return dict(r) if r else None


def upsert_contact(login_id, cid, name, number, org="", email=""):
    """Insert or update; returns (ok, contact_id | error_key)."""
    name = (name or "").strip()[:120]
    number = (number or "").strip()[:40]
    org = (org or "").strip()[:120]
    email = (email or "").strip()[:254]
    n = norm_number(number)
    if not name:
        return False, "noname"
    if not n:
        return False, "nonumber"
    if email and not re.match(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$", email):
        return False, "emailbad"
    with M.db() as c:
        if cid:
            r = c.execute("UPDATE contacts SET name=?, number=?, number_norm=?, org=?, email=?,"
                          " updated_at=datetime('now') WHERE id=? AND login_id=?",
                          (name, number, n, org, email, cid, login_id))
            if not r.rowcount:
                return False, "notfound"
        else:
            r = c.execute("INSERT INTO contacts (login_id, name, number, number_norm, org, email)"
                          " VALUES (?,?,?,?,?,?)", (login_id, name, number, n, org, email))
            cid = r.lastrowid
        c.commit()
    return True, cid


def remove_contacts(login_id, ids):
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return 0
    with M.db() as c:
        n = 0
        for i in ids:
            # FK cascades aren't enforced in sqlite: delete notes first.
            c.execute("DELETE FROM contact_notes WHERE contact_id=? AND login_id=?",
                      (i, login_id))
            n += c.execute("DELETE FROM contacts WHERE id=? AND login_id=?",
                           (i, login_id)).rowcount
        c.commit()
    return n


# ---------------------------------------------------------------- vCard / CSV

def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text or "")


def parse_vcard(text):
    """[(name, number, org, email)] from vCard text (one TEL each)."""
    cards, cur = [], {}
    for line in _unfold(text).splitlines():
        line = line.strip()
        if line.upper() == "BEGIN:VCARD":
            cur = {"tels": []}
        elif line.upper() == "END:VCARD":
            name = cur.get("fn") or cur.get("n") or ""
            for tel in cur.get("tels", []) or [""]:
                cards.append((name.strip(), tel.strip(),
                              cur.get("org", "").strip(), cur.get("email", "").strip()))
            cur = {}
        else:
            key, _, val = line.partition(":")
            key = key.upper().split(";")[0]
            if key == "FN":
                cur["fn"] = val
            elif key == "N" and not cur.get("fn"):
                parts = val.split(";")
                cur["n"] = (" ".join(p for p in (parts[1:2] + parts[0:1]) if p)).strip()
            elif key == "TEL":
                cur.setdefault("tels", []).append(val)
            elif key == "ORG":
                cur["org"] = val.split(";")[0]
            elif key == "EMAIL":
                cur.setdefault("email", val)
    return cards


def parse_csv(text):
    """[(name, number, org, email)] from CSV with a header row."""
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(text or ""))
    except Exception:
        return rows
    if not reader.fieldnames:
        return rows
    heads = {h.strip().lower(): h for h in reader.fieldnames}
    def col(*names):
        for n in names:
            if n in heads:
                return heads[n]
        return None
    cn, cnum, corg, cemail = (col("name", "full name", "display name"),
                              col("number", "phone", "tel", "mobile"),
                              col("org", "organization", "company"),
                              col("email", "e-mail"))
    if not cn or not cnum:
        return rows
    for r in reader:
        try:
            rows.append(((r.get(cn) or "").strip(), (r.get(cnum) or "").strip(),
                         (r.get(corg) or "").strip() if corg else "",
                         (r.get(cemail) or "").strip() if cemail else ""))
        except Exception:
            continue
    return rows


def to_vcard(rows):
    out = []
    for r in rows:
        out.append("BEGIN:VCARD\r\nVERSION:3.0\r\n"
                   "FN:%s\r\nTEL;TYPE=CELL:%s\r\nORG:%s\r\nEMAIL:%s\r\nEND:VCARD"
                   % (r["name"].replace("\r", "").replace("\n", " "),
                      r["number"], r["org"], r["email"]))
    return "\r\n".join(out) + "\r\n"


def import_rows(login_id, rows):
    """Bulk-add parsed (name, number, org, email) rows. Returns (added, skipped)."""
    added, skipped = 0, 0
    seen = set()
    with M.db() as c:
        have = {r[0] for r in c.execute(
            "SELECT number_norm FROM contacts WHERE login_id=?", (login_id,))}
        for name, number, org, email in rows:
            n = norm_number(number)
            if not name or not n or n in have or (name, n) in seen:
                skipped += 1
                continue
            c.execute("INSERT INTO contacts (login_id, name, number, number_norm, org, email)"
                      " VALUES (?,?,?,?,?,?)",
                      (login_id, name.strip()[:120], number.strip()[:40], n,
                       (org or "").strip()[:120], (email or "").strip()[:254]))
            have.add(n)
            seen.add((name, n))
            added += 1
        c.commit()
    return added, skipped


# ---------------------------------------------------------------- notes

def notes_for(login_id, contact_id=None, limit=500):
    with M.db() as c:
        if contact_id:
            rows = c.execute("SELECT * FROM contact_notes WHERE login_id=? AND contact_id=?"
                             " ORDER BY id DESC LIMIT ?", (login_id, contact_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM contact_notes WHERE login_id=? "
                             "ORDER BY id DESC LIMIT ?", (login_id, limit)).fetchall()
        return [dict(r) for r in rows]


def add_note(login_id, contact_id, body, item_type="", item_id=None, item_at=""):
    body = (body or "").strip()
    if not body:
        return False, "empty"
    if len(body) > 4000:
        return False, "toolong"
    contact = contact_get(login_id, contact_id)
    if not contact:
        return False, "notfound"
    item_type = (item_type or "").strip().lower()
    if item_type and item_type not in NOTE_TYPES:
        return False, "badtype"
    iid = None
    if item_id:
        try:
            iid = int(item_id)
        except (TypeError, ValueError):
            return False, "badtype"
    # Snapshot the item's date when it exists, so the timeline reads right
    # even after the 90-day CDR purge takes the original row.
    if not item_at and iid and item_type:
        item_at = _item_date(login_id, item_type, iid) or ""
    with M.db() as c:
        c.execute("INSERT INTO contact_notes (login_id, contact_id, item_type, item_id, item_at, body)"
                  " VALUES (?,?,?,?,?,?)",
                  (login_id, contact_id, item_type, iid, (item_at or "")[:19], body))
        c.commit()
    return True, None


def remove_notes(login_id, ids):
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return 0
    with M.db() as c:
        n = 0
        for i in ids:
            n += c.execute("DELETE FROM contact_notes WHERE id=? AND login_id=?",
                           (i, login_id)).rowcount
        c.commit()
    return n


def note_counts(login_id):
    """{contact_id: count} for badge display."""
    with M.db() as c:
        return {r[0]: r[1] for r in c.execute(
            "SELECT contact_id, COUNT(*) FROM contact_notes WHERE login_id=? GROUP BY contact_id",
            (login_id,))}


def latest_note_id(login_id):
    with M.db() as c:
        r = c.execute("SELECT COALESCE(MAX(id),0) FROM contact_notes WHERE login_id=?",
                      (login_id,)).fetchone()
    return r[0] if r else 0


def _item_date(login_id, item_type, item_id):
    """started_at/received_at/sent_at of the item a note points at, or ''."""
    try:
        with M.db() as c:
            if item_type == "call":
                mine = {r["id"] for r in M.ucp._my_calls({"id": login_id, "exten": ""}, limit=1)}
                r = c.execute("SELECT started_at FROM cdr WHERE id=?", (item_id,)).fetchone()
            elif item_type == "message":
                r = c.execute("SELECT sent_at FROM messages WHERE id=? AND login_id=?",
                              (item_id, login_id)).fetchone()
            elif item_type == "voicemail":
                box = M.ucp._mailbox(login_id)
                r = (c.execute("SELECT received_at FROM voicemail_messages WHERE id=? AND mailbox=?",
                               (item_id, box)).fetchone() if box else None)
            elif item_type == "recording":
                r = c.execute("SELECT started_at FROM recordings WHERE id=? AND login_id=?",
                              (item_id, login_id)).fetchone()
            else:
                r = None
            return (r[0] or "")[:19] if r else ""
    except Exception:
        return ""


def delete_notes_for_item(login_id, item_type, item_id):
    """Drop notes that pointed at a deleted voicemail/recording."""
    with M.db() as c:
        n = c.execute("DELETE FROM contact_notes WHERE login_id=? AND item_type=? AND item_id=?",
                      (login_id, item_type, item_id)).rowcount
        c.commit()
    return n


# ---------------------------------------------------------------- timeline

def _body_snippet(body, n=140):
    body = (body or "").replace("\n", " ")
    return body if len(body) <= n else body[:n] + "…"


def timeline_for(login_id, limit=TIMELINE_LIMIT, contact_id=None):
    """One merged, newest-first feed of calls, texts, voicemails, recordings.

    Built on the same accessors the lists use, so plan/ownership rules apply
    (users never see admin recordings, etc.). Notes attach via contact_id.
    """
    me_row = {"id": login_id}
    with M.db() as c:
        me = c.execute("SELECT * FROM logins WHERE id=?", (login_id,)).fetchone()
    me = dict(me) if me else me_row
    names = names_for(login_id)
    items = []

    def nm(raw):
        return names.get(norm_number(raw), "")

    for r in M.ucp._my_calls(me, limit=limit, missed_only=False):
        other = r.get("other") or ""
        items.append({"type": "call", "id": r["id"], "at": (r.get("started_at") or "")[:19],
                      "name": nm(other), "number": other,
                      "direction": r.get("kind") or "",
                      "duration_sec": r.get("bill_sec") or r.get("duration_sec") or 0,
                      "body": ""})
    for m in M.ucp._my_messages(me, limit=limit):
        peer = m["to_ext"] if m["from_ext"] == me.get("exten") else m["from_ext"]
        items.append({"type": "message", "id": m["id"], "at": (m.get("sent_at") or "")[:19],
                      "name": nm(peer), "number": peer,
                      "direction": "out" if m["from_ext"] == me.get("exten") else "in",
                      "duration_sec": 0, "body": _body_snippet(m.get("body"))})
    box = M.ucp._mailbox(login_id)
    if box:
        with M.db() as c:
            vms = [dict(r) for r in c.execute(
                "SELECT id, caller, received_at, duration_sec FROM voicemail_messages"
                " WHERE mailbox=? ORDER BY id DESC LIMIT ?", (box, limit)).fetchall()]
        for v in vms:
            items.append({"type": "voicemail", "id": v["id"], "at": (v.get("received_at") or "")[:19],
                          "name": nm(v.get("caller")), "number": v.get("caller") or "",
                          "direction": "in", "duration_sec": v.get("duration_sec") or 0, "body": ""})
    for r in M.ucp._recordings_for(me, limit=limit):
        peer = r.get("peer") or ""
        items.append({"type": "recording", "id": r["id"], "at": (r.get("started_at") or "")[:19],
                      "name": nm(peer), "number": peer,
                      "direction": r.get("direction") or "",
                      "duration_sec": r.get("duration_sec") or 0, "body": ""})
    items.sort(key=lambda i: (i["at"], i["id"]), reverse=True)

    counts = note_counts(login_id)
    if contact_id:
        contact = contact_get(login_id, contact_id)
        want = contact["number_norm"] if contact else "\x00"
        items = [i for i in items if norm_number(i["number"]) == want]
        for i in items:
            i["note_count"] = counts.get(contact_id, 0)
    else:
        # Map numbers back to contacts for per-item note badges.
        with M.db() as c:
            num2cid = {r["number_norm"]: r["id"] for r in c.execute(
                "SELECT id, number_norm FROM contacts WHERE login_id=?", (login_id,))}
        for i in items:
            cid = num2cid.get(norm_number(i["number"]))
            i["note_count"] = counts.get(cid, 0) if cid else 0
    return items[:limit]


# ---------------------------------------------------------------- web UI

def _render(request, s, me, tab, title, body):
    return M.ucp._render(request, s, me, tab, title, body)


def contacts_page(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        return RedirectResponse("/login")
    q = (request.query_params.get("q") or "").strip()[:60]
    rows = contacts_for(me["id"], q=q)
    counts = note_counts(me["id"])
    csrf = M._csrf_field(s)
    flash = M.ucp._flash(request)
    trs = "".join(
        f'<tr><td><a href="/ucp/contacts/{r["id"]}">{M.esc(r["name"])}</a></td>'
        f'<td>{M.esc(r["number"])}</td><td>{M.esc(r["org"])}</td>'
        f'<td>{counts.get(r["id"], 0) or ""}</td>'
        f'<td class="actions"><form method="post" action="/ucp/contacts/delete" class="inline"'
        f' onsubmit="return confirm(\'Delete this contact and its notes?\')">{csrf}'
        f'<input type="hidden" name="ids" value="{r["id"]}"><button class="btn ghost danger">Delete</button></form></td></tr>'
        for r in rows) or f'<tr><td colspan="5" class="muted">{"No contacts match." if q else "No contacts yet."}</td></tr>'
    body = f"""{flash}
<form method="get" action="/ucp/contacts" class="inline">
<input type="search" name="q" value="{M.esc(q)}" placeholder="Search contacts" aria-label="Search contacts">
<button class="btn ghost">Search</button></form>
<table><tr><th>Name</th><th>Number</th><th>Organization</th><th>Notes</th><th></th></tr>{trs}</table>
<h3>Add contact</h3>
<form method="post" action="/ucp/contacts/save">{csrf}
<label>Name <input name="name" required maxlength="120"></label>
<label>Number <input name="number" required maxlength="40" inputmode="tel"></label>
<label>Organization <input name="org" maxlength="120"></label>
<label>Email <input name="email" type="email" maxlength="254"></label>
<button class="btn">Add contact</button></form>
<h3>Import / export</h3>
<form method="post" action="/ucp/contacts/import" enctype="multipart/form-data">{csrf}
<label>vCard (.vcf) or CSV file <input type="file" name="file" accept=".vcf,.vcf,.csv,text/vcard,text/csv" required></label>
<button class="btn ghost">Import</button></form>
<p class="muted"><a href="/ucp/contacts/export">Download all contacts as vCard</a>.
Names you set here show up next to numbers in your call history, texts, voicemail and recordings —
and on your phone's screen while it rings.</p>"""
    return _render(request, s, me, "ucp-contacts", "Contacts", body)


def contact_page(request: Request, cid: int):
    s, me = M.ucp._me(request)
    if not s:
        return RedirectResponse("/login")
    c = contact_get(me["id"], cid)
    if not c:
        return RedirectResponse("/ucp/contacts")
    csrf = M._csrf_field(s)
    flash = M.ucp._flash(request)
    items = timeline_for(me["id"], limit=200, contact_id=cid)
    notes = notes_for(me["id"], contact_id=cid)
    kind_icon = {"call": "☎", "message": "✉", "voicemail": "📼", "recording": "⏺"}
    trows = "".join(
        f'<tr><td>{kind_icon.get(i["type"], "•")}</td><td>{M.esc(i["direction"])}</td>'
        f'<td>{M.esc(i["at"])}</td><td>{M.esc(i["body"] or (str(i["duration_sec"]) + "s" if i["duration_sec"] else ""))}</td></tr>'
        for i in items) or '<tr><td colspan="4" class="muted">Nothing yet.</td></tr>'
    nrows = "".join(
        f'<tr><td>{M.esc(n["body"])}</td><td class="muted">{M.esc(n["created_at"])}</td>'
        f'<td class="actions"><form method="post" action="/ucp/notes/delete" class="inline"'
        f' onsubmit="return confirm(\'Delete this note?\')">{csrf}'
        f'<input type="hidden" name="ids" value="{n["id"]}">'
        f'<input type="hidden" name="back" value="/ucp/contacts/{cid}">'
        f'<button class="btn ghost danger">Delete</button></form></td></tr>'
        for n in notes) or '<tr><td colspan="3" class="muted">No notes yet.</td></tr>'
    body = f"""{flash}
<p><a href="/ucp/contacts">← All contacts</a></p>
<h3>{M.esc(c["name"])}</h3>
<p class="muted">{M.esc(c["number"])} · {M.esc(c["org"])} · {M.esc(c["email"])}</p>
<form method="post" action="/ucp/contacts/save" class="inline">{csrf}
<input type="hidden" name="id" value="{c["id"]}">
<label>Name <input name="name" value="{M.esc(c["name"])}" required maxlength="120"></label>
<label>Number <input name="number" value="{M.esc(c["number"])}" required maxlength="40" inputmode="tel"></label>
<label>Organization <input name="org" value="{M.esc(c["org"])}" maxlength="120"></label>
<label>Email <input name="email" type="email" value="{M.esc(c["email"])}" maxlength="254"></label>
<button class="btn ghost">Save</button></form>
<h3>Timeline</h3>
<table><tr><th></th><th></th><th>When</th><th></th></tr>{trows}</table>
<h3>Notes</h3>
<table><tr><th>Note</th><th>Written</th><th></th></tr>{nrows}</table>
<form method="post" action="/ucp/notes/save">{csrf}
<input type="hidden" name="contact_id" value="{c["id"]}">
<input type="hidden" name="back" value="/ucp/contacts/{c["id"]}">
<label>Add a note<br><textarea name="body" rows="3" cols="60" required maxlength="4000"></textarea></label><br>
<button class="btn">Add note</button></form>
<p class="muted">Notes are private to you and survive the 90-day call-history cleanup.</p>"""
    return _render(request, s, me, "ucp-contacts", M.esc(c["name"]), body)


async def contact_save(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return M.ucp._back("/ucp/contacts", "locked")
    f = await request.form()
    cid = f.get("id")
    try:
        cid = int(cid) if cid else None
    except (TypeError, ValueError):
        cid = None
    ok, res = upsert_contact(me["id"], cid, f.get("name"), f.get("number"),
                              f.get("org"), f.get("email"))
    M.ucp._audit(me, "ucp.contact.save", f"id={res} ok={ok}")
    if not ok:
        return M.ucp._back("/ucp/contacts", "contactbad")
    return M.ucp._back(f"/ucp/contacts/{res}" if not f.get("id") else "/ucp/contacts", "saved")


async def contact_delete(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return M.ucp._back("/ucp/contacts", "locked")
    f = await request.form()
    ids = f.getlist("ids") if hasattr(f, "getlist") else [f.get("ids")]
    n = remove_contacts(me["id"], ids)
    M.ucp._audit(me, "ucp.contact.delete", f"n={n}")
    return M.ucp._back("/ucp/contacts", "deleted")


async def contact_import(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return M.ucp._back("/ucp/contacts", "locked")
    f = await request.form()
    up = f.get("file")
    data = await up.read() if up and hasattr(up, "read") else b""
    if len(data) > 2 * 1024 * 1024:
        return M.ucp._back("/ucp/contacts", "contactbig")
    text = data.decode("utf-8", "replace")
    rows = parse_vcard(text) if "BEGIN:VCARD" in text.upper() else parse_csv(text)
    added, skipped = import_rows(me["id"], rows)
    M.ucp._audit(me, "ucp.contact.import", f"added={added} skipped={skipped}")
    return M.ucp._back("/ucp/contacts", "imported")


def contact_export(request: Request):
    from fastapi.responses import PlainTextResponse
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    rows = contacts_for(me["id"], limit=10000)
    return PlainTextResponse(to_vcard(rows), media_type="text/vcard",
                             headers={"Content-Disposition": "attachment; filename=contacts.vcf"})


async def note_save(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return M.ucp._back("/ucp/contacts", "locked")
    f = await request.form()
    try:
        cid = int(f.get("contact_id") or 0)
    except (TypeError, ValueError):
        return M.ucp._back("/ucp/contacts", "contactbad")
    ok, err = add_note(me["id"], cid, f.get("body"), f.get("item_type") or "",
                       f.get("item_id") or None)
    M.ucp._audit(me, "ucp.note.save", f"contact={cid} ok={ok}")
    back = (f.get("back") or f"/ucp/contacts/{cid}")
    if not back.startswith("/ucp/contacts"):
        back = "/ucp/contacts"
    return M.ucp._back(back, "saved" if ok else "notebad")


async def note_delete(request: Request):
    s, me = M.ucp._me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return M.ucp._back("/ucp/contacts", "locked")
    f = await request.form()
    ids = f.getlist("ids") if hasattr(f, "getlist") else [f.get("ids")]
    n = remove_notes(me["id"], ids)
    M.ucp._audit(me, "ucp.note.delete", f"n={n}")
    back = (f.get("back") or "/ucp/contacts")
    if not back.startswith("/ucp/contacts"):
        back = "/ucp/contacts"
    return M.ucp._back(back, "deleted")


def install(app_module):
    """Register the /ucp/contacts web routes on app_module.app."""
    global M
    M = app_module
    app = M.app
    html = dict(response_class=HTMLResponse)
    app.get("/ucp/contacts", **html)(contacts_page)
    app.get("/ucp/contacts/{cid}", **html)(contact_page)
    app.post("/ucp/contacts/save")(contact_save)
    app.post("/ucp/contacts/delete")(contact_delete)
    app.post("/ucp/contacts/import")(contact_import)
    app.get("/ucp/contacts/export")(contact_export)
    app.post("/ucp/notes/save")(note_save)
    app.post("/ucp/notes/delete")(note_delete)
    # Extra flash messages for these pages (ucp owns the flash table).
    M.ucp.FLASH.update({
        "contactbad": ("bad", "A contact needs a name and a phone number."),
        "contactbig": ("bad", "That file is too large (2 MB max)."),
        "imported": ("ok", "Contacts imported. Duplicates were skipped."),
        "notebad": ("bad", "Couldn't save that note."),
    })
