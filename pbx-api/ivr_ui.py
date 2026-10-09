# SPDX-License-Identifier: GPL-2.0-or-later
"""IVR (auto-attendant) menus: admin pages (/ivr) and user pages (/ucp/ivr).

One editor serves both:
- Admins manage every menu, including system menus (no owner) that can
  have an internal number and send callers to anyone's voicemail. They also
  set how many menus users may have (entitlements: the billing hook).
- Users manage only their own menus, up to their 'ivr_menus' quota. Their
  menus can ring any extension or group, reach their OWN voicemail or OWN
  other menus, repeat, or hang up. No outside numbers (toll fraud; billing).

Installed by app.py: ivr_ui.install(app_module).
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import wave

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

import entitlements as ent

M = None
GREET_DIR = os.environ.get("PBX_IVR_DIR", "/var/lib/pbx/ivr")
MAX_UPLOAD = 10 * 1024 * 1024      # 10 MB
MAX_GREETING_SEC = 300             # 5 minutes
KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "*", "#"]
TIMEOUTS = [3, 5, 7, 10, 15]
RETRIES = [0, 1, 2, 3, 5]
EXT_RE = re.compile(r"^\d{2,6}$")
DEST_LABEL = {"": "— not used —", "ext": "Ring extension", "group": "Ring group",
              "vm": "Voicemail", "ivr": "Another menu", "repeat": "Repeat menu",
              "hangup": "Hang up"}


def esc(x):
    return M.esc(x)


# ---------------------------------------------------------------- data

def _menu(menu_id):
    with M.db() as c:
        r = c.execute("SELECT * FROM ivr_menus WHERE id=?", (menu_id,)).fetchone()
    return dict(r) if r else None


def _menus(owner_id=None, all_menus=False):
    with M.db() as c:
        if all_menus:
            rows = c.execute("SELECT m.*, l.username AS owner FROM ivr_menus m"
                             " LEFT JOIN logins l ON l.id=m.owner_login_id"
                             " ORDER BY m.owner_login_id IS NOT NULL, l.username, m.name").fetchall()
        else:
            rows = c.execute("SELECT m.*, NULL AS owner FROM ivr_menus m WHERE owner_login_id=?"
                             " ORDER BY id", (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def _active_ids(owner_id):
    """Menus within the owner's quota (oldest first) - mirrors pbx-brain."""
    with M.db() as c:
        n = ent.quota(c, owner_id, "ivr_menus")
        if n <= 0:
            return set()
        return {r[0] for r in c.execute(
            "SELECT id FROM ivr_menus WHERE owner_login_id=? ORDER BY id LIMIT ?", (owner_id, n))}


def _extensions():
    with M.db() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, exten, display_name, username FROM logins"
            " WHERE enabled=1 AND exten!='' ORDER BY exten")]


def _summ(dest, menus_by_id):
    t = (dest or {}).get("type", "")
    tg = (dest or {}).get("target", "")
    if t == "ivr":
        return f'{DEST_LABEL[t]}: {esc(menus_by_id.get(str(tg), {}).get("name", "?"))}'
    if t in ("ext", "vm", "group"):
        return f"{DEST_LABEL[t]} {esc(tg)}"
    return esc(DEST_LABEL.get(t, t))


def _opts(menu):
    try:
        o = json.loads(menu.get("options") or "{}")
        return o if isinstance(o, dict) else {}
    except Exception:
        return {}


def _fb(menu):
    try:
        f = json.loads(menu.get("fallback") or "{}")
        return f if isinstance(f, dict) else {"type": "hangup"}
    except Exception:
        return {"type": "hangup"}


# ---------------------------------------------------------------- greeting audio

def _convert_greeting(upload_bytes: bytes, filename: str, menu_id, directory=None,
                      prefix="ivr", max_sec=MAX_GREETING_SEC) -> str:
    """Store an uploaded greeting as 8 kHz mono 16-bit WAV. Returns path.
    Raises ValueError with a user-facing message. Also used for voicemail
    greetings (ucp.py) with their own directory/prefix."""
    directory = directory or GREET_DIR
    if len(upload_bytes) > MAX_UPLOAD:
        raise ValueError("Greeting file is too big (max 10 MB).")
    os.makedirs(directory, exist_ok=True)
    out = os.path.join(directory, f"{prefix}-{menu_id}-{secrets.token_hex(4)}.wav")
    suffix = os.path.splitext(filename or "")[1].lower()[:6] or ".bin"
    with tempfile.NamedTemporaryFile(dir=directory, suffix=suffix, delete=False) as tf:
        tf.write(upload_bytes)
        src = tf.name
    try:
        sox = shutil.which("sox")
        if sox:
            r = subprocess.run([sox, src, "-r", "8000", "-c", "1", "-b", "16",
                                "-e", "signed-integer", out],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise ValueError("Couldn't read that audio file. Upload a WAV or MP3.")
        else:
            try:
                with wave.open(src) as w:
                    ok = (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)
            except Exception:
                ok = False
            if not ok:
                raise ValueError("This server can't convert audio yet (the 'sox' tool isn't installed). "
                                 "Upload a WAV that is 8 kHz, mono, 16-bit, or ask the admin to run "
                                 "'apt install sox libsox-fmt-mp3'.")
            shutil.copyfile(src, out)
        with wave.open(out) as w:
            secs = w.getnframes() / float(w.getframerate() or 8000)
        if secs > max_sec:
            os.remove(out)
            raise ValueError(f"Greeting is too long (max {max_sec // 60 or 1} minute{'s' if max_sec >= 120 else ''}).")
        if secs < 0.3:
            os.remove(out)
            raise ValueError("Greeting seems to be empty.")
        os.chmod(out, 0o644)
        return out
    finally:
        try:
            os.remove(src)
        except OSError:
            pass


def _drop_greeting(path):
    try:
        if path and os.path.commonpath([os.path.abspath(path), GREET_DIR]) == GREET_DIR:
            os.remove(path)
    except Exception:
        pass


def greeting_audio(request: Request, menu_id: int):
    s = M._sess(request)
    if not s:
        raise HTTPException(401)
    m = _menu(menu_id)
    if not m or not m["greeting_path"]:
        raise HTTPException(404)
    if s["role"] != "admin" and m["owner_login_id"] != s["id"]:
        raise HTTPException(403)
    p = m["greeting_path"]
    if os.path.commonpath([os.path.abspath(p), GREET_DIR]) != GREET_DIR or not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- editor

def _dest_picker(prefix, dest, exts, menus, restricted, me, self_id):
    t = (dest or {}).get("type", "")
    tg = str((dest or {}).get("target", ""))
    types = ["", "ext", "group", "vm", "ivr", "repeat", "hangup"]
    if prefix == "fb":
        types = ["hangup", "ext", "group", "vm", "ivr"]
    sel = "".join(f'<option value="{k}" {"selected" if k == t else ""}>{esc(DEST_LABEL[k])}</option>'
                  for k in types)
    ext_opts = "".join(
        f'<option value="{esc(e["exten"])}" {"selected" if t == "ext" and tg == e["exten"] else ""}>'
        f'{esc(e["exten"])} {esc(e["display_name"] or e["username"])}</option>' for e in exts)
    vm_exts = [e for e in exts if not restricted or e["id"] == me["id"]]
    vm_opts = "".join(
        f'<option value="{esc(e["exten"])}" {"selected" if t == "vm" and tg == e["exten"] else ""}>'
        f'{esc(e["exten"])} {esc(e["display_name"] or e["username"])}</option>' for e in vm_exts)
    ivr_opts = "".join(
        f'<option value="{m["id"]}" {"selected" if t == "ivr" and tg == str(m["id"]) else ""}>'
        f'{esc(m["name"])}{" (system)" if not m.get("owner_login_id") and not restricted else ""}</option>'
        for m in menus if m["id"] != self_id)
    return f"""<div class="dest" data-prefix="{prefix}">
<select name="{prefix}_type" class="dtype">{sel}</select>
<select name="{prefix}_ext" class="dt dt-ext">{ext_opts}</select>
<input name="{prefix}_group" class="dt dt-group" placeholder="e.g. 8800,8801" value="{esc(tg) if t == 'group' else ''}">
<select name="{prefix}_vm" class="dt dt-vm">{vm_opts}</select>
<select name="{prefix}_ivr" class="dt dt-ivr">{ivr_opts or '<option value="">(no other menus)</option>'}</select>
</div>"""


EDITOR_JS = """<script>
function syncDest(d){const t=d.querySelector('.dtype').value;
 d.querySelectorAll('.dt').forEach(e=>{e.style.display=e.classList.contains('dt-'+t)?'':'none'});}
document.querySelectorAll('.dest').forEach(d=>{syncDest(d);
 d.querySelector('.dtype').addEventListener('change',()=>syncDest(d));});
</script>"""


def _editor(request, s, me, menu, restricted, action, cancel, error=""):
    menu = menu or {"id": None, "name": "", "exten": "", "greeting_path": "", "timeout_sec": 5,
                    "max_retries": 2, "direct_dial": 1, "options": "{}",
                    "fallback": '{"type":"hangup"}', "enabled": 1, "owner_login_id": None}
    exts = _extensions()
    if restricted:
        menus = _menus(owner_id=me["id"])
    else:
        menus = _menus(all_menus=True)
        if menu.get("owner_login_id"):
            # a user's menu may only point at that user's menus
            menus = [m for m in menus if m["owner_login_id"] == menu["owner_login_id"]]
    owner_me = me
    if not restricted and menu.get("owner_login_id"):
        with M.db() as c:
            r = c.execute("SELECT * FROM logins WHERE id=?", (menu["owner_login_id"],)).fetchone()
        owner_me = dict(r) if r else me
    restrict_dests = restricted or bool(menu.get("owner_login_id"))
    opts = _opts(menu)
    rows = "".join(
        f'<tr><td class="key">{esc(k)}</td><td>{_dest_picker("o" + str(i), opts.get(k), exts, menus, restrict_dests, owner_me, menu["id"])}</td></tr>'
        for i, k in enumerate(KEYS))
    csrf = M._csrf_field(s)
    greet = ""
    if menu["id"] and menu["greeting_path"]:
        base = "/api/ivr-greeting"
        greet = (f'<div class="greet"><audio controls preload="none" src="{base}/{menu["id"]}"></audio>'
                 f'<label class="inline-chk"><input type="checkbox" name="remove_greeting" value="1"> Remove greeting</label></div>')
    else:
        greet = '<p class="muted">No greeting yet: callers hear a standard "please enter the number" prompt.</p>'
    exten_field = "" if restrict_dests else (
        f'<label>Internal number (optional)<br><input name="exten" value="{esc(menu["exten"])}" '
        f'placeholder="e.g. 7000" inputmode="numeric"></label>'
        f'<p class="muted">Lets phones dial this menu. Must not clash with an extension.</p>')
    sel = lambda name, vals, cur: "".join(
        f'<option value="{v}" {"selected" if int(cur or 0) == v else ""}>{v}</option>' for v in vals)
    err = f'<div class="flash bad">{esc(error)}</div>' if error else ""
    return f"""{err}
<form method="post" action="{action}" enctype="multipart/form-data" class="ivr-form">{csrf}
<div class="grid2">
<section class="panel">
<h3>Menu</h3>
<label>Name<br><input name="name" value="{esc(menu["name"])}" required maxlength="40" placeholder="e.g. Main menu"></label>
{exten_field}
<label>Greeting (WAV or MP3)<br><input type="file" name="greeting" accept="audio/*,.wav,.mp3"></label>
{greet}
<p class="muted">Record something like: "Thanks for calling. For sales press 1, for support press 2, or dial an extension at any time."</p>
<label>Wait for a key after the greeting<br><select name="timeout_sec">{sel("t", TIMEOUTS, menu["timeout_sec"])}</select> seconds</label>
<label>Replay the menu if nothing / a wrong key is pressed<br><select name="max_retries">{sel("r", RETRIES, menu["max_retries"])}</select> times</label>
<label class="switch"><input type="checkbox" name="direct_dial" value="1" {"checked" if menu["direct_dial"] else ""}> <span>Callers may dial an extension number</span></label>
<label class="switch"><input type="checkbox" name="enabled" value="1" {"checked" if menu["enabled"] else ""}> <span>Menu enabled</span></label>
</section>
<section class="panel">
<h3>Keys</h3>
<table class="keys">{rows}</table>
<h3 style="margin-top:18px">When retries run out</h3>
{_dest_picker("fb", _fb(menu), exts, menus, restrict_dests, owner_me, menu["id"])}
</section>
</div>
<p><button class="btn">Save menu</button> <a class="btn ghost" href="{cancel}">Cancel</a></p>
</form>{EDITOR_JS}"""


def _parse(form, restricted, owner, menu_id):
    """Validate the editor form. Returns (fields dict, error str)."""
    exts = {e["exten"]: e for e in _extensions()}
    name = (form.get("name") or "").strip()
    if not name or len(name) > 40 or any(ord(ch) < 32 for ch in name):
        return None, "Give the menu a name (up to 40 characters)."
    if owner:
        allowed_menus = {str(m["id"]) for m in _menus(owner_id=owner["id"])}
    else:
        allowed_menus = {str(m["id"]) for m in _menus(all_menus=True)}

    def dest(prefix):
        t = form.get(prefix + "_type") or ""
        if t in ("", "repeat", "hangup"):
            return ({"type": t} if t else None), None
        if t == "ext":
            v = form.get(prefix + "_ext") or ""
            if v not in exts:
                return None, "Pick an extension to ring."
            return {"type": "ext", "target": v}, None
        if t == "vm":
            v = form.get(prefix + "_vm") or ""
            if v not in exts:
                return None, "Pick a voicemail box."
            if owner and exts[v]["id"] != owner["id"]:
                return None, "Menus can only send callers to your own voicemail."
            return {"type": "vm", "target": v}, None
        if t == "group":
            raw = (form.get(prefix + "_group") or "").replace(" ", "")
            lst = [x for x in raw.split(",") if x]
            if not lst or len(lst) > 10 or any(x not in exts for x in lst):
                return None, "A ring group needs 1-10 existing extensions, separated by commas."
            return {"type": "group", "target": ",".join(dict.fromkeys(lst))}, None
        if t == "ivr":
            v = form.get(prefix + "_ivr") or ""
            if v not in allowed_menus or (menu_id and v == str(menu_id)):
                return None, "Pick another menu to send callers to."
            return {"type": "ivr", "target": int(v)}, None
        return None, "Unknown destination."

    options = {}
    for i, k in enumerate(KEYS):
        d, e = dest("o" + str(i))
        if e:
            return None, f"Key {k}: {e}"
        if d:
            options[k] = d
    fb, e = dest("fb")
    if e:
        return None, f"When retries run out: {e}"
    fb = fb or {"type": "hangup"}
    if fb["type"] == "repeat":
        fb = {"type": "hangup"}
    exten = ""
    if not restricted and not owner:
        exten = (form.get("exten") or "").strip()
        if exten:
            if not EXT_RE.match(exten):
                return None, "Internal number must be 2-6 digits."
            if exten in exts:
                return None, f"{exten} is already an extension."
            with M.db() as c:
                clash = c.execute("SELECT name FROM ivr_menus WHERE exten=? AND id!=?",
                                  (exten, menu_id or 0)).fetchone()
            if clash:
                return None, f"{exten} is already used by menu '{clash[0]}'."
    try:
        timeout = int(form.get("timeout_sec") or 5)
        retries = int(form.get("max_retries") or 2)
    except ValueError:
        return None, "Bad timeout/retries."
    if timeout not in TIMEOUTS or retries not in RETRIES:
        return None, "Bad timeout/retries."
    if not options and not form.get("direct_dial"):
        return None, "Set at least one key, or let callers dial extensions."
    return {"name": name, "exten": exten, "timeout_sec": timeout, "max_retries": retries,
            "direct_dial": 1 if form.get("direct_dial") else 0,
            "enabled": 1 if form.get("enabled") else 0,
            "options": json.dumps(options), "fallback": json.dumps(fb)}, None


async def _save(request, s, menu_id, owner, restricted):
    """Create/update a menu from the posted form. Returns (menu_id, error)."""
    form = await request.form()
    fields, err = _parse(form, restricted, owner, menu_id)
    if err:
        return None, err
    with M.db() as c:
        dup = c.execute("SELECT 1 FROM ivr_menus WHERE COALESCE(owner_login_id,0)=? AND name=? AND id!=?",
                        (owner["id"] if owner else 0, fields["name"], menu_id or 0)).fetchone()
    if dup:
        return None, "You already have a menu with that name."
    old = _menu(menu_id) if menu_id else None
    with M.db() as c:
        if menu_id:
            c.execute("UPDATE ivr_menus SET name=?, exten=?, timeout_sec=?, max_retries=?, direct_dial=?,"
                      " enabled=?, options=?, fallback=? WHERE id=?",
                      (fields["name"], fields["exten"], fields["timeout_sec"], fields["max_retries"],
                       fields["direct_dial"], fields["enabled"], fields["options"], fields["fallback"], menu_id))
        else:
            cur = c.execute("INSERT INTO ivr_menus (owner_login_id, name, exten, timeout_sec, max_retries,"
                            " direct_dial, enabled, options, fallback) VALUES (?,?,?,?,?,?,?,?,?)",
                            (owner["id"] if owner else None, fields["name"], fields["exten"],
                             fields["timeout_sec"], fields["max_retries"], fields["direct_dial"],
                             fields["enabled"], fields["options"], fields["fallback"]))
            menu_id = cur.lastrowid
        c.commit()
    up = form.get("greeting")
    if up is not None and getattr(up, "filename", ""):
        data = await up.read()
        try:
            path = _convert_greeting(data, up.filename, menu_id)
        except ValueError as e:
            return menu_id, str(e)
        with M.db() as c:
            c.execute("UPDATE ivr_menus SET greeting_path=? WHERE id=?", (path, menu_id))
            c.commit()
        if old:
            _drop_greeting(old["greeting_path"])
    elif form.get("remove_greeting") and old:
        with M.db() as c:
            c.execute("UPDATE ivr_menus SET greeting_path='' WHERE id=?", (menu_id,))
            c.commit()
        _drop_greeting(old["greeting_path"])
    return menu_id, None


def _delete(menu):
    with M.db() as c:
        c.execute("UPDATE user_prefs SET answer_ivr_id=NULL WHERE answer_ivr_id=?", (menu["id"],))
        c.execute("UPDATE inbound_routes SET ivr_id=NULL WHERE ivr_id=?", (menu["id"],))
        c.execute("DELETE FROM ivr_menus WHERE id=?", (menu["id"],))
        c.commit()
    _drop_greeting(menu["greeting_path"])


def _list_table(menus, base, show_owner, active):
    by_id = {str(m["id"]): m for m in menus}
    rows = ""
    for m in menus:
        o = _opts(m)
        keys = ", ".join(f"{esc(k)}→{_summ(d, by_id)}" for k, d in o.items()) or '<span class="muted">none</span>'
        state = ('<span class="pill ok">On</span>' if m["enabled"] and (m["id"] in active or not m.get("owner_login_id"))
                 else '<span class="pill bad">Off</span>' if not m["enabled"]
                 else '<span class="pill warn">Over limit</span>')
        rows += (f'<tr><td><b>{esc(m["name"])}</b>{"<br><span class=muted>dial " + esc(m["exten"]) + "</span>" if m["exten"] else ""}</td>'
                 + (f'<td>{esc(m["owner"] or "system")}</td>' if show_owner else "")
                 + f'<td class="muted">{keys}</td><td>{state}</td>'
                 f'<td><a class="btn ghost" href="{base}/{m["id"]}/edit">Edit</a></td></tr>')
    return rows


# ---------------------------------------------------------------- user pages

def _me(request):
    return M.ucp._me(request)


def user_list(request: Request):
    s, me = _me(request)
    if not s:
        return RedirectResponse("/login")
    with M.db() as c:
        q = ent.quota(c, me["id"], "ivr_menus")
    menus = _menus(owner_id=me["id"])
    active = _active_ids(me["id"])
    pref = M.ucp._prefs(me["id"])
    if q <= 0:
        body = ('<p>IVR menus aren\'t included in your account yet.</p>'
                '<p><a class="btn" href="/ucp/billing">See plans</a></p>')
        if menus:
            body += '<p class="muted">Your existing menus are kept but switched off.</p>'
        return M.ucp._render(request, s, me, "ucp-ivr", "IVR menus", body)
    rows = _list_table(menus, "/ucp/ivr", False, active) or '<tr><td colspan="4" class="muted">No menus yet.</td></tr>'
    answering = next((m["name"] for m in menus if m["id"] == pref.get("answer_ivr_id")), None)
    can_add = len(menus) < q
    body = f"""
<p>An IVR menu answers your calls with a greeting and lets callers press keys
("press 1 for…") or dial an extension. Using {len(menus)} of {q} allowed.</p>
<p>{"<span class='pill ok'>Answering your calls: " + esc(answering) + "</span>" if answering else "<span class='muted'>None of your menus is answering your calls. Pick one under Settings → Call handling.</span>"}</p>
<table><tr><th>Menu</th><th>Keys</th><th>State</th><th></th></tr>{rows}</table>
<p>{'<a class="btn" href="/ucp/ivr/new/edit">New menu</a>' if can_add else '<span class="muted">You have reached your menu limit.</span> <a href="/ucp/billing">Upgrade your plan</a>'}</p>"""
    return M.ucp._render(request, s, me, "ucp-ivr", "IVR menus", body)


def _user_menu_or_404(me, menu_id):
    m = _menu(menu_id)
    if not m or m["owner_login_id"] != me["id"]:
        raise HTTPException(404)
    return m


def user_edit(request: Request, menu_id: str):
    s, me = _me(request)
    if not s:
        return RedirectResponse("/login")
    with M.db() as c:
        q = ent.quota(c, me["id"], "ivr_menus")
    if menu_id == "new":
        if q <= 0 or len(_menus(owner_id=me["id"])) >= q:
            return RedirectResponse("/ucp/ivr", status_code=303)
        m, action = None, "/ucp/ivr/new/edit"
    else:
        m = _user_menu_or_404(me, int(menu_id))
        action = f"/ucp/ivr/{m['id']}/edit"
    body = _editor(request, s, me, m, True, action, "/ucp/ivr")
    if m:
        body += (f'<form method="post" action="/ucp/ivr/{m["id"]}/delete" onsubmit="return confirm(\'Delete this menu?\')">'
                 f'{M._csrf_field(s)}<button class="link-btn bad">Delete menu</button></form>')
    return M.ucp._render(request, s, me, "ucp-ivr", "Edit menu" if m else "New menu", body)


async def user_save(request: Request, menu_id: str):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return RedirectResponse("/ucp/ivr?msg=locked", status_code=303)
    with M.db() as c:
        q = ent.quota(c, me["id"], "ivr_menus")
    if menu_id == "new":
        if q <= 0 or len(_menus(owner_id=me["id"])) >= q:
            return RedirectResponse("/ucp/ivr", status_code=303)
        mid = None
    else:
        mid = _user_menu_or_404(me, int(menu_id))["id"]
    new_id, err = await _save(request, s, mid, me, True)
    if err:
        m = _menu(new_id) if new_id else None
        action = f"/ucp/ivr/{new_id}/edit" if new_id else "/ucp/ivr/new/edit"
        return M.ucp._render(request, s, me, "ucp-ivr", "Edit menu",
                             _editor(request, s, me, m, True, action, "/ucp/ivr", err))
    M.ucp._audit(me, "ucp.ivr.save", f"id={new_id}")
    return RedirectResponse("/ucp/ivr?msg=saved", status_code=303)


async def user_delete(request: Request, menu_id: int):
    s, me = _me(request)
    if not s:
        raise HTTPException(401)
    await M._check_csrf(request, s)
    if M.ucp._locked():
        return RedirectResponse("/ucp/ivr?msg=locked", status_code=303)
    m = _user_menu_or_404(me, menu_id)
    _delete(m)
    M.ucp._audit(me, "ucp.ivr.delete", f"id={menu_id}")
    return RedirectResponse("/ucp/ivr?msg=saved", status_code=303)


# ---------------------------------------------------------------- admin pages

def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def admin_list(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    menus = _menus(all_menus=True)
    active = set()
    for oid in {m["owner_login_id"] for m in menus if m["owner_login_id"]}:
        active |= _active_ids(oid)
    with M.db() as c:
        dq = ent.default_quota(c, "ivr_menus")
        users = [dict(r) for r in c.execute("SELECT id, username, exten FROM logins WHERE role='user' ORDER BY exten, username")]
        for u in users:
            u["quota"] = ent.quota(c, u["id"], "ivr_menus")
            u["override"] = ent.has_override(c, u["id"], "ivr_menus")
            u["count"] = c.execute("SELECT COUNT(*) FROM ivr_menus WHERE owner_login_id=?", (u["id"],)).fetchone()[0]
        routes = [dict(r) for r in c.execute("SELECT did, ivr_id FROM inbound_routes WHERE ivr_id IS NOT NULL")]
    rows = _list_table(menus, "/ivr", True, active) or '<tr><td colspan="5" class="muted">No menus yet.</td></tr>'
    csrf = M._csrf_field(s)
    urows = "".join(
        f'<tr><td>{esc(u["username"])}</td><td>{esc(u["exten"])}</td><td>{u["count"]}</td>'
        f'<td><form method="post" action="/ivr/access/{u["id"]}" class="inline">{csrf}'
        f'<input name="quota" value="{u["quota"] if u["override"] else ""}" placeholder="default ({dq})" size="9" inputmode="numeric"> '
        f'<button class="link-btn">Save</button></form></td></tr>' for u in users)
    used_by = ", ".join(f'{esc(r["did"])}→{esc(next((m["name"] for m in menus if m["id"] == r["ivr_id"]), "?"))}' for r in routes)
    flash = ""
    if request.query_params.get("msg") == "saved":
        flash = '<div class="flash ok">Saved.</div>'
    body = f"""{flash}<h2>IVR menus</h2>
<p class="muted">System menus are yours to point inbound numbers at. User menus are built by users in their control panel.
{("Inbound numbers using menus: " + used_by) if used_by else "No inbound numbers use a menu yet — set one under Routes."}</p>
<table><tr><th>Menu</th><th>Owner</th><th>Keys</th><th>State</th><th></th></tr>{rows}</table>
<p><a class="btn" href="/ivr/new/edit">New system menu</a></p>
<h3>User access</h3>
<p class="muted">All user limits (minutes, voicemail, texts, recording, IVR) are also on <a href="/billing">Billing → User access</a>.
How many menus each user may create. This is where a billing system will plug in later
(it writes the same limits). 0 turns IVR off for that user; their menus are kept but stop answering.</p>
<form method="post" action="/ivr/access-default">{csrf}
<label>Default for users without their own limit: <input name="quota" value="{dq}" size="4" inputmode="numeric"></label>
<button class="btn ghost">Save default</button></form>
<table><tr><th>User</th><th>Ext</th><th>Menus</th><th>Limit (empty = default)</th></tr>{urows or '<tr><td colspan="4" class="muted">No users</td></tr>'}</table>"""
    return HTMLResponse(M.page("IVR", body, s["username"], s["role"], "ivr"))


def admin_edit(request: Request, menu_id: str):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    with M.db() as c:
        me = dict(c.execute("SELECT * FROM logins WHERE id=?", (s["id"],)).fetchone())
    m = None if menu_id == "new" else _menu(int(menu_id))
    if menu_id != "new" and not m:
        return RedirectResponse("/ivr")
    action = f"/ivr/{menu_id}/edit"
    owner_note = ""
    if m and m["owner_login_id"]:
        with M.db() as c:
            o = c.execute("SELECT username FROM logins WHERE id=?", (m["owner_login_id"],)).fetchone()
        owner_note = f'<p class="muted">Owned by user <b>{esc(o[0] if o else "?")}</b> — same limits as their own editor.</p>'
    body = f"<h2>{'Edit' if m else 'New system'} menu</h2>{owner_note}" + _editor(request, s, me, m, False, action, "/ivr")
    if m:
        body += (f'<form method="post" action="/ivr/{m["id"]}/delete" onsubmit="return confirm(\'Delete this menu? Inbound numbers using it will ring their extension list instead.\')">'
                 f'{M._csrf_field(s)}<button class="link-btn bad">Delete menu</button></form>')
    return HTMLResponse(M.page("IVR", body, s["username"], s["role"], "ivr"))


async def admin_save(request: Request, menu_id: str):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    await M._check_csrf(request, s)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "ivr")
    m = None if menu_id == "new" else _menu(int(menu_id))
    if menu_id != "new" and not m:
        return RedirectResponse("/ivr")
    owner = None
    if m and m["owner_login_id"]:
        with M.db() as c:
            owner = dict(c.execute("SELECT * FROM logins WHERE id=?", (m["owner_login_id"],)).fetchone())
    new_id, err = await _save(request, s, m["id"] if m else None, owner, False)
    if err:
        with M.db() as c:
            me = dict(c.execute("SELECT * FROM logins WHERE id=?", (s["id"],)).fetchone())
        mm = _menu(new_id) if new_id else None
        body = "<h2>Menu</h2>" + _editor(request, s, me, mm, False,
                                         f"/ivr/{new_id or 'new'}/edit", "/ivr", err)
        return HTMLResponse(M.page("IVR", body, s["username"], s["role"], "ivr"), status_code=400)
    return RedirectResponse("/ivr?msg=saved", status_code=303)


async def admin_delete(request: Request, menu_id: int):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    await M._check_csrf(request, s)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "ivr")
    m = _menu(menu_id)
    if m:
        _delete(m)
    return RedirectResponse("/ivr?msg=saved", status_code=303)


async def admin_access_user(request: Request, login_id: int):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    await M._check_csrf(request, s)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "ivr")
    form = await request.form()
    v = (form.get("quota") or "").strip()
    with M.db() as c:
        if v == "":
            ent.set_quota(c, login_id, "ivr_menus", None)
        elif v.isdigit() and int(v) <= 100:
            ent.set_quota(c, login_id, "ivr_menus", int(v))
    return RedirectResponse("/ivr?msg=saved", status_code=303)


async def admin_access_default(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    await M._check_csrf(request, s)
    if M._get_setting("safety_lock") == "1":
        return M._panel_locked(s, "ivr")
    form = await request.form()
    v = (form.get("quota") or "").strip()
    if v.isdigit() and int(v) <= 100:
        with M.db() as c:
            ent.set_default_quota(c, "ivr_menus", int(v))
    return RedirectResponse("/ivr?msg=saved", status_code=303)


def system_menus():
    """For the inbound-route editor: every menu, labelled."""
    return _menus(all_menus=True)


# ---------------------------------------------------------------- install

def install(app_module):
    global M
    M = app_module
    app = M.app
    html = dict(response_class=HTMLResponse)
    app.get("/api/ivr-greeting/{menu_id}")(greeting_audio)
    app.get("/ivr", **html)(admin_list)
    app.get("/ivr/{menu_id}/edit", **html)(admin_edit)
    app.post("/ivr/{menu_id}/edit")(admin_save)
    app.post("/ivr/{menu_id}/delete")(admin_delete)
    app.post("/ivr/access/{login_id}")(admin_access_user)
    app.post("/ivr/access-default")(admin_access_default)
    app.get("/ucp/ivr", **html)(user_list)
    app.get("/ucp/ivr/{menu_id}/edit", **html)(user_edit)
    app.post("/ucp/ivr/{menu_id}/edit")(user_save)
    app.post("/ucp/ivr/{menu_id}/delete")(user_delete)
