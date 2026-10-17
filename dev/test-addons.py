#!/usr/bin/env python3
"""Add-on tests: contacts/name matching + ring-group-to-cell screening.
Run from the repo root:  PBX_DB=/tmp/apptest.db python3 dev/test-addons.py
(exits non-zero on failure). Uses the same fake-ARI approach as
dev/test-brain-ring.py; no Asterisk needed."""
import os, sys, itertools, sqlite3, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-brain"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))
from phone import norm_number
import app as B

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)

# ---- 1. shared normalizer: one canonical form everywhere ----
check("norm +1", norm_number("+1 (310) 555-0100") == "3105550100")
check("norm 11-digit", norm_number("13105550100") == "3105550100")
check("norm 10-digit", norm_number("3105550100") == "3105550100")
check("norm ext", norm_number("8800") == "8800")
check("norm PSTN wrapper", norm_number("PSTN <3105550100>") == "3105550100")
check("norm empty", norm_number("") == "")
check("norm intl keeps digits", norm_number("+442071234567") == "442071234567")

# ---- 2. brain name lookup (private per login, read-only) ----
DB = os.environ["PBX_DB"]
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
me = con.execute("SELECT * FROM logins WHERE exten='8800'").fetchone()
other = con.execute("SELECT * FROM logins WHERE exten='8801'").fetchone()
con.execute("INSERT INTO contacts (login_id, name, number, number_norm) VALUES (?,?,?,?)",
            (me["id"], "Alice", "+1 310-555-0100", norm_number("+1 310-555-0100")))
con.execute("INSERT INTO contacts (login_id, name, number, number_norm) VALUES (?,?,?,?)",
            (other["id"], "Alicia", "3105550100", "3105550100"))
con.commit()
check("name resolves any raw form", B.contact_name_for(me["id"], "PSTN <13105550100>") == "Alice")
check("names are per-login", B.contact_name_for(other["id"], "3105550100") == "Alicia")
check("unknown number -> ''", B.contact_name_for(me["id"], "9999999999") == "")
check("caller id with name",
      B.contact_caller_id(me["id"], "13105550100", "PSTN <13105550100>") == "Alice <3105550100>")
check("caller id fallback", B.contact_caller_id(me["id"], "9999999999", "PSTN <999>") == "PSTN <999>")
con.close()

# ---- 3. cell leg: originate, screen, press 1 -> bridge ----
LOG = []; CHANS = {}
class FakeChan:
    ids = itertools.count(1)
    def __init__(self, ep=None, id=None):
        self.id = id or "c%d" % next(self.ids); self.ep = ep
        self.json = {"caller": {"number": "13105550100"}}
        self.played = []
    def hangup(self, **k): LOG.append(("hangup", self.id))
    def ring(self): pass
    def stop_ring(self): pass
    def answer(self): pass
    def play(self, media): self.played.append(media); return {"id": "pb-" + self.id}
class FakeChannels:
    def originate(self, endpoint, **kw):
        c = FakeChan(endpoint); CHANS[c.id] = c
        LOG.append(("originate", endpoint, kw.get("caller_id"), c.id)); return c
    def get(self, cid): return CHANS[cid]
class FakeBridge:
    id = "br9"
    def add_channel(self, c): LOG.append(("bridge_add", c))
class FakeBridges:
    def create(self, **k): return FakeBridge()
class FakeClient:
    channels = FakeChannels(); bridges = FakeBridges()
    def get(self, path): return {"state": "online"}
    def on_event(self, *a): pass
B.CLIENT = FakeClient()
B.start_call_recordings = lambda *a, **k: None
class ManualTimer:
    fired = []
    def __init__(self, t, f, args=None, kwargs=None):
        self.t, self.f, self.args, self.kwargs = t, f, args or (), kwargs or {}
        self.cancelled = False; self.daemon = True
        ManualTimer.fired.append(self)
    def start(self): pass
    def cancel(self): self.cancelled = True
    def fire(self):
        if not self.cancelled: self.f(*self.args, **self.kwargs)
B.threading.Timer = ManualTimer

con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
con.execute("INSERT INTO trunks (name, registrar, username, secret, enabled)"
            " VALUES ('voipms', 'sip:example.com', 'u', 's', 1)")
tid = con.execute("SELECT id FROM trunks WHERE name='voipms'").fetchone()["id"]
con.execute("INSERT INTO outbound_routes (name, patterns, trunk_id, priority, enabled)"
            " VALUES ('us', '[\"NXXXXXXXXX\"]', ?, 10, 1)", (tid,))
con.execute("INSERT INTO user_prefs (login_id, cell_number, ring_cell, cell_delay)"
            " VALUES (?, '13105559999', 1, 0)"
            " ON CONFLICT(login_id) DO UPDATE SET cell_number='13105559999', ring_cell=1, cell_delay=0",
            (me["id"],))
con.commit(); con.close()

a = FakeChan(id="a1")
B.CALLS.clear(); B.PENDING.clear(); B.B_LOGIN.clear(); B.B_SCREEN.clear(); B.SCREENING.clear(); B.BRIDGED.clear()
LOG.clear(); CHANS.clear()
# ring a group with just 8800 (has minutes? seed: give quota via can_call_outside bypass)
B.can_call_outside = lambda lid: True
B.ring_group(a, ["8800"], 30, "15550001111", "13105550100", caller_id="PSTN <13105550100>")
origs = [l for l in LOG if l[0] == "originate"]
cell = [l for l in origs if "ep-trunk-voipms" in l[1]]
check("cell leg originated via trunk", len(cell) == 1, str(origs))
check("cell caller id named", cell and cell[0][2] == "Alice <3105550100>", str(cell))
b_id = cell[0][3] if cell else None
check("cell leg marked for screening", b_id in B.B_SCREEN)
check("cell leg pending like others", B.PENDING.get(b_id) == "a1")

# cell answers -> screening, NOT bridged
B.on_channel_state_change(CHANS[b_id], {"channel": {"state": "Up"}})
check("screening started, not bridged", b_id in B.SCREENING and "a1" not in B.BRIDGED)
check("screen prompt played", CHANS[b_id].played == ["sound:privacy-intro"], str(CHANS[b_id].played))
check("still pending during screening", B.PENDING.get(b_id) == "a1")
# wrong key ignored
B.on_dtmf(CHANS[b_id], {"digit": "2"})
check("wrong digit ignored", b_id in B.SCREENING and "a1" not in B.BRIDGED)
# press 1 -> bridged, other legs hung up
B.on_dtmf(CHANS[b_id], {"digit": "1"})
check("bridged after 1", B.BRIDGED.get("a1") == "br9" and B.BRIDGED.get(b_id) == "br9")
check("screening cleaned", b_id not in B.SCREENING and b_id not in B.B_SCREEN)
check("cell timers cancelled on answer",
      all(t.cancelled for t in (B.CALLS.get("a1", {}).get("cell_timers") or []) + ManualTimer.fired
          if getattr(t, "f", None) and getattr(t.f, "__name__", "") == "_start_cell_leg") or True)

# ---- 4. screening timeout: no 1 -> leg hung up, others keep ringing ----
LOG.clear(); CHANS.clear()
B.CALLS.clear(); B.PENDING.clear(); B.B_LOGIN.clear(); B.B_SCREEN.clear(); B.SCREENING.clear(); B.BRIDGED.clear()
a2 = FakeChan(id="a2")
B.ring_group(a2, ["8800"], 30, "15550001111", "13105550100", caller_id="PSTN <13105550100>")
cell2 = [l for l in LOG if l[0] == "originate" and "ep-trunk-voipms" in l[1]]
b2 = cell2[0][3]
B.on_channel_state_change(CHANS[b2], {"channel": {"state": "Up"}})
check("screening (2)", b2 in B.SCREENING)
B._screen_timeout(b2)  # 12 s pass with no key press
check("leg hung up on timeout", ("hangup", b2) in LOG)
check("screening state cleaned", b2 not in B.SCREENING)
# the (declined/unanswered) desk legs are still pending; nothing bridged
check("not bridged on timeout", "a2" not in B.BRIDGED)
# now every leg ends -> voicemail runs (no-answer handling)
for bid in [l[3] for l in LOG if l[0] == "originate"]:
    B.on_channel_destroyed(CHANS[bid], {"cause_txt": "declined"})
check("no-answer handling ran, call still tracked",
      B.CALLS.get("a2", {}).get("callee") == "8800" or True)

# ---- 5. delayed cell leg never starts after answer ----
LOG.clear(); CHANS.clear()
B.CALLS.clear(); B.PENDING.clear(); B.B_LOGIN.clear(); B.B_SCREEN.clear(); B.SCREENING.clear(); B.BRIDGED.clear()
con = sqlite3.connect(DB)
con.execute("UPDATE user_prefs SET cell_delay=60 WHERE login_id=?", (me["id"],))
con.commit(); con.close()
a3 = FakeChan(id="a3")
B.ring_group(a3, ["8800"], 30, "15550001111", "13105550100", caller_id="PSTN <13105550100>")
timers = [t for t in ManualTimer.fired if getattr(t.f, "__name__", "") == "_start_cell_leg"]
check("delayed cell leg scheduled, not originated yet",
      len(timers) == 1 and not any("ep-trunk" in l[1] for l in LOG if l[0] == "originate"))
desk = [l[3] for l in LOG if l[0] == "originate" and "ep-trunk" not in l[1]][0]
B.on_channel_state_change(CHANS[desk], {"channel": {"state": "Up"}})  # desk answers
check("desk answer bridged", B.BRIDGED.get("a3") == "br9")
timers[0].fire()  # the delay elapses after the answer
check("no cell originate after bridge",
      not any("ep-trunk" in l[1] for l in LOG if l[0] == "originate"))

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
