# SPDX-License-Identifier: GPL-2.0-or-later
"""Admin -> Call flow: time conditions and caller ID rules.

Time conditions: business hours per weekday, holidays and a manual
Open / Closed switch; each state sends calls somewhere (ring group,
extension, voicemail, IVR menu, another time condition...). A phone number
(Routes), an IVR key or a caller rule can point at one.

Caller rules: "if the caller ID matches, do this", checked before a number's
normal route: an exact number, the start of a number (area code) or no
caller ID; send it to a ring group, voicemail, a menu, a time condition,
"not in service", hang up, or ring the normal route without voicemail.

The decisions themselves live in shared/callflow.py (used by pbx-brain).
Installed by app.py: callflow_ui.install(app_module).
"""
import json
import urllib.parse

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import callflow as cf

M = None
TZ_CHOICES = ["", "America/Los_Angeles", "America/Denver", "America/Phoenix", "America/Chicago",
              "America/New_York", "America/Anchorage", "Pacific/Honolulu", "America/Toronto",
              "America/Vancouver", "America/Halifax", "Europe/London", "UTC"]
TC_DEST_TYPES = ["rgroup", "ext", "group", "vm", "ivr", "tc", "disconnected", "hangup"]
RULE_DEST_TYPES = ["rgroup", "ext", "group", "vm", "ivr", "tc", "route_novm", "disconnected", "hangup"]
MATCH_LABEL = {"exact": "This number", "prefix": "Numbers starting with (area code / prefix)",
               "anonymous": "No caller ID (blocked, private, anonymous)"}
MAX_RULES = 500


def esc(x):
    return M.esc(x)


def _admin(request):
    s = M._sess(request)
    if not s or s["role"] != "admin":
        return None
    return s


def _back(msg="", err="", path="/callflow"):
    q = urllib.parse.urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(path + ("?" + q if q else ""), status_code=303)


# ---------------------------------------------------------------- data

def _q(sql, args=()):
    with M.db() as c:
        return [dict(r) for r in c.execute(sql, args)]


def _exts():
    return _q("SELECT id, exten, display_name, username FROM logins WHERE enabled=1 AND exten!='' ORDER BY exten")


def _groups():
    return _q("SELECT id, name FROM ring_groups ORDER BY name")


def _menus():
    return _q("SELECT id, name FROM ivr_menus WHERE owner_login_id IS NULL ORDER BY name")


def _tcs():
    return _q("SELECT * FROM time_conditions ORDER BY name")


def _dids():
    return [r["did"] for r in _q("SELECT did FROM inbound_routes ORDER BY did")]


def _names():
    n = {("rgroup", str(g["id"])): g["name"] for g in _groups()}
    n.update({("ivr", str(m["id"])): m["name"] for m in _menus()})
    n.update({("tc", str(t["id"])): t["name"] for t in _tcs()})
    return n


# ---------------------------------------------------------------- destination picker

def dest_picker(prefix, dest, types, allow_none=False, none_label="", exclude_tc=None):
    """Destination select plus the field for its target (shown by JS)."""
    dest = dest or {}
    t = dest.get("type") or ("" if allow_none else types[0])
    tg = str(dest.get("target") or "")
    opts = ([f'<option value="" {"selected" if not t else ""}>{esc(none_label)}</option>'] if allow_none else [])
    opts += [f'<option value="{k}" {"selected" if k == t else ""}>{esc(cf.DEST_TYPES[k])}</option>' for k in types]

    def sel(kind, rows, key, label):
        o = "".join(f'<option value="{esc(r[key])}" {"selected" if t == kind and tg == str(r[key]) else ""}>'
                    f'{esc(label(r))}</option>' for r in rows)
        return f'<select name="{prefix}_{kind}" class="dt dt-{kind}">{o or "<option value=\'\'>(none yet)</option>"}</select>'
    exts = _exts()
    ext_label = lambda e: f'{e["exten"]} {e["display_name"] or e["username"]}'
    tcs = [x for x in _tcs() if str(x["id"]) != str(exclude_tc or "")]
    return (f'<div class="dest"><select name="{prefix}_type" class="dtype">{"".join(opts)}</select>'
            + sel("rgroup", _groups(), "id", lambda g: g["name"])
            + sel("ext", exts, "exten", ext_label)
            + f'<input name="{prefix}_group" class="dt dt-group" placeholder="e.g. 8800,8801" '
              f'value="{esc(tg) if t == "group" else ""}">'
            + sel("vm", exts, "exten", ext_label)
            + sel("ivr", _menus(), "id", lambda m: m["name"])
            + sel("tc", tcs, "id", lambda x: x["name"])
            + "</div>")


DEST_JS = """<script>
function syncDest(d){const t=d.querySelector('.dtype').value;
 d.querySelectorAll('.dt').forEach(e=>{e.style.display=e.classList.contains('dt-'+t)?'':'none'});}
document.querySelectorAll('.dest').forEach(d=>{syncDest(d);
 d.querySelector('.dtype').addEventListener('change',()=>syncDest(d));});
</script>"""


def parse_dest(form, prefix, types, allow_none=False, exclude_tc=None):
    """(dest dict or None, error)."""
    t = form.get(prefix + "_type") or ""
    if not t:
        return (None, None) if allow_none else (None, "Pick where calls go.")
    if t not in types:
        return None, "Unknown destination."
    if t in ("route_novm", "disconnected", "hangup"):
        return {"type": t}, None
    if t == "group":
        exts = {e["exten"] for e in _exts()}
        lst = [x for x in (form.get(prefix + "_group") or "").replace(" ", "").split(",") if x]
        if not lst or len(lst) > 10 or any(x not in exts for x in lst):
            return None, "A list needs 1-10 existing extensions, separated by commas."
        return {"type": "group", "target": ",".join(dict.fromkeys(lst))}, None
    v = str(form.get(prefix + "_" + t) or "")
    valid = {
        "rgroup": {str(g["id"]) for g in _groups()},
        "ext": {e["exten"] for e in _exts()},
        "vm": {e["exten"] for e in _exts()},
        "ivr": {str(m["id"]) for m in _menus()},
        "tc": {str(x["id"]) for x in _tcs() if str(x["id"]) != str(exclude_tc or "")},
    }[t]
    if v not in valid:
        return None, {"rgroup": "Pick a ring group.", "ext": "Pick an extension.", "vm": "Pick a voicemail box.",
                      "ivr": "Pick an IVR menu.", "tc": "Pick another time condition."}[t]
    return {"type": t, "target": int(v) if t in ("rgroup", "ivr", "tc") else v}, None


# ---------------------------------------------------------------- overview page

STATE_PILL = {"open": ("Open", "ok"), "closed": ("Closed", "bad"), "holiday": ("Holiday", "warn")}


def list_page(request: Request):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    names = _names()
    csrf = M._csrf_field(s)
    flash = ""
    if request.query_params.get("msg"):
        flash = f'<div class="flash ok">{esc(request.query_params["msg"])}</div>'
    if request.query_params.get("err"):
        flash = f'<div class="flash bad">{esc(request.query_params["err"])}</div>'
    trs = []
    for tc in _tcs():
        dest, state, why = cf.tc_dest(tc)
        label, cls = STATE_PILL[state]
        hours = cf.jload(tc["hours"], {})
        days = "; ".join(f'{d[:1].upper()}{d[1:]} {cf.periods_text(hours.get(d))}' for d in cf.DAYS if hours.get(d))
        ov = tc["override"] or "auto"
        btn = lambda mode, txt: (f'<button class="btn {"" if ov == mode else "ghost"}" name="mode" value="{mode}"'
                                 f' title="{esc(txt)}">{esc(mode.capitalize() if mode != "auto" else "Auto")}</button>')
        trs.append(
            f'<tr><td><b>{esc(tc["name"])}</b>{"" if tc["enabled"] else " <span class=muted>(off)</span>"}'
            f'<br><span class="muted">{esc(days or "no opening hours set")}</span></td>'
            f'<td><span class="pill {cls}">{label}</span><br><span class="muted">{esc(why)}</span>'
            f'<br>&rarr; {esc(cf.describe_dest(dest, names))}</td>'
            f'<td><form method="post" action="/callflow/tc/{tc["id"]}/override" class="inline">{csrf}'
            f'{btn("auto", "Follow the hours and holidays")}{btn("open", "Open now, whatever the hours")}'
            f'{btn("closed", "Closed now, whatever the hours")}</form></td>'
            f'<td><a class="btn ghost" href="/callflow/tc/{tc["id"]}/edit">Edit</a></td></tr>')
    rules = _q("SELECT * FROM caller_rules ORDER BY (did=''), sort, id")
    rtr = []
    for r in rules:
        what = ("No caller ID" if r["match"] == "anonymous" else
                (f'Starts with {r["pattern"]}' if r["match"] == "prefix" else r["pattern"]))
        rtr.append(
            f'<tr><td>{esc(r["did"] or "Any number")}</td><td><b>{esc(what)}</b>'
            f'{("<br><span class=muted>" + esc(r["note"]) + "</span>") if r["note"] else ""}</td>'
            f'<td>{esc(cf.describe_dest(cf.jload(r["dest"], {}), names))}</td>'
            f'<td>{r["hits"]}{("<br><span class=muted>last " + esc(r["last_hit"]) + "</span>") if r["last_hit"] else ""}</td>'
            f'<td>{"on" if r["enabled"] else "<span class=muted>off</span>"}</td>'
            f'<td><a class="btn ghost" href="/callflow/rule/{r["id"]}/edit">Edit</a></td></tr>')
    body = f"""{flash}<h2>Call flow</h2>
<p class="muted">Time conditions send calls one way during business hours and another way after hours or on
holidays. Caller rules pick out particular callers before a number's normal route. Point a phone number at a time
condition under <a href="/routes">Routes</a>, or use one from an <a href="/ivr">IVR menu</a> key.</p>
<h3 id="time-conditions">Time conditions</h3>
<div class="scrollbox"><table><tr><th>Name</th><th>Right now</th><th>Switch</th><th></th></tr>
{''.join(trs) or '<tr><td colspan="4" class="muted">No time conditions yet.</td></tr>'}</table></div>
<p><a class="btn" href="/callflow/tc/new/edit">New time condition</a></p>
<h3 id="caller-rules" style="margin-top:24px">Caller rules</h3>
<p class="muted">Checked first on every incoming call to your numbers; the first match wins (rules for one number
before rules for any number). Each user also has their own list under My Phone &rarr; Settings &rarr; Call filtering.</p>
<div class="scrollbox"><table><tr><th>Calls to</th><th>Caller</th><th>Goes to</th><th>Used</th><th></th><th></th></tr>
{''.join(rtr) or '<tr><td colspan="6" class="muted">No caller rules yet.</td></tr>'}</table></div>
<p><a class="btn" href="/callflow/rule/new/edit">New caller rule</a></p>"""
    return HTMLResponse(M.page("Call flow", body, s["username"], s["role"], "callflow"))


# ---------------------------------------------------------------- time conditions

def _tc(tid):
    rows = _q("SELECT * FROM time_conditions WHERE id=?", (tid,))
    return rows[0] if rows else None


def _tc_form(s, tc, err="", raw=None):
    """raw: the posted form, to show again with an error (keeps what was typed)."""
    new = tc is None or not tc.get("id")
    tc = tc or {"id": None, "name": "", "tz": "", "override": "auto", "enabled": 1,
                "hours": json.dumps({d: [["09:00", "17:00"]] for d in cf.DAYS[:5]}), "holidays": "[]",
                "open_dest": "{}", "closed_dest": "{}", "holiday_dest": ""}
    hours = cf.jload(tc["hours"], {})
    day_val = (lambda d: raw.get("h_" + d) or "") if raw is not None else (lambda d: cf.periods_text(hours.get(d)))
    days = "".join(
        f'<tr><td>{cf.DAY_LABEL[d]}</td><td><input name="h_{d}" value="{esc(day_val(d))}" '
        f'placeholder="closed" size="28"></td></tr>' for d in cf.DAYS)
    hol = (raw.get("holidays") or "") if raw is not None else \
        "\n".join(f'{h.get("date", "")} {h.get("name", "")}'.strip() for h in cf.jload(tc["holidays"], []))
    tzs = list(TZ_CHOICES)
    if tc["tz"] and tc["tz"] not in tzs:
        tzs.append(tc["tz"])
    tz_opts = "".join(f'<option value="{esc(z)}" {"selected" if z == (tc["tz"] or "") else ""}>'
                      f'{esc(z or "Server time")}</option>' for z in tzs)
    ov_opts = "".join(f'<option value="{k}" {"selected" if k == (tc["override"] or "auto") else ""}>{v}</option>'
                      for k, v in (("auto", "Auto: follow the hours and holidays"),
                                   ("open", "Open: always use the open destination"),
                                   ("closed", "Closed: always use the closed destination")))
    tid = tc["id"]
    csrf = M._csrf_field(s)
    errh = f'<div class="flash bad">{esc(err)}</div>' if err else ""
    delete = "" if new else (
        f'<form method="post" action="/callflow/tc/{tid}/delete" onsubmit="return confirm(\'Delete this time condition?\')">'
        f'{csrf}<button class="btn ghost danger">Delete</button></form>')
    return f"""{errh}<h2>{'New' if new else 'Edit'} time condition</h2>
<form method="post" action="/callflow/tc/{tid or 'new'}/edit" class="panel" style="max-width:720px">{csrf}
<label>Name<br><input name="name" value="{esc(tc['name'])}" required maxlength="40" placeholder="e.g. Office hours"></label>
<label>Time zone<br><select name="tz">{tz_opts}</select></label>
<h3>Open hours</h3>
<p class="muted">One or more ranges per day, like <code>9:00-17:00</code> or <code>9:00-12:00, 13:00-17:00</code>;
<code>24h</code> for all day; empty = closed. A range can run past midnight (<code>22:00-02:00</code>).</p>
<table class="kv">{days}</table>
<h3>Holidays</h3>
<p class="muted">One per line: <code>2026-12-25 Christmas</code> for one date, or <code>07-04 Independence Day</code> for
every year. These days use the holiday destination (or the closed one).</p>
<textarea name="holidays" rows="5" placeholder="2026-12-25 Christmas&#10;01-01 New Year's Day">{esc(hol)}</textarea>
<h3>Where calls go</h3>
<label>When open</label>{dest_picker("open", cf.jload(tc["open_dest"], {}), TC_DEST_TYPES, exclude_tc=tid)}
<label>When closed</label>{dest_picker("closed", cf.jload(tc["closed_dest"], {}), TC_DEST_TYPES, exclude_tc=tid)}
<label>On holidays</label>{dest_picker("holiday", cf.jload(tc["holiday_dest"], {}), TC_DEST_TYPES, allow_none=True,
                                       none_label="Same as closed", exclude_tc=tid)}
<label>Switch<br><select name="override">{ov_opts}</select></label>
<p class="muted">Also on the Call flow page, for "we're closing early today" or "open this Saturday".</p>
<label class="switch"><input type="checkbox" name="enabled" value="1" {"checked" if tc["enabled"] else ""}> <span>Enabled</span></label>
<p><button class="btn">Save</button> <a class="btn ghost" href="/callflow">Cancel</a></p>
</form>{delete}{DEST_JS}"""


def tc_edit(request: Request, tid: str):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    tc = None if tid == "new" else _tc(int(tid) if tid.isdigit() else 0)
    if tid != "new" and not tc:
        return _back(err="No such time condition.")
    return HTMLResponse(M.page("Call flow", _tc_form(s, tc), s["username"], s["role"], "callflow"))


def parse_tc(form, tid=None):
    name = (form.get("name") or "").strip()
    if not name or len(name) > 40 or any(ord(ch) < 32 for ch in name):
        return None, "Give it a name (up to 40 characters)."
    tz = (form.get("tz") or "").strip()
    if tz:
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)
        except Exception:
            return None, "Unknown time zone."
    hours = {}
    for d in cf.DAYS:
        try:
            p = cf.parse_periods(form.get("h_" + d))
        except ValueError as e:
            return None, f"{cf.DAY_LABEL[d]}: {e}"
        if p:
            hours[d] = p
    hols = []
    for line in (form.get("holidays") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        date, _, nm = line.partition(" ")
        ok = False
        for fmt in ("%Y-%m-%d", "%m-%d"):
            try:
                import datetime
                datetime.datetime.strptime(("2000-" + date) if fmt == "%m-%d" else date, "%Y-%m-%d")
                ok = (fmt == "%Y-%m-%d" and len(date) == 10) or (fmt == "%m-%d" and len(date) == 5)
            except ValueError:
                pass
            if ok:
                break
        if not ok:
            return None, f'Holiday "{line}": start with a date like 2026-12-25 or 12-25.'
        hols.append({"date": date, "name": nm.strip()[:40]})
    if len(hols) > 100:
        return None, "Up to 100 holidays."
    dests = {}
    for key, none_ok in (("open", False), ("closed", False), ("holiday", True)):
        d, e = parse_dest(form, key, TC_DEST_TYPES, allow_none=none_ok, exclude_tc=tid)
        if e:
            return None, f"When {key}: {e}"
        dests[key] = json.dumps(d) if d else ""
    ov = form.get("override") if form.get("override") in ("auto", "open", "closed") else "auto"
    return {"name": name, "tz": tz, "hours": json.dumps(hours), "holidays": json.dumps(hols),
            "open_dest": dests["open"], "closed_dest": dests["closed"], "holiday_dest": dests["holiday"],
            "override": ov, "enabled": 1 if form.get("enabled") else 0}, None


async def tc_save(request: Request, tid: str):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "callflow")
    form = await request.form()
    old = None if tid == "new" else _tc(int(tid) if tid.isdigit() else 0)
    if tid != "new" and not old:
        return _back(err="No such time condition.")
    v, err = parse_tc(form, old["id"] if old else None)
    if not err and _q("SELECT 1 FROM time_conditions WHERE name=? AND id!=?", (v["name"], old["id"] if old else 0)):
        err = "There's already a time condition with that name."
    if err:
        draft = {"id": old["id"] if old else None, "name": form.get("name") or "", "tz": form.get("tz") or "",
                 "override": form.get("override") or "auto", "enabled": 1 if form.get("enabled") else 0,
                 "hours": "{}", "holidays": "[]"}
        for key in ("open", "closed", "holiday"):
            d, _e = parse_dest(form, key, TC_DEST_TYPES, allow_none=True, exclude_tc=draft["id"])
            draft[key + "_dest"] = json.dumps(d) if d else (old or {}).get(key + "_dest", "")
        return HTMLResponse(M.page("Call flow", _tc_form(s, draft, err, raw=form), s["username"], s["role"],
                                   "callflow"), status_code=400)
    cols = ["name", "tz", "hours", "holidays", "open_dest", "closed_dest", "holiday_dest", "override", "enabled"]
    with M.db() as c:
        if old:
            c.execute(f"UPDATE time_conditions SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                      tuple(v[k] for k in cols) + (old["id"],))
        else:
            c.execute(f"INSERT INTO time_conditions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      tuple(v[k] for k in cols))
        c.commit()
    M._audit_log(s["username"], "callflow.tc.save", v["name"])
    return _back(msg=f"Saved {v['name']}.")


async def tc_override(request: Request, tid: int):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "callflow")
    mode = (await request.form()).get("mode")
    if mode not in ("auto", "open", "closed"):
        return _back(err="Pick Auto, Open or Closed.")
    tc = _tc(tid)
    if not tc:
        return _back(err="No such time condition.")
    with M.db() as c:
        c.execute("UPDATE time_conditions SET override=? WHERE id=?", (mode, tid))
        c.commit()
    M._audit_log(s["username"], "callflow.tc.override", f"{tc['name']} -> {mode}")
    return _back(msg=f"{tc['name']}: {'following the hours' if mode == 'auto' else 'set to ' + mode.capitalize()}.")


def _uses(kind, ident):
    """Where a time condition is used: [description]."""
    ident = str(ident)
    out = []
    for r in _q("SELECT did FROM inbound_routes WHERE tc_id=?", (ident,)):
        out.append(f"number {r['did']}")

    def hit(js):
        d = cf.jload(js, {})
        return d.get("type") == kind and str(d.get("target")) == ident
    for m in _q("SELECT name, options, fallback, invalid_fallback FROM ivr_menus"):
        opts = cf.jload(m["options"], {})
        if any(isinstance(d, dict) and d.get("type") == kind and str(d.get("target")) == ident for d in opts.values()) \
                or hit(m["fallback"]) or hit(m.get("invalid_fallback") or ""):
            out.append(f"IVR menu {m['name']}")
    for r in _q("SELECT id, dest FROM caller_rules"):
        if hit(r["dest"]):
            out.append(f"caller rule #{r['id']}")
    for t in _tcs():
        if str(t["id"]) != ident and any(hit(t[k]) for k in ("open_dest", "closed_dest", "holiday_dest")):
            out.append(f"time condition {t['name']}")
    return out


async def tc_delete(request: Request, tid: int):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "callflow")
    tc = _tc(tid)
    if not tc:
        return _back(err="No such time condition.")
    used = _uses("tc", tid)
    if used:
        return _back(err=f"{tc['name']} is still used by: {', '.join(used)}. Change those first.")
    with M.db() as c:
        c.execute("DELETE FROM time_conditions WHERE id=?", (tid,))
        c.commit()
    M._audit_log(s["username"], "callflow.tc.delete", tc["name"])
    return _back(msg=f"Deleted {tc['name']}.")


# ---------------------------------------------------------------- caller rules

def _rule(rid):
    rows = _q("SELECT * FROM caller_rules WHERE id=?", (rid,))
    return rows[0] if rows else None


def _rule_form(s, r, err=""):
    new = r is None or not r.get("id")
    r = r or {"id": None, "did": "", "match": "exact", "pattern": "", "dest": "{}", "note": "",
              "sort": 0, "enabled": 1}
    did_opts = '<option value="">Any of my numbers</option>' + "".join(
        f'<option value="{esc(d)}" {"selected" if cf.norm(d) == cf.norm(r["did"]) and r["did"] else ""}>{esc(d)}</option>'
        for d in _dids())
    m_opts = "".join(f'<option value="{k}" {"selected" if k == r["match"] else ""}>{esc(v)}</option>'
                     for k, v in MATCH_LABEL.items())
    csrf = M._csrf_field(s)
    errh = f'<div class="flash bad">{esc(err)}</div>' if err else ""
    delete = "" if new else (
        f'<form method="post" action="/callflow/rule/{r["id"]}/delete" onsubmit="return confirm(\'Delete this rule?\')">'
        f'{csrf}<button class="btn ghost danger">Delete</button></form>')
    return f"""{errh}<h2>{'New' if new else 'Edit'} caller rule</h2>
<form method="post" action="/callflow/rule/{r['id'] or 'new'}/edit" class="panel" style="max-width:640px">{csrf}
<label>Calls to<br><select name="did">{did_opts}</select></label>
<label>When the caller is<br><select name="match" onchange="document.getElementById('pat').style.display=this.value=='anonymous'?'none':''">{m_opts}</select></label>
<div id="pat" style="{'display:none' if r['match'] == 'anonymous' else ''}">
<label>Number<br><input name="pattern" value="{esc(r['pattern'])}" placeholder="(555) 555-5555, or 559 for an area code" inputmode="tel" maxlength="20"></label>
</div>
<label>Send the call to</label>
{dest_picker("dest", cf.jload(r["dest"], {}), RULE_DEST_TYPES)}
<p class="muted"><b>Normal route, but no voicemail</b>: the call rings exactly as it would, but if nobody answers
(or everyone declines) it hangs up instead of going to voicemail.</p>
<label>Note (optional)<br><input name="note" value="{esc(r['note'])}" maxlength="80" placeholder="e.g. Ex-customer, sales calls"></label>
<label>Order<br><input name="sort" value="{int(r.get('sort') or 0)}" inputmode="numeric" size="5"></label>
<p class="muted">Lower numbers are checked first (rules for one number always come before rules for any number).</p>
<label class="switch"><input type="checkbox" name="enabled" value="1" {"checked" if r["enabled"] else ""}> <span>Enabled</span></label>
<p><button class="btn">Save</button> <a class="btn ghost" href="/callflow#caller-rules">Cancel</a></p>
</form>{delete}{DEST_JS}"""


def rule_edit(request: Request, rid: str):
    s = _admin(request)
    if not s:
        return RedirectResponse("/login")
    r = None if rid == "new" else _rule(int(rid) if rid.isdigit() else 0)
    if rid != "new" and not r:
        return _back(err="No such rule.")
    return HTMLResponse(M.page("Call flow", _rule_form(s, r), s["username"], s["role"], "callflow"))


def parse_rule(form):
    did = cf.norm(form.get("did") or "")
    if did and did not in {cf.norm(d) for d in _dids()}:
        return None, "Pick one of your numbers."
    match = form.get("match") if form.get("match") in MATCH_LABEL else "exact"
    pattern = ""
    if match != "anonymous":
        pattern = cf.norm(form.get("pattern") or "") if match == "exact" else cf.digits(form.get("pattern") or "")
        if match == "exact" and not 3 <= len(pattern) <= 15:
            return None, "Enter the caller's number."
        if match == "prefix" and not 1 <= len(pattern) <= 10:
            return None, "Enter the start of the number, e.g. 559 or 1800."
    d, e = parse_dest(form, "dest", RULE_DEST_TYPES)
    if e:
        return None, e
    try:
        sort = max(-1000, min(int(form.get("sort") or 0), 1000))
    except ValueError:
        sort = 0
    return {"did": did, "match": match, "pattern": pattern, "dest": json.dumps(d),
            "note": (form.get("note") or "").strip()[:80], "sort": sort,
            "enabled": 1 if form.get("enabled") else 0}, None


async def rule_save(request: Request, rid: str):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "callflow")
    form = await request.form()
    old = None if rid == "new" else _rule(int(rid) if rid.isdigit() else 0)
    if rid != "new" and not old:
        return _back(err="No such rule.")
    v, err = parse_rule(form)
    if not err and not old and _q("SELECT COUNT(*) AS n FROM caller_rules")[0]["n"] >= MAX_RULES:
        err = f"Up to {MAX_RULES} rules."
    if err:
        draft = dict(old or {"id": None}, did=form.get("did") or "", match=form.get("match") or "exact",
                     pattern=form.get("pattern") or "", note=form.get("note") or "",
                     sort=form.get("sort") or 0, enabled=1 if form.get("enabled") else 0,
                     dest=(old or {}).get("dest", "{}"))
        return HTMLResponse(M.page("Call flow", _rule_form(s, draft, err), s["username"], s["role"], "callflow"),
                            status_code=400)
    cols = ["did", "match", "pattern", "dest", "note", "sort", "enabled"]
    with M.db() as c:
        if old:
            c.execute(f"UPDATE caller_rules SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                      tuple(v[k] for k in cols) + (old["id"],))
        else:
            c.execute(f"INSERT INTO caller_rules ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      tuple(v[k] for k in cols))
        c.commit()
    M._audit_log(s["username"], "callflow.rule.save", f"{v['match']} {v['pattern']} -> {v['dest']}")
    return _back(msg="Caller rule saved.", path="/callflow")


async def rule_delete(request: Request, rid: int):
    s = _admin(request)
    if not s:
        raise HTTPException(403)
    await M._check_csrf(request, s)
    if M._admin_locked():
        return M._panel_locked(s, "callflow")
    with M.db() as c:
        c.execute("DELETE FROM caller_rules WHERE id=?", (rid,))
        c.commit()
    M._audit_log(s["username"], "callflow.rule.delete", str(rid))
    return _back(msg="Caller rule deleted.")


# ---------------------------------------------------------------- Routes form hook

def route_options(cur_tc):
    """<option>s for the Routes form's "Send callers to" select."""
    return "".join(f'<option value="t{t["id"]}" {"selected" if t["id"] == cur_tc else ""}>'
                   f'Time condition: {esc(t["name"])}</option>' for t in _tcs())


def tc_name(tid):
    t = _tc(tid) if tid else None
    return t["name"] if t else "(deleted)"


def install(app_module):
    global M
    M = app_module
    app = M.app
    app.get("/callflow", response_class=HTMLResponse)(list_page)
    app.get("/callflow/tc/{tid}/edit", response_class=HTMLResponse)(tc_edit)
    app.post("/callflow/tc/{tid}/edit")(tc_save)
    app.post("/callflow/tc/{tid}/override")(tc_override)
    app.post("/callflow/tc/{tid}/delete")(tc_delete)
    app.get("/callflow/rule/{rid}/edit", response_class=HTMLResponse)(rule_edit)
    app.post("/callflow/rule/{rid}/edit")(rule_save)
    app.post("/callflow/rule/{rid}/delete")(rule_delete)
