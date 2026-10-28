#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Call flow: time conditions, caller ID rules, users' ring-no-voicemail and
VIP entries, and IVR no-key vs wrong-key handling.

1. the decision logic (shared/callflow.py) at fixed times
2. pbx-brain routing with a fake ARI (no Asterisk)
3. the admin pages (Call flow, Routes, IVR) on a throwaway DB

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-callflow.py      # run from the repo root; exits non-zero on failure
"""
import datetime, itertools, json, os, re, sqlite3, sys
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "shared"))
import callflow as cf
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)

# ------------------------------------------------------------ 1. logic
D = datetime.datetime
tc = {"override": "auto", "tz": "", "hours": json.dumps({"mon": [["09:00", "17:00"]], "fri": [["22:00", "02:00"]]}),
      "holidays": json.dumps([{"date": "12-25", "name": "Christmas"}, {"date": "2026-11-26", "name": "Thanksgiving"}]),
      "open_dest": '{"type":"rgroup","target":1}', "closed_dest": '{"type":"vm","target":"8800"}', "holiday_dest": ""}
mon = D(2026, 10, 26)                      # a Monday
check("Monday 10:00 open", cf.tc_state(tc, mon.replace(hour=10))[0] == "open")
check("Monday 17:00 closed (end is exclusive)", cf.tc_state(tc, mon.replace(hour=17))[0] == "closed")
check("Monday 08:59 closed", cf.tc_state(tc, mon.replace(hour=8, minute=59))[0] == "closed")
check("Tuesday closed (no hours)", cf.tc_state(tc, D(2026, 10, 27, 10))[0] == "closed")
check("Friday 23:00 open (overnight)", cf.tc_state(tc, D(2026, 10, 30, 23))[0] == "open")
check("Saturday 01:30 open (Friday's overnight)", cf.tc_state(tc, D(2026, 10, 31, 1, 30))[0] == "open")
check("Saturday 02:00 closed", cf.tc_state(tc, D(2026, 10, 31, 2))[0] == "closed")
check("yearly holiday", cf.tc_state(tc, D(2027, 12, 25, 10))[0] == "holiday")
d, st, why = cf.tc_dest(tc, D(2026, 11, 26, 10))
check("one-day holiday, no holiday choice -> closed destination", st == "holiday" and d == {"type": "vm", "target": "8800"}
      and why == "Thanksgiving", (d, st, why))
check("override closed beats hours", cf.tc_state(dict(tc, override="closed"), mon.replace(hour=10))[0] == "closed")
check("override open beats hours", cf.tc_state(dict(tc, override="open"), D(2026, 10, 27, 3))[0] == "open")
check("parse periods", cf.parse_periods("9-17") == [] if False else cf.parse_periods("9:00-12:00, 13:00-17:30")
      == [["09:00", "12:00"], ["13:00", "17:30"]] and cf.parse_periods("24h") == [["00:00", "24:00"]])
try:
    cf.parse_periods("nine to five"); check("bad range refused", False)
except ValueError:
    check("bad range refused", True)
rules = [{"id": 1, "did": "", "match": "prefix", "pattern": "559", "enabled": 1, "dest": "{}"},
         {"id": 2, "did": "5550001111", "match": "exact", "pattern": "5595551234", "enabled": 1, "dest": "{}"},
         {"id": 3, "did": "", "match": "anonymous", "pattern": "", "enabled": 1, "dest": "{}"},
         {"id": 4, "did": "", "match": "exact", "pattern": "3105550100", "enabled": 0, "dest": "{}"}]
check("number-specific rule wins", cf.match_rule(rules, "15550001111", "+15595551234")["id"] == 2)
check("area code rule on another number", cf.match_rule(rules, "5550002222", "5595551234")["id"] == 1)
check("anonymous", cf.match_rule(rules, "5550002222", "anonymous")["id"] == 3 and cf.match_rule(rules, "x", "")["id"] == 3)
check("disabled rule ignored, no match", cf.match_rule(rules, "5550002222", "3105550100") is None)

# ------------------------------------------------------------ 2. pbx-brain
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
import app as A          # creates tables / migrations on the test DB
A.init_db()
db = sqlite3.connect(os.environ["PBX_DB"]); db.row_factory = sqlite3.Row
db.execute("INSERT INTO ring_groups (name, members, ring_seconds) VALUES ('Front desk', '[\"8801\"]', 20)")
gid = db.execute("SELECT id FROM ring_groups WHERE name='Front desk'").fetchone()[0]
db.execute("INSERT INTO time_conditions (name, hours, open_dest, closed_dest) VALUES ('Office', ?, ?, ?)",
           (json.dumps({d: [["00:00", "24:00"]] for d in cf.DAYS}), json.dumps({"type": "rgroup", "target": gid}),
            json.dumps({"type": "vm", "target": "8800"})))
tcid = db.execute("SELECT id FROM time_conditions").fetchone()[0]
db.execute("INSERT INTO inbound_routes (did, ring_extens, tc_id) VALUES ('5550001111', '[\"8800\"]', ?)", (tcid,))
db.execute("INSERT INTO inbound_routes (did, ring_extens) VALUES ('5550002222', '[\"8800\"]')")
for k, v in (("default_quota_call_minutes", "-1"), ("default_quota_voicemail", "1")):
    db.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (k, v))
db.commit()

sys.path.insert(0, os.path.join(here, "..", "pbx-brain"))
import importlib
B = importlib.import_module("app") if False else None
import importlib.util
spec = importlib.util.spec_from_file_location("brain", os.path.join(here, "..", "pbx-brain", "app.py"))
B = importlib.util.module_from_spec(spec); spec.loader.exec_module(B)

LOG = []; CHANS = {}
class FakeChan:
    ids = itertools.count(1)
    def __init__(self, ep=None, id=None, caller="5595551234"):
        self.id = id or f"b{next(self.ids)}"; self.ep = ep
        self.json = {"caller": {"number": caller}}
    def hangup(self, **k): LOG.append(("hangup", self.id))
    def ring(self): pass
    def answer(self): pass
    def play(self, media): LOG.append(("play", self.id, media)); return {"id": "pb-" + self.id}
class FakeChannels:
    def originate(self, endpoint, **kw):
        c = FakeChan(endpoint); CHANS[c.id] = c; LOG.append(("originate", endpoint, c.id)); return c
    def get(self, cid): return CHANS.setdefault(cid, FakeChan(id=cid))
class FakeClient:
    channels = FakeChannels()
    def get(self, path): raise RuntimeError("404")
    def delete(self, path): pass
B.CLIENT = FakeClient()
class NoTimer:
    def __init__(self, t, f, args=()): self.f, self.args = f, args
    def start(self): pass
    def cancel(self): pass
    daemon = True
B.threading.Timer = NoTimer
VM = []
B.start_voicemail_real = B.start_voicemail
def fake_vm_entry(chan, mailbox, caller):
    if chan.id in B.NO_VM:
        LOG.append(("hangup", chan.id)); return
    VM.append((chan.id, mailbox))
B.start_voicemail = fake_vm_entry
B.deny_and_hangup = lambda chan, prompt=None, why="": LOG.append(("denied", chan.id, prompt))

def call(did, caller, cid):
    LOG.clear(); VM.clear(); B.CALLS.clear(); B.PENDING.clear(); B.NO_VM.clear()
    a = FakeChan(id=cid, caller=caller); CHANS[cid] = a
    B.handle_inbound(a, did)
    return a
def rung():
    return [x[1] for x in LOG if x[0] == "originate"]
u8800 = B.q1("SELECT * FROM logins WHERE exten='8800'"); u8801 = B.q1("SELECT * FROM logins WHERE exten='8801'")

call("5550001111", "5595551234", "c1")
check("number -> time condition (open 24h) -> ring group rings 8801",
      rung() and all(u8801["sip_username"] in e for e in rung()), LOG)
db.execute("UPDATE time_conditions SET override='closed'"); db.commit()
call("5550001111", "5595551234", "c2")
check("switched to Closed -> 8800's voicemail", VM and VM[0][0] == "c2" and not rung(), (VM, LOG))
db.execute("UPDATE time_conditions SET override='auto'"); db.commit()

db.execute("INSERT INTO caller_rules (did, match, pattern, dest) VALUES ('', 'exact', '5595551234', ?)",
           (json.dumps({"type": "disconnected"}),)); db.commit()
call("5550002222", "+1 559-555-1234", "c3")
check("caller rule: not in service", any(x[0] == "denied" for x in LOG) and not rung(), LOG)
check("rule use counted", db.execute("SELECT hits FROM caller_rules").fetchone()[0] == 1)
db.execute("UPDATE caller_rules SET dest=?", (json.dumps({"type": "route_novm"}),)); db.commit()
a = call("5550002222", "5595551234", "c4")
check("route_novm: rings the number's normal route", any(u8800["sip_username"] in e for e in rung()), LOG)
check("…and is marked no-voicemail", "c4" in B.NO_VM)
# nobody answers: every leg ends -> would be voicemail, but hangs up instead
for b in list(B.CALLS["c4"]["b_ids"]):
    B.hangup_other_leg(b)
check("route_novm: no answer -> hang up, no voicemail", not VM and ("hangup", "c4") in LOG, (VM, LOG))
db.execute("UPDATE caller_rules SET dest=?", (json.dumps({"type": "rgroup", "target": gid}),)); db.commit()
call("5550002222", "5595551234", "c5")
check("caller rule -> ring group instead of the normal route",
      rung() and all(u8801["sip_username"] in e for e in rung()), LOG)
db.execute("DELETE FROM caller_rules"); db.commit()

# per-user: ring but no voicemail, and VIP through DND
db.execute("INSERT INTO blocked_numbers (login_id, number, action) VALUES (?, '5595551234', 'ring_novm')", (u8800["id"],))
db.commit()
call("5550002222", "5595551234", "c6")
check("user ring_novm: still rings", any(u8800["sip_username"] in e for e in rung()), LOG)
check("user ring_novm: no voicemail for this caller", "c6" in B.NO_VM)
check("user ring_novm isn't a text block", not B.blocked_by(u8800["id"], "5595551234"))
db.execute("UPDATE blocked_numbers SET action='vip'")
db.execute("INSERT INTO user_prefs (login_id, dnd) VALUES (?, 1) ON CONFLICT(login_id) DO UPDATE SET dnd=1", (u8800["id"],))
db.commit()
call("5550002222", "5595551234", "c7")
check("user VIP rings through Do Not Disturb", any(u8800["sip_username"] in e for e in rung()), LOG)
call("5550002222", "3105550100", "c8")
check("…other callers still get DND (voicemail)", not rung() and VM, (LOG, VM))
db.execute("UPDATE user_prefs SET dnd=0 WHERE login_id=?", (u8800["id"],)); db.execute("DELETE FROM blocked_numbers"); db.commit()

# IVR: no key vs wrong key
db.execute("INSERT INTO ivr_menus (name, timeout_sec, max_retries, options, fallback, invalid_retries, invalid_fallback)"
           " VALUES ('Main', 5, 2, ?, ?, 0, ?)", (json.dumps({"1": {"type": "ext", "target": "8801"}}),
                                                  json.dumps({"type": "vm", "target": "8800"}), json.dumps({"type": "hangup"})))
db.commit()
ivr = B.q1("SELECT * FROM ivr_menus WHERE name='Main'")
def ivr_call(cid):
    LOG.clear(); VM.clear(); B.CALLS.clear()
    a = FakeChan(id=cid); CHANS[cid] = a
    B.start_ivr(a, ivr, {"caller": "5595551234", "caller_id": "x", "did": "", "me": None})
    return B.IVR_SESSIONS[cid]
s = ivr_call("i1")
B._ivr_retry(s, "timeout"); B._ivr_retry(s, "timeout")
check("no key: replays twice", "i1" in B.IVR_SESSIONS and not VM)
B._ivr_retry(s, "timeout")
check("no key: then its destination (voicemail)", VM and VM[0][0] == "i1", (VM, LOG))
s = ivr_call("i2")
B._ivr_retry(s, "invalid")
check("wrong key with 0 replays: straight to its own destination (hang up)", ("hangup", "i2") in LOG and not VM, (LOG, VM))

# ------------------------------------------------------------ 3. admin pages
from fastapi.testclient import TestClient
import bcrypt
adm = db.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
db.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(b"pw-admin-1", bcrypt.gensalt()).decode(), adm))
db.commit()
A._login_failures.clear()
ac = TestClient(A.app, client=("192.168.1.50", 5555), follow_redirects=False)
ac.post("/login", data={"username": adm, "password": "pw-admin-1"})
csrf = lambda url: re.search(r'name="csrf_token" value="([^"]+)"', ac.get(url).text).group(1)
t = ac.get("/callflow").text
check("Call flow page lists the time condition, open now", "Office" in t and "Open" in t)
tok = csrf("/callflow/tc/new/edit")
form = {"csrf_token": tok, "name": "Support hours", "tz": "America/Los_Angeles", "h_mon": "9:00-12:00, 13:00-17:00",
        "h_sat": "10:00-14:00", "holidays": "12-25 Christmas\n2026-11-26 Thanksgiving", "open_type": "rgroup",
        "open_rgroup": str(gid), "closed_type": "vm", "closed_vm": "8800", "holiday_type": "", "override": "auto",
        "enabled": "1"}
r = ac.post("/callflow/tc/new/edit", data=form)
row = db.execute("SELECT * FROM time_conditions WHERE name='Support hours'").fetchone()
check("time condition saved", r.status_code == 303 and row and json.loads(row["hours"])["mon"] == [["09:00", "12:00"], ["13:00", "17:00"]]
      and len(json.loads(row["holidays"])) == 2 and json.loads(row["open_dest"]) == {"type": "rgroup", "target": gid},
      (r.status_code, r.text[-400:] if r.status_code != 303 else ""))
r = ac.post("/callflow/tc/new/edit", data=dict(form, name="Bad", h_tue="nine to five"))
check("bad hours refused, typed text kept", r.status_code == 400 and "nine to five" in r.text and "Tuesday" in r.text)
r = ac.post(f"/callflow/tc/{row['id']}/override", data={"csrf_token": tok, "mode": "closed"})
check("switch to Closed", db.execute("SELECT override FROM time_conditions WHERE id=?", (row["id"],)).fetchone()[0] == "closed")
tok = csrf("/callflow/rule/new/edit")
r = ac.post("/callflow/rule/new/edit", data={"csrf_token": tok, "did": "", "match": "exact", "pattern": "(555) 555-5555",
                                              "dest_type": "route_novm", "note": "test", "sort": "0", "enabled": "1"})
rr = db.execute("SELECT * FROM caller_rules").fetchone()
check("caller rule saved (number normalised)", rr and rr["pattern"] == "5555555555" and json.loads(rr["dest"]) == {"type": "route_novm"},
      (r.status_code, r.text[-300:] if r.status_code != 303 else ""))
t = ac.get("/routes/inbound/new/edit").text
check("Routes form offers time conditions", f'value="t{tcid}"' in t and "Time condition: Office" in t)
check("Routes list shows the time condition", "Time condition: Office" in ac.get("/routes").text)
tok = csrf("/callflow")
r = ac.post(f"/callflow/tc/{tcid}/delete", data={"csrf_token": tok})
check("can't delete a time condition a number uses", "still used by" in r.headers.get("location", "").replace("+", " ")
      and db.execute("SELECT 1 FROM time_conditions WHERE id=?", (tcid,)).fetchone())
r = ac.post(f"/ring-groups/{gid}/delete", data={"csrf_token": tok})
check("can't delete a ring group a time condition uses", db.execute("SELECT 1 FROM ring_groups WHERE id=?", (gid,)).fetchone() is not None)
t = ac.get(f"/ivr/{ivr['id']}/edit").text if ac.get(f"/ivr/{ivr['id']}/edit").status_code == 200 else ac.get("/ivr").text
check("IVR editor has wrong-key settings and new destinations",
      'name="invalid_retries"' in t and "Time condition" in t and "Ring group" in t, t[:200])
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
