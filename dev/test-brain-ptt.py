#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""pbx-brain push-to-talk (*88<exten>) with a fake ARI: the gate is the
recording-style split — placing needs the caller's effective permission,
receiving needs the callee's effective permission AND ptt_enabled. Gate
failures fast-fail (denied announcement + hangup). A good PTT call
originates B legs carrying ONLY the X-PTT header (nothing auto-answers:
no Call-Info answer-after), bridges on accept with a beep, is never
recorded, and lands in the CDR with direction 'ptt'. If the callee
doesn't accept, THEIR ptt_missed setting decides: 'voicemail' (default)
routes the caller to voicemail; 'retry' re-rings up to 3 total attempts,
then voicemail.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-brain-ptt.py      # run from the repo root; exits non-zero on failure
"""
import os, sys, sqlite3, itertools
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-brain"))
import app as B

fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, x); ok or fails.append(n)

LOG = []; CHANS = {}; STATES = {}; FAIL = set(); VM = []
BRIDGES = []

class FakeChan:
    ids = itertools.count(100)
    def __init__(self, ep=None, id=None, caller="8800"):
        self.id = id or "b%d" % next(self.ids); self.ep = ep
        self.json = {"caller": {"number": caller}}
        self.played = []; self.hung = False
    def hangup(self, **k): LOG.append(("hangup", self.id)); self.hung = True
    def ring(self): LOG.append(("ring", self.id))
    def stop_ring(self): pass
    def answer(self): pass
    def play(self, media):
        # No playback id -> play_then() runs its callback at once, so a
        # deny_and_hangup() completes synchronously in the test.
        self.played.append(media); LOG.append(("play", self.id, media)); return {}

class FakeChannels:
    def originate(self, endpoint, **kw):
        LOG.append(("originate", endpoint, kw))
        if endpoint in FAIL: raise RuntimeError("no contacts")
        c = FakeChan(endpoint); CHANS[c.id] = c; return c
    def get(self, cid): return CHANS[cid]

class FakeBridge:
    ids = itertools.count(1)
    def __init__(self):
        self.id = "br%d" % next(self.ids); self.added = []; self.played = []
        self.recorded = []
    def add_channel(self, c): self.added.append(c); LOG.append(("bridge_add", c))
    def play(self, media): self.played.append(media); LOG.append(("bridge_play", media))
    def record(self, **k): self.recorded.append(k)

class FakeBridges:
    def create(self, **k):
        b = FakeBridge(); BRIDGES.append(b); return b
    def get(self, bid): return BRIDGES[-1]

class FakeClient:
    channels = FakeChannels(); bridges = FakeBridges()
    def get(self, path):
        name = path.split("/")[-1]
        if name not in STATES: raise RuntimeError("404")
        return {"state": STATES[name]}

B.CLIENT = FakeClient()
B.route_to_voicemail = lambda *a, **k: VM.append(a)
# Recording fully armed for both parties (flags + consent + plan feature),
# so a normal call WOULD record; PTT must still never record.
B.has_feature = lambda lid, f: True

d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
ids = {r["exten"]: r["id"] for r in d.execute("SELECT id, exten FROM logins WHERE exten IN ('8800','8801')")}
sip = {r["exten"]: r["sip_username"] for r in d.execute("SELECT exten, sip_username FROM logins WHERE exten IN ('8800','8801')")}
d.execute("UPDATE logins SET user_record=1, user_wants_record=1")
for e in ("8800", "8801"):
    d.execute("INSERT OR IGNORE INTO user_prefs (login_id) VALUES (?)", (ids[e],))
    d.execute("UPDATE user_prefs SET recording_consent_version=? WHERE login_id=?",
              (B.RECORDING_CONSENT_VERSION, ids[e]))
d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('rec_announce', '0')")
d.commit()

def set_ptt(exten, allowed, enabled):
    d.execute("UPDATE logins SET ptt_allowed=?, ptt_enabled=? WHERE exten=?",
              (allowed, enabled, exten)); d.commit()

def set_missed(exten, mode):
    d.execute("UPDATE logins SET ptt_missed=? WHERE exten=?", (mode, exten)); d.commit()

def reset():
    B.CALLS.clear(); B.PENDING.clear(); B.B_LOGIN.clear(); B.B_SCREEN.clear()
    B.REC_RECORDINGS.clear(); LOG.clear(); CHANS.clear(); VM.clear()
    BRIDGES.clear(); FAIL.clear(); STATES.clear()

def dial_ptt(a_id, caller="8800", target="*888801"):
    a = FakeChan(id=a_id, caller=caller); CHANS[a_id] = a
    B.handle_internal(a, target)
    return a

def originates():
    return [(x[1], x[2]) for x in LOG if x[0] == "originate"]

def denied(a):
    return a.hung and B.DENIED_PROMPT in a.played

# Sanity: with these flags a normal bridged call DOES record, so the
# "PTT never records" checks below are not vacuous.
reset()
a0 = FakeChan(id="a0", caller="8801"); CHANS["a0"] = a0
B.CALLS["a0"] = {"caller": "8801", "callee": "8800", "direction": "internal",
                 "login_id": ids["8801"], "callee_id": ids["8800"]}
B.start_call_recordings(FakeBridge(), "a0", ids["8800"])
check("sanity: normal call records when armed", len(B.REC_RECORDINGS) > 0, B.REC_RECORDINGS.keys())

# (a) callee ptt_allowed=0 -> fast-fail, no B originate, no voicemail
reset(); set_ptt("8800", 1, 1); set_ptt("8801", 0, 1)
a = dial_ptt("a1")
check("a: callee not allowed -> no originate", originates() == [], originates())
check("a: callee not allowed -> denied + hung up", denied(a), (a.played, a.hung))
check("a: voicemail never invoked", VM == [], VM)

# (b) callee allowed=1 but enabled=0 -> same fast-fail
reset(); set_ptt("8800", 1, 1); set_ptt("8801", 1, 0)
a = dial_ptt("a2")
check("b: callee not enabled -> no originate", originates() == [], originates())
check("b: callee not enabled -> denied + hung up", denied(a), (a.played, a.hung))
check("b: voicemail never invoked", VM == [], VM)

# (d) caller ptt_allowed=0 -> rejected even when the callee is fully on
reset(); set_ptt("8800", 0, 1); set_ptt("8801", 1, 1)
a = dial_ptt("a3")
check("d: caller not allowed -> no originate", originates() == [], originates())
check("d: caller not allowed -> denied + hung up", denied(a), (a.played, a.hung))
check("d: voicemail never invoked", VM == [], VM)

# (e) callee fully on but every leg fails to originate (offline) -> fast-fail
reset(); set_ptt("8800", 1, 1); set_ptt("8801", 1, 1)
STATES[sip["8801"] + "-app"] = "offline"
FAIL.add("PJSIP/%s" % sip["8801"])
a = dial_ptt("a4")
check("e: originate failed -> denied + hung up", denied(a), (a.played, a.hung))
check("e: voicemail never invoked", VM == [], VM)
check("e: no pending legs left", B.PENDING == {}, B.PENDING)

# (f) B leg ends unanswered, callee's missed setting = voicemail (the
#     default) -> caller goes to the callee's voicemail, NOT denied
reset(); set_ptt("8800", 1, 1); set_ptt("8801", 1, 1); set_missed("8801", "voicemail")
STATES[sip["8801"] + "-app"] = "offline"
a = dial_ptt("a5")
orig = originates()
check("f: one B leg originated", len(orig) == 1, orig)
b_id = B.CALLS["a5"]["b_id"]
B.on_channel_destroyed(CHANS[b_id], {"cause_txt": "unreachable"})
check("f: B died -> caller routed to voicemail", VM == [("a5", "8801")], VM)
check("f: caller not denied", not denied(a), (a.played, a.hung))

# (g) callee's missed setting = retry -> the phone rings again (chirp
#     again) up to 3 total attempts, then voicemail
reset(); set_ptt("8800", 1, 1); set_ptt("8801", 1, 1); set_missed("8801", "retry")
STATES[sip["8801"] + "-app"] = "offline"
a = dial_ptt("a7")
check("g: attempt 1 originated", len(originates()) == 1, originates())
B.on_channel_destroyed(CHANS[B.CALLS["a7"]["b_id"]], {"cause_txt": "no answer"})
check("g: no accept -> attempt 2 rings", len(originates()) == 2 and not a.hung, originates())
B.on_channel_destroyed(CHANS[B.CALLS["a7"]["b_id"]], {"cause_txt": "no answer"})
check("g: no accept -> attempt 3 rings", len(originates()) == 3 and not a.hung, originates())
B.on_channel_destroyed(CHANS[B.CALLS["a7"]["b_id"]], {"cause_txt": "no answer"})
check("g: attempts exhausted -> voicemail", VM == [("a7", "8801")], VM)
check("g: no 4th attempt", len(originates()) == 3, originates())
set_missed("8801", "voicemail")

# (c) both on -> B legs carry the PTT headers, bridge + beep on answer,
#     recording never starts, CDR is marked direction='ptt'
reset(); set_ptt("8800", 1, 0); set_ptt("8801", 1, 1)
STATES[sip["8801"] + "-app"] = "online"
a = dial_ptt("a6")
orig = originates()
check("c: B legs originated (main + app)", [o[0] for o in orig] ==
      ["PJSIP/%s" % sip["8801"], "PJSIP/%s-app" % sip["8801"]], orig)
check("c: X-PTT header on every leg, and NO auto-answer header", all(
    (o[1].get("variables") or {}).get("PJSIP_HEADER(add,X-PTT)") == "1" and
    "PJSIP_HEADER(add,Call-Info)" not in (o[1].get("variables") or {})
    for o in orig), orig)
check("c: caller ID name is PTT-prefixed", all(
    (o[1].get("caller_id") or "").startswith("PTT ") for o in orig), orig)
check("c: caller not denied", not a.hung and VM == [], (a.hung, VM))
b_id = orig[0][1] and B.CALLS["a6"]["b_id"]
B.on_channel_state_change(CHANS[b_id], {"channel": {"state": "Up"}})
br = BRIDGES[-1] if BRIDGES else None
check("c: bridged A + B on answer", br is not None and "a6" in br.added and b_id in br.added,
      br.added if br else None)
check("c: beep played to both sides", br is not None and B.PTT_BEEP in br.played,
      br.played if br else None)
check("c: recording never started", B.REC_RECORDINGS == {} and (br is None or br.recorded == []),
      (B.REC_RECORDINGS, br.recorded if br else None))
check("c: losing leg hung up", any(x[0] == "hangup" and x[1] != b_id for x in LOG), LOG)
B.write_cdr(a, {})
row = d.execute("SELECT direction, disposition FROM cdr WHERE call_id='a6'").fetchone()
check("c: CDR marked as PTT", row is not None and row["direction"] == "ptt"
      and row["disposition"] == "ANSWERED", dict(row) if row else None)

print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
