#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""pbx-brain guest extensions with a fake ARI.

A guest extension (logins.is_guest) is a restricted account type: internal
calling always works, but outside calling, PSTN inbound, texting and 911
are each off unless the admin turned that one rule on (guest_outbound /
guest_inbound / guest_sms / guest_e911). Normal extensions are never
restricted by these flags — 911 least of all.

    export PBX_DB=/tmp/guesttest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-brain-guest.py    # run from the repo root
"""
import os, sys, sqlite3, itertools
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-brain"))
import app as B

fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, x)
    if not ok:
        fails.append(n)

LOG = []; CHANS = {}; STATES = {}; VM = []
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
        self.played.append(media); LOG.append(("play", self.id, media)); return {}

class FakeChannels:
    def originate(self, endpoint, **kw):
        LOG.append(("originate", endpoint, kw))
        c = FakeChan(endpoint); CHANS[c.id] = c; return c
    def get(self, cid): return CHANS[cid]

class FakeBridge:
    ids = itertools.count(1)
    def __init__(self):
        self.id = "br%d" % next(self.ids); self.added = []; self.played = []
    def add_channel(self, c): self.added.append(c); LOG.append(("bridge_add", c))
    def play(self, media): self.played.append(media); LOG.append(("bridge_play", media))

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

EMERG = []
_orig_emerg = B.e911.handle_emergency
B.e911.handle_emergency = lambda channel, me, number, **kw: EMERG.append((channel.id, number))

d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
# A guest login on ext 8802 (all rules off unless a test turns one on).
d.execute("INSERT OR REPLACE INTO logins (username, pwhash, role, exten, sip_username,"
          " sip_secret, display_name, is_guest)"
          " VALUES ('phone8802', 'x', 'user', '8802', 'phone8802', 'secret-8802', 'Guest Phone', 1)")
d.execute("INSERT OR IGNORE INTO voicemail_boxes (mailbox, login_id)"
          " VALUES ('vm-8802', (SELECT id FROM logins WHERE username='phone8802'))")
# One trunk + one outbound route, and unlimited plan defaults so quota
# never masks the guest gates under test.
d.execute("INSERT OR REPLACE INTO trunks (id, name, registrar, username, secret, enabled)"
          " VALUES (1, 'test-trunk', 'sip:example.com', 'u', 'p', 1)")
d.execute("INSERT OR REPLACE INTO outbound_routes (id, name, patterns, trunk_id, priority, enabled)"
          " VALUES (1, 'test-out', '[\"1NXXNXXXXXX\"]', 1, 10, 1)")
for k in ("default_quota_call_minutes", "default_quota_messages"):
    d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?, '-1')", (k,))
d.commit()

def set_guest(exten, **flags):
    sets = ", ".join("%s=%d" % (k, 1 if v else 0) for k, v in flags.items())
    d.execute("UPDATE logins SET %s WHERE exten=?" % sets, (exten,)); d.commit()

def set_sms_mode(exten, mode):
    """Set the guest texting scope + the synced external flag, the way
    the admin panel writes them."""
    ext = 1 if mode in ("all", "external") else 0
    d.execute("UPDATE logins SET guest_sms_mode=?, guest_sms=? WHERE exten=?",
              (mode, ext, exten)); d.commit()

def reset():
    B.CALLS.clear(); B.PENDING.clear(); B.B_LOGIN.clear()
    LOG.clear(); CHANS.clear(); VM.clear(); BRIDGES.clear(); EMERG.clear()

def dial(a_id, caller, number):
    a = FakeChan(id=a_id, caller=caller); CHANS[a_id] = a
    B.handle_internal(a, number)
    return a

def originates():
    return [x[1] for x in LOG if x[0] == "originate"]

def denied(a):
    return a.hung and B.DENIED_PROMPT in a.played

OUTSIDE = "12125550199"   # fictional 555 number

# --- 911 -----------------------------------------------------------------
reset(); set_guest("8802", guest_e911=0)
a = dial("g1", "8802", "911")
check("911: guest with e911 off is denied", denied(a), (a.played, a.hung))
check("911: emergency handler NOT invoked for blocked guest", EMERG == [], EMERG)

reset(); set_guest("8802", guest_e911=1)
a = dial("g2", "8802", "911")
check("911: guest with e911 on reaches the emergency handler",
      len(EMERG) == 1 and EMERG[0][1] == "911" and not a.hung, EMERG)

reset()
a = dial("g3", "8800", "911")
check("911: normal extension is never blocked",
      len(EMERG) == 1 and not denied(a), EMERG)

# --- outbound -------------------------------------------------------------
reset(); set_guest("8802", guest_outbound=0)
a = dial("g4", "8802", OUTSIDE)
check("outbound: guest with outbound off is denied", denied(a), (a.played, a.hung))
check("outbound: no trunk leg originated", originates() == [], originates())

reset(); set_guest("8802", guest_outbound=1)
a = dial("g5", "8802", OUTSIDE)
check("outbound: guest with outbound on reaches the trunk",
      any("ep-trunk-test-trunk" in ep for ep in originates()), originates())

reset()
a = dial("g6", "8800", OUTSIDE)
check("outbound: normal extension unaffected",
      any("ep-trunk-test-trunk" in ep for ep in originates()), originates())

reset(); set_guest("8802", guest_outbound=0)
me = B.q1("SELECT * FROM logins WHERE exten='8802'")
a = FakeChan(id="g7", caller="8802"); CHANS["g7"] = a
check("outbound: handle_outbound itself refuses a blocked guest (DISA/forward path)",
      B.handle_outbound(a, me, OUTSIDE) is False and originates() == [], originates())

# --- internal calling always works ----------------------------------------
reset(); set_guest("8802", guest_outbound=0, guest_inbound=0, guest_sms=0, guest_e911=0)
a = dial("g8", "8802", "8800")
check("internal: guest can call a normal extension",
      any(ep == "PJSIP/phone8800" for ep in originates()), originates())

reset()
a = dial("g9", "8800", "8802")
check("internal: normal extension can call the guest",
      any(ep == "PJSIP/phone8802" for ep in originates()), originates())

# --- PSTN inbound -----------------------------------------------------------
reset(); set_guest("8802", guest_inbound=0)
ch = FakeChan(id="i1", caller="12125550100"); CHANS["i1"] = ch
B.ring_group(ch, ["8802", "8801"], 5, "5550192837", "12125550100")
info = B.CALLS.get("i1", {})
check("inbound: blocked guest is skipped in a DID ring list",
      info.get("callee") == "8801", info.get("callee"))
check("inbound: only the normal member is rung",
      originates() == ["PJSIP/phone8801"], originates())

reset(); set_guest("8802", guest_inbound=1)
ch = FakeChan(id="i2", caller="12125550100"); CHANS["i2"] = ch
B.ring_group(ch, ["8802", "8801"], 5, "5550192837", "12125550100")
info = B.CALLS.get("i2", {})
check("inbound: guest with inbound on rings with the group",
      info.get("callee") == "8802,8801", info.get("callee"))

reset(); set_guest("8802", guest_inbound=0)
ch = FakeChan(id="i3", caller="8800"); CHANS["i3"] = ch
B.ring_group(ch, ["8802"], 5, "", "8800", internal=True)
info = B.CALLS.get("i3", {})
check("inbound: internal group call still rings the blocked guest",
      info.get("callee") == "8802", info.get("callee"))

reset(); set_guest("8802", guest_inbound=0)
guest_row = B.q1("SELECT * FROM logins WHERE exten='8802'")
ch = FakeChan(id="i4", caller="12125550100"); CHANS["i4"] = ch
B.ring_local(ch, None, guest_row, caller_label="12125550100",
             caller_id="PSTN <12125550100>", direction="inbound")
check("inbound: direct ring_local to blocked guest goes to voicemail, never rings",
      len(VM) == 1 and originates() == [], (VM, originates()))

# --- texts ------------------------------------------------------------------
# The texting scope is 4-way: all | external | internal | off.
reset(); set_sms_mode("8802", "off")
ok, info = B.send_message_api("8800", "8802", "hello from guest")
check("sms(off): guest cannot text internally", ok is False, info)
ok, info = B.send_message_api("8802", "8800", "hello to guest")
check("sms(off): texts to the guest are refused", ok is False, info)
ok, info = B.send_message_api(OUTSIDE, "8802", "hello outside")
check("sms(off): guest cannot text outside numbers", ok is False, info)

reset(); set_sms_mode("8802", "all")
ok, info = B.send_message_api("8800", "8802", "hello from guest")
check("sms(all): guest can text internally", ok is True, info)
ok, info = B.send_message_api("8802", "8800", "hello to guest")
check("sms(all): texts to the guest are delivered/stored", ok is True, info)

reset(); set_sms_mode("8802", "internal")
ok, info = B.send_message_api("8800", "8802", "hello from guest")
check("sms(internal): guest can text internally", ok is True, info)
ok, info = B.send_message_api("8802", "8800", "hello to guest")
check("sms(internal): texts to the guest are delivered/stored", ok is True, info)
ok, info = B.send_message_api(OUTSIDE, "8802", "hello outside")
check("sms(internal): guest cannot text outside numbers",
      ok is False and "turned off" in str(info.get("error", "")), info)

reset(); set_sms_mode("8802", "external")
ok, info = B.send_message_api("8800", "8802", "hello from guest")
check("sms(external): guest cannot text internally", ok is False, info)
ok, info = B.send_message_api("8802", "8800", "hello to guest")
check("sms(external): internal texts to the guest are refused", ok is False, info)
ok, info = B.send_message_api(OUTSIDE, "8802", "hello outside")
check("sms(external): guest passes the guest gate for outside numbers (fails later on DID/config)",
      "turned off" not in str(info.get("error", "")), info)

# Legacy rows (mode column blank) fall back to the old on/off flag.
reset(); d.execute("UPDATE logins SET guest_sms_mode='' WHERE exten='8802'"); d.commit()
set_guest("8802", guest_sms=1)
ok, info = B.send_message_api("8800", "8802", "legacy on")
check("sms(legacy flag on, no mode): internal text allowed", ok is True, info)
set_guest("8802", guest_sms=0)
ok, info = B.send_message_api("8800", "8802", "legacy off")
check("sms(legacy flag off, no mode): internal text refused", ok is False, info)
set_sms_mode("8802", "off")

reset(); set_sms_mode("8802", "off")
ok, info = B.send_message_api("8801", "8800", "normal to normal")
check("sms: normal-to-normal texting unaffected", ok is True, info)

# --- push-to-talk -------------------------------------------------------------
# PTT permission is effective: the admin override flag (ptt_allowed) OR
# the plan's push_to_talk entitlement. Guests follow the same rule.
def set_ptt(exten, allowed=None, enabled=None, plan="keep"):
    if allowed is not None:
        d.execute("UPDATE logins SET ptt_allowed=? WHERE exten=?", (allowed, exten))
    if enabled is not None:
        d.execute("UPDATE logins SET ptt_enabled=? WHERE exten=?", (enabled, exten))
    if plan != "keep":
        lid = d.execute("SELECT id FROM logins WHERE exten=?", (exten,)).fetchone()[0]
        d.execute("DELETE FROM user_entitlements WHERE login_id=? AND feature='push_to_talk'", (lid,))
        if plan is not None:
            d.execute("INSERT INTO user_entitlements (login_id, feature, quota, source)"
                      " VALUES (?, 'push_to_talk', ?, 'plan:1')", (lid, plan))
    d.commit()

def ptt(a_id, caller, target):
    reset()
    return dial(a_id, caller, "*88" + target)

# Control callee: 8800 can receive (override + wish on).
set_ptt("8800", allowed=1, enabled=1, plan=None)
set_ptt("8802", allowed=0, enabled=1, plan=None)
a = ptt("p1", "8802", "8800")
check("ptt: guest with no override and no plan cannot place", denied(a), (a.played, a.hung))

set_ptt("8802", allowed=1)
a = ptt("p2", "8802", "8800")
check("ptt: guest with the admin override can place",
      not denied(a) and any(ep == "PJSIP/phone8800" for ep in originates()), originates())

set_ptt("8802", allowed=0, plan=1)
a = ptt("p3", "8802", "8800")
check("ptt: guest with a PTT plan (no override) can place",
      not denied(a) and any(ep == "PJSIP/phone8800" for ep in originates()), originates())

set_ptt("8802", plan=0)
a = ptt("p4", "8802", "8800")
check("ptt: plan entitlement of 0 does not grant", denied(a), (a.played, a.hung))
set_ptt("8802", plan=None)

# Receiving: caller 8800 has the override; the guest is the callee.
set_ptt("8800", allowed=1, enabled=1, plan=None)
set_ptt("8802", allowed=0, enabled=1, plan=None)
a = ptt("p5", "8800", "8802")
check("ptt: guest with no override/plan cannot receive", denied(a), (a.played, a.hung))

set_ptt("8802", plan=1)
a = ptt("p6", "8800", "8802")
check("ptt: guest with a PTT plan can receive",
      not denied(a) and any(ep == "PJSIP/phone8802" for ep in originates()), originates())

set_ptt("8802", plan=None, allowed=1)
a = ptt("p7", "8800", "8802")
check("ptt: guest with the override can receive",
      not denied(a) and any(ep == "PJSIP/phone8802" for ep in originates()), originates())

set_ptt("8802", enabled=0)
a = ptt("p8", "8800", "8802")
check("ptt: permission without the user's own wish still cannot receive",
      denied(a), (a.played, a.hung))
set_ptt("8802", allowed=0, enabled=0, plan=None)

# Baseline: a normal user with no plan and no override has no PTT either.
set_ptt("8801", allowed=0, enabled=1, plan=None)
set_ptt("8800", allowed=1, enabled=1, plan=None)
a = ptt("p9", "8801", "8800")
check("ptt: normal user with no override and no plan cannot place", denied(a), (a.played, a.hung))
set_ptt("8801", plan=1)
a = ptt("p10", "8801", "8800")
check("ptt: normal user with a PTT plan can place",
      not denied(a) and any(ep == "PJSIP/phone8800" for ep in originates()), originates())
set_ptt("8801", allowed=0, enabled=0, plan=None)

print()
print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
