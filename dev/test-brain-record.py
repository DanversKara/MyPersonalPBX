#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""pbx-brain call recording with a fake bridge: calls are recorded both ways
(outbound, inbound from outside, extension to extension, ring groups) and
each user's copy is filed under that user.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-brain-record.py      # run from the repo root; exits non-zero on failure
"""
import os, sys, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-brain"))
import app as B
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, x); ok or fails.append(n)

B.REC_ADMIN_DIR = B.REC_USER_DIR = os.environ.get("TMPDIR", "/tmp") + "/rec-test"
class FakeBridge:
    id = "br1"
    def record(self, **k): pass
    def play(self, m): pass
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
ids = {r["exten"]: r["id"] for r in d.execute("SELECT id, exten FROM logins WHERE exten IN ('8800','8801')")}
def setup(user=(), admin=(), admin_on=True):
    d.execute("UPDATE logins SET record_admin=0, user_record=0, user_wants_record=0")
    for e in user:
        d.execute("UPDATE logins SET user_record=1, user_wants_record=1 WHERE exten=?", (e,))
        d.execute("INSERT OR IGNORE INTO user_prefs (login_id) VALUES (?)", (ids[e],))
        d.execute("UPDATE user_prefs SET recording_consent_version=? WHERE login_id=?", (B.RECORDING_CONSENT_VERSION, ids[e]))
    for e in admin:
        d.execute("UPDATE logins SET record_admin=1 WHERE exten=?", (e,))
    d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('admin_rec_enabled', ?)", ("1" if admin_on else "0",))
    d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('rec_announce', '0')")
    d.commit()
B.has_feature = lambda lid, f: True
def run(info, answered=None):
    B.REC_RECORDINGS.clear(); B.CALLS["a1"] = info
    B.start_call_recordings(FakeBridge(), "a1", answered)
    return sorted((r["system"], r["exten"], r["login_id"], r["direction"], r["peer"]) for r in B.REC_RECORDINGS.values())

out_pstn = {"caller": "8800", "callee": "15551234567", "direction": "outbound", "login_id": ids["8800"]}
in_pstn = {"caller": "15551234567", "callee": "8800", "direction": "inbound", "login_id": None, "callee_id": ids["8800"]}
in_group = {"caller": "15551234567", "callee": "8800,8801", "direction": "inbound", "login_id": None}
internal = {"caller": "8801", "callee": "8800", "direction": "internal", "login_id": ids["8801"], "callee_id": ids["8800"]}

setup(user=["8800"])
check("user: outbound to outside", run(out_pstn) == [("user", "8800", ids["8800"], "outbound", "15551234567")], run(out_pstn))
check("user: inbound from outside", run(in_pstn, ids["8800"]) == [("user", "8800", ids["8800"], "inbound", "15551234567")], run(in_pstn, ids["8800"]))
check("user: inbound via ring group, 8800 answers", run(in_group, ids["8800"]) == [("user", "8800", ids["8800"], "inbound", "15551234567")], run(in_group, ids["8800"]))
check("user: ring group, 8801 answers -> 8800 not recorded", run(in_group, ids["8801"]) == [])
check("user: call from another extension", run(internal, ids["8800"]) == [("user", "8800", ids["8800"], "internal", "8801")], run(internal, ids["8800"]))
setup(user=["8800", "8801"])
check("user: both record -> a copy each", run(internal, ids["8800"]) == sorted([
    ("user", "8800", ids["8800"], "internal", "8801"), ("user", "8801", ids["8801"], "internal", "8800")]), run(internal, ids["8800"]))
setup(admin=["8800"])
check("admin: inbound from outside", [r[0] for r in run(in_pstn, ids["8800"])] == ["admin"], run(in_pstn, ids["8800"]))
check("admin: inbound via ring group", [r[0] for r in run(in_group, ids["8800"])] == ["admin"], run(in_group, ids["8800"]))
check("admin: call from another extension", [r[0] for r in run(internal, ids["8800"])] == ["admin"])
setup(admin=["8800"], admin_on=False)
check("admin: system switch off -> nothing", run(in_pstn, ids["8800"]) == [])
setup()
check("nobody records -> nothing", run(in_pstn, ids["8800"]) == [] and run(out_pstn) == [])
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
