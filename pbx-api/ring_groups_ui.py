# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin Ring groups page (/ring-groups).

A ring group rings several extensions at once (first to answer gets the
call). Inbound routes can send callers to a group (Routes page), and a group
can have its own internal number so phones can dial it.

No answer:
  member : one member's voicemail box (default the first member)
  all    : every member gets their own copy
  ext / group / ivr / number : send the caller on to that destination
  none   : hang up
"""
import json
import re
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

M = None
EXT_RE = re.compile(r"^\d{2,8}$")
VM_MODES = (("member", "Voicemail: one member's box"), ("all", "Voicemail: every member gets a copy"),
            ("ext", "Send to another extension"), ("group", "Send to another ring group"),
            ("ivr", "Send to an IVR menu"), ("number", "Send to an outside number"),
            ("none", "Hang up (no voicemail)"))
NUM_RE = re.compile(r"^\+?\d{7,15}$")


def esc(x):
    return M.esc(x)


def _admin(request):
    s = M._sess(request)
    return s if s and s["role"] == "admin" else None


def _back(msg="", err="", gid=None):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    path = f"/ring-groups/{gid}/edit" if gid else "/ring-groups"
    return RedirectResponse(path + ("?" + q if q else ""), status_code=303)


def _members(row):
    try:
        v = json.loads(row["members"] or "[]")
        return [str(x) for x in v if str(x).strip()]
    except Exception:
        return []


def all_groups():
    with M.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ring_groups ORDER BY name")]


def _target_label(g):
    t = str(g.get("noanswer_target") or "")
    if g["vm_mode"] == "group" and t.isdigit():
        with M.db() as c:
            r = c.execute("SELECT name FROM ring_groups WHERE id=?", (int(t),)).fetchone()
        return r[0] if r else "(deleted group)"
    if g["vm_mode"] == "ivr" and t.isdigit():
        with M.db() as c:
            r = c.execute("SELECT name FROM ivr_menus WHERE id=?", (int(t),)).fetchone()
        return r[0] if r else "(deleted menu)"
    return t


def _users():
    with M.db() as c:
        return c.execute("SELECT id, username, exten, display_name, enabled FROM logins"
                         " WHERE exten != '' ORDER BY exten").fetchall()


def _flash(request):
    out = ""
    if request.query_params.get("msg"):
        out += f'<div class="flash ok">{esc(request.query_params["msg"])}</div>'
    if request.query_params.get("err"):
        out += f'<div class="flash bad">{esc(request.query_params["err"])}</div>'
    return out


def list_page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    names = {u["exten"]: (u["display_name"] or u["username"]) for u in _users()}
    with M.db() as c:
        used = {}
        for r in c.execute("SELECT did, group_id FROM inbound_routes WHERE group_id IS NOT NULL"):
            used.setdefault(r["group_id"], []).append(r["did"])
    rows = "".join(
        f"<tr><td><b>{esc(g['name'])}</b></td><td>{esc(g['exten'] or '-')}</td>"
        f"<td>{', '.join(esc(names.get(m, m)) + ' <span class=muted>' + esc(m) + '</span>' for m in _members(g)) or '-'}</td>"
        f"<td>{g['ring_seconds']}s</td><td>{esc(dict(VM_MODES).get(g['vm_mode'], g['vm_mode']))}"
        f"{(' (' + esc(names.get(g['vm_exten'], g['vm_exten'])) + ')') if g['vm_mode'] == 'member' and g['vm_exten'] else ''}"
        f"{(': ' + esc(_target_label(g))) if g['vm_mode'] in ('ext', 'group', 'ivr', 'number') else ''}</td>"
        f"<td>{esc(', '.join(used.get(g['id'], [])) or '-')}</td><td>{'yes' if g['enabled'] else 'no'}</td>"
        f"<td><a class='btn ghost' href='/ring-groups/{g['id']}/edit'>Edit</a></td></tr>"
        for g in all_groups()) or '<tr><td colspan="8" class="muted">No ring groups yet.</td></tr>'
    body = f"""{_flash(request)}<h2>Ring groups</h2>
<p class="muted">Ring several phones at once - the first to answer gets the call. Point a phone number at a group on
the <a href="/routes">Routes</a> page, or give the group an internal number so phones can dial it.</p>
<table><tr><th>Name</th><th>Internal number</th><th>Members (in order)</th><th>Ring time</th><th>No answer</th><th>Phone numbers</th><th>Enabled</th><th></th></tr>
{rows}</table>
<p><a href="/ring-groups/new/edit" class="btn">Add ring group</a></p>"""
    return HTMLResponse(M.page("Ring groups", body, s["username"], s["role"], "groups"))


def edit_page(request: Request, gid: str):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    g = None
    if gid != "new":
        with M.db() as c:
            g = c.execute("SELECT * FROM ring_groups WHERE id=?", (gid,)).fetchone()
        if not g:
            return _back(err="No such ring group.")
    members = _members(g) if g else []
    users = _users()
    # members first (in order), then everyone else
    order = members + [u["exten"] for u in users if u["exten"] not in members]
    by_ext = {u["exten"]: u for u in users}
    rows = ""
    for i, ex in enumerate(order):
        u = by_ext.get(ex)
        label = (esc(u["display_name"] or u["username"]) + f' <span class="muted">{esc(ex)}</span>'
                 + ('' if u["enabled"] else ' <span class="pill">disabled</span>')) if u else esc(ex) + ' <span class="pill bad">no such user</span>'
        pos = members.index(ex) + 1 if ex in members else ""
        rows += (f'<tr><td><input type="checkbox" name="m_{esc(ex)}" value="1" {"checked" if ex in members else ""}></td>'
                 f'<td>{label}</td><td><input name="o_{esc(ex)}" value="{pos}" size="3" inputmode="numeric" placeholder="-"></td></tr>')
    vm_mode = g["vm_mode"] if g else "member"
    vm_opts = "".join(f'<option value="{k}" {"selected" if k == vm_mode else ""}>{esc(t)}</option>' for k, t in VM_MODES)
    vm_ext = g["vm_exten"] if g else ""
    vm_user_opts = '<option value="">First member</option>' + "".join(
        f'<option value="{esc(u["exten"])}" {"selected" if u["exten"] == vm_ext else ""}>{esc(u["display_name"] or u["username"])} ({esc(u["exten"])})</option>'
        for u in users)
    tgt = str((g["noanswer_target"] if g and "noanswer_target" in g.keys() else "") or "")
    ext_opts = "".join(f'<option value="{esc(u["exten"])}" {"selected" if vm_mode == "ext" and u["exten"] == tgt else ""}>'
                       f'{esc(u["display_name"] or u["username"])} ({esc(u["exten"])})</option>' for u in users)
    grp_opts = "".join(f'<option value="{x["id"]}" {"selected" if vm_mode == "group" and str(x["id"]) == tgt else ""}>{esc(x["name"])}</option>'
                       for x in all_groups() if not g or x["id"] != g["id"])
    with M.db() as c:
        menus = c.execute("SELECT id, name FROM ivr_menus WHERE enabled=1 ORDER BY name").fetchall()
    ivr_opts = "".join(f'<option value="{m["id"]}" {"selected" if vm_mode == "ivr" and str(m["id"]) == tgt else ""}>{esc(m["name"])}</option>'
                       for m in menus)
    csrf = M._csrf_field(s)
    delete = (f'<form method="post" action="/ring-groups/{g["id"]}/delete" class="inline" '
              f'onsubmit="return confirm(\'Delete this ring group? Phone numbers pointing at it stop ringing anyone.\')">{csrf}'
              f'<button class="btn ghost danger">Delete group</button></form>') if g else ""
    body = f"""{_flash(request)}<h2>{'Edit' if g else 'Add'} ring group</h2>
<form method="post" action="/ring-groups/{gid}/edit">{csrf}
<label>Name<br><input name="name" value="{esc(g['name'] if g else '')}" required maxlength="40" placeholder="Front office"></label>
<label>Internal number <span class="muted">(optional - lets phones dial the group)</span><br><input name="exten" value="{esc((g['exten'] if g else '') or '')}" inputmode="numeric" size="8" placeholder="e.g. 600"></label>
<label>Ring for (seconds)<br><input name="ring_seconds" value="{g['ring_seconds'] if g else 30}" inputmode="numeric" size="5"></label>
<h3>Members</h3>
<p class="muted">Tick who rings. "Order" decides who is first (the default voicemail box); everyone rings at the same time.</p>
<table><tr><th>Rings</th><th>User</th><th>Order</th></tr>{rows}</table>
<h3>If nobody answers</h3>
<div class="rg-field"><b>What happens</b><br><select name="vm_mode" id="rg-mode">{vm_opts}</select></div>
<div class="rg-field rg-t" data-mode="member"><b>Whose voicemail box</b><br><select name="vm_exten">{vm_user_opts}</select></div>
<div class="rg-field rg-t" data-mode="all"><span class="muted">Each member gets their own copy of the message.</span></div>
<div class="rg-field rg-t" data-mode="ext"><b>Send to extension</b><br><select name="t_ext"><option value="">Choose…</option>{ext_opts}</select></div>
<div class="rg-field rg-t" data-mode="group"><b>Send to ring group</b><br><select name="t_group"><option value="">Choose…</option>{grp_opts}</select></div>
<div class="rg-field rg-t" data-mode="ivr"><b>Send to IVR menu</b><br><select name="t_ivr"><option value="">Choose…</option>{ivr_opts}</select></div>
<div class="rg-field rg-t" data-mode="number"><b>Send to outside number</b><br><input name="t_number" value="{esc(tgt if vm_mode == 'number' else '')}" inputmode="tel" placeholder="3105551234">
<br><span class="muted">Uses the first member's outside minutes.</span></div>
<div class="rg-field rg-t" data-mode="none"><span class="muted">The call ends - no voicemail.</span></div>
<p class="muted rg-t" data-mode="ext group ivr number">If that destination isn't available, the call goes to the first member's voicemail.</p>
<script>
(function () {{
  var sel = document.getElementById('rg-mode');
  function show() {{
    document.querySelectorAll('.rg-t').forEach(function (el) {{
      el.style.display = el.dataset.mode.split(' ').indexOf(sel.value) >= 0 ? '' : 'none';
    }});
  }}
  sel.addEventListener('change', show); show();
}})();
</script>
<style>.rg-field{{margin:10px 0}}.rg-field select,.rg-field input{{min-width:240px}}</style>
<label class="switch"><input type="checkbox" name="enabled" value="1" {"checked" if (not g or g['enabled']) else ""}> <span>Enabled</span></label>
<button class="btn">Save</button> <a class="btn ghost" href="/ring-groups">Cancel</a>
</form>
{delete}"""
    return HTMLResponse(M.page("Ring group", body, s["username"], s["role"], "groups"))


async def _post(request):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._get_setting("safety_lock") == "1":
        return s, M._panel_locked(s, "groups")
    return s, None


def _exten_clash(c, exten, gid):
    if c.execute("SELECT 1 FROM logins WHERE exten=?", (exten,)).fetchone():
        return "an extension"
    if c.execute("SELECT 1 FROM ivr_menus WHERE exten=?", (exten,)).fetchone():
        return "an IVR menu"
    if c.execute("SELECT 1 FROM ring_groups WHERE exten=? AND id!=?", (exten, gid or 0)).fetchone():
        return "another ring group"
    if exten in ("911", "933", "9911", "9933"):
        return "emergency dialing"
    return None


async def save(request: Request, gid: str):
    s, locked = await _post(request)
    if locked:
        return locked
    f = await request.form()
    back_id = None if gid == "new" else gid
    name = " ".join((f.get("name") or "").split())[:40]
    if not name:
        return _back(err="Give the group a name.", gid=back_id)
    exten = (f.get("exten") or "").strip()
    if exten and not EXT_RE.match(exten):
        return _back(err="Internal number must be 2-8 digits.", gid=back_id)
    try:
        secs = max(5, min(int(f.get("ring_seconds") or 30), 300))
    except ValueError:
        return _back(err="Ring time must be a number of seconds.", gid=back_id)
    users = {u["exten"] for u in _users()}
    picked = []
    for ex in users:
        if f.get(f"m_{ex}"):
            try:
                o = int(f.get(f"o_{ex}") or 999)
            except ValueError:
                o = 999
            picked.append((o, ex))
    members = [ex for _, ex in sorted(picked)]
    if not members:
        return _back(err="Tick at least one member.", gid=back_id)
    vm_mode = f.get("vm_mode") if f.get("vm_mode") in dict(VM_MODES) else "member"
    vm_ext = (f.get("vm_exten") or "").strip()
    if vm_ext and vm_ext not in users:
        return _back(err="Pick an existing user for the voicemail box.", gid=back_id)
    enabled = 1 if f.get("enabled") else 0
    target = ""
    if vm_mode == "ext":
        target = (f.get("t_ext") or "").strip()
        if target not in users:
            return _back(err="Pick the extension to send unanswered calls to.", gid=back_id)
    elif vm_mode == "group":
        target = (f.get("t_group") or "").strip()
        if not target.isdigit() or (gid != "new" and target == str(gid)):
            return _back(err="Pick another ring group to send unanswered calls to.", gid=back_id)
    elif vm_mode == "ivr":
        target = (f.get("t_ivr") or "").strip()
        if not target.isdigit():
            return _back(err="Pick the IVR menu to send unanswered calls to.", gid=back_id)
    elif vm_mode == "number":
        target = re.sub(r"[\s().-]", "", f.get("t_number") or "")
        if not NUM_RE.match(target) or target.lstrip("+") in ("911", "933"):
            return _back(err="Enter the outside number (7-15 digits).", gid=back_id)
    with M.db() as c:
        if exten:
            clash = _exten_clash(c, exten, None if gid == "new" else int(gid))
            if clash:
                return _back(err=f"{exten} is already used by {clash}.", gid=back_id)
        dup = c.execute("SELECT 1 FROM ring_groups WHERE name=? AND id!=?",
                        (name, 0 if gid == "new" else int(gid))).fetchone()
        if dup:
            return _back(err="Another group already has that name.", gid=back_id)
        vals = (name, exten or None, json.dumps(members), secs, vm_mode, vm_ext, target, enabled)
        if gid == "new":
            c.execute("INSERT INTO ring_groups (name, exten, members, ring_seconds, vm_mode, vm_exten, noanswer_target, enabled)"
                      " VALUES (?,?,?,?,?,?,?,?)", vals)
        else:
            if not c.execute("SELECT 1 FROM ring_groups WHERE id=?", (int(gid),)).fetchone():
                return _back(err="No such ring group.")
            c.execute("UPDATE ring_groups SET name=?, exten=?, members=?, ring_seconds=?, vm_mode=?, vm_exten=?,"
                      " noanswer_target=?, enabled=? WHERE id=?", vals + (int(gid),))
        c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)",
                  (s["username"], "ringgroup.save", f"{name}: {members} vm={vm_mode}"))
        c.commit()
    return _back(msg=f"Ring group '{name}' saved.")


async def delete(request: Request, gid: int):
    s, locked = await _post(request)
    if locked:
        return locked
    with M.db() as c:
        g = c.execute("SELECT name FROM ring_groups WHERE id=?", (gid,)).fetchone()
        if not g:
            return _back(err="No such ring group.")
        c.execute("UPDATE inbound_routes SET group_id=NULL WHERE group_id=?", (gid,))
        c.execute("UPDATE ring_groups SET vm_mode='member', noanswer_target='' WHERE vm_mode='group' AND noanswer_target=?",
                  (str(gid),))
        c.execute("DELETE FROM ring_groups WHERE id=?", (gid,))
        c.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)",
                  (s["username"], "ringgroup.delete", g["name"]))
        c.commit()
    return _back(msg=f"Ring group '{g['name']}' deleted.")


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.get("/ring-groups", response_class=HTMLResponse)(list_page)
    app.get("/ring-groups/{gid}/edit", response_class=HTMLResponse)(edit_page)
    app.post("/ring-groups/{gid}/edit")(save)
    app.post("/ring-groups/{gid}/delete")(delete)
