#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Guest texting scope through the AGI msg-route (softphone SIP MESSAGEs).

The internal route enforces the INTERNAL side of the guest's 4-way
texting scope (all | external | internal | off): a guest whose mode
excludes internal texting can neither send to nor receive from another
extension via a softphone. Storage in the messages table is the verdict.

    export PBX_DB=/tmp/guestagi.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-guest-agi.py     # run from the repo root
"""
import os, sys, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-brain"))
import agi_server as G

fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, str(x)[:160])
    if not ok:
        fails.append(n)

DB = os.environ["PBX_DB"]
d = sqlite3.connect(DB); d.row_factory = sqlite3.Row
# Same guest as the brain test: ext 8802, SIP username phone8802.
d.execute("INSERT OR REPLACE INTO logins (username, pwhash, role, exten, sip_username,"
          " sip_secret, display_name, is_guest)"
          " VALUES ('phone8802', 'x', 'user', '8802', 'phone8802', 'secret-8802', 'Guest Phone', 1)")
d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('default_quota_messages', '-1')")
d.execute("DELETE FROM messages")
d.commit()

def set_sms_mode(mode):
    ext = 1 if mode in ("all", "external") else 0
    d.execute("UPDATE logins SET guest_sms_mode=?, guest_sms=? WHERE exten='8802'",
              (mode, ext)); d.commit()

def stored():
    n = d.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    d.execute("DELETE FROM messages"); d.commit()
    return n

def route(frm, to, body="hi"):
    # agi_arg_2 carries the From URI; its user part is the SIP username.
    G.handle_msg_route({"agi_arg_1": to,
                        "agi_arg_2": "sip:%s@pbx.local" % frm,
                        "agi_arg_3": body})

for mode, out_ok, in_ok in (("off", False, False), ("external", False, False),
                            ("internal", True, True), ("all", True, True)):
    set_sms_mode(mode)
    route("phone8802", "8800")   # guest (by SIP username) -> normal ext
    check(f"agi sms({mode}): guest -> extension {'stored' if out_ok else 'dropped'}",
          (stored() == 1) is out_ok)
    route("phone8800", "8802")   # normal ext (seed sip user = phone8800) -> guest
    check(f"agi sms({mode}): extension -> guest {'stored' if in_ok else 'dropped'}",
          (stored() == 1) is in_ok)

set_sms_mode("off")
route("phone8800", "8801")
check("agi sms: normal -> normal unaffected", stored() == 1)

print()
print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
