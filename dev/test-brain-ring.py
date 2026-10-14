#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""pbx-brain ringing with a fake ARI: the My Phone app line (<user>-app) rings with the
main line only when online; first answer wins; voicemail only after every leg ends.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-brain-ring.py      # run from the repo root; exits non-zero on failure
"""
import os, sys, itertools
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-brain"))
import app as B

class FakeChan:
    ids = itertools.count(1)
    def __init__(self, ep=None, id=None):
        self.id = id or f"b{next(self.ids)}"; self.ep = ep; self.hung = False
        self.json = {"caller": {"number": "8801"}}
    def hangup(self, **k): LOG.append(("hangup", self.id)); self.hung = True
    def ring(self): LOG.append(("ring", self.id))
    def stop_ring(self): pass
    def answer(self): pass
LOG = []; CHANS = {}
class FakeChannels:
    def originate(self, endpoint, **kw):
        if endpoint in FAIL: raise RuntimeError("no contacts")
        c = FakeChan(endpoint); CHANS[c.id] = c; LOG.append(("originate", endpoint, c.id)); return c
    def get(self, cid): return CHANS.setdefault(cid, FakeChan(id=cid))
class FakeBridge:
    id = "br1"
    def add_channel(self, c): LOG.append(("bridge_add", c))
class FakeBridges:
    def create(self, **k): return FakeBridge()
class FakeClient:
    channels = FakeChannels(); bridges = FakeBridges()
    def get(self, path):
        name = path.split("/")[-1]
        if name not in STATES: raise RuntimeError("404")
        return {"state": STATES[name]}
STATES = {}; FAIL = set()
B.CLIENT = FakeClient()
B.start_call_recordings = lambda *a, **k: None
import threading
class NoTimer:
    def __init__(self, t, f): self.f = f
    def start(self): pass
    def cancel(self): pass
    daemon = True
B.threading.Timer = NoTimer

fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, x); ok or fails.append(n)
me = B.q1("SELECT * FROM logins WHERE exten='8801'"); callee = B.q1("SELECT * FROM logins WHERE exten='8800'")
u = callee["sip_username"]

check("app offline -> main only", B.phone_endpoints(callee) == [f"PJSIP/{u}"])
STATES[f"{u}-app"] = "offline"
check("app line offline -> main only", B.phone_endpoints(callee) == [f"PJSIP/{u}"])
STATES[f"{u}-app"] = "online"
check("app online -> both", B.phone_endpoints(callee) == [f"PJSIP/{u}", f"PJSIP/{u}-app"])

# ring_local rings both; answer on the app leg hangs up the desk leg and bridges
a = FakeChan(id="a1"); CHANS["a1"] = a; LOG.clear()
B.ring_local(a, me, callee)
orig = [x for x in LOG if x[0] == "originate"]
check("ring_local originated 2 legs", [o[1] for o in orig] == [f"PJSIP/{u}", f"PJSIP/{u}-app"], orig)
desk, app_leg = orig[0][2], orig[1][2]
check("both pending for A", B.PENDING.get(desk) == "a1" and B.PENDING.get(app_leg) == "a1")
B.on_legb_answer(CHANS[app_leg])
check("desk leg hung up when app answers", ("hangup", desk) in LOG)
check("bridged A + app", ("bridge_add", "a1") in LOG and ("bridge_add", app_leg) in LOG)
check("winner recorded", B.CALLS["a1"]["b_id"] == app_leg)

# desk phone declines while app still ringing -> no voicemail yet
B.PENDING.clear(); B.CALLS.clear(); LOG.clear()
a2 = FakeChan(id="a2"); CHANS["a2"] = a2
vm = []
B.route_to_voicemail = lambda *x, **k: vm.append(x)
B.ring_local(a2, me, callee)
o = [x[2] for x in LOG if x[0] == "originate"]
B.hangup_other_leg(o[0])          # desk declined
check("still waiting for app leg", vm == [] and o[1] in B.PENDING, vm)
B.hangup_other_leg(o[1])          # app declined too
check("voicemail after both end", len(vm) == 1 or not B.PENDING, vm)

# main line fails to originate but app is online -> app still rings
B.PENDING.clear(); B.CALLS.clear(); LOG.clear(); FAIL.add(f"PJSIP/{u}")
a3 = FakeChan(id="a3"); CHANS["a3"] = a3
B.ring_local(a3, me, callee)
check("app rings when main originate fails", [x[1] for x in LOG if x[0]=="originate"] == [f"PJSIP/{u}-app"] and not a3.hung)
FAIL.clear()

# ring_group rings app lines too
B.PENDING.clear(); B.CALLS.clear(); LOG.clear()
a4 = FakeChan(id="a4"); CHANS["a4"] = a4
B.ring_group(a4, ["8800"], 20, "", "8801", group={"name": "g", "members": "[]", "id": 1}, internal=True)
eps = [x[1] for x in LOG if x[0] == "originate"]
check("ring_group rings both lines", eps == [f"PJSIP/{u}", f"PJSIP/{u}-app"], eps)
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
