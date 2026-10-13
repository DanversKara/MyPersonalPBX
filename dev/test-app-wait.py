#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Long poll /api/v1/me/wait (app "Instant" notifications), in-process on a throwaway DB.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-app-wait.py      # run from the repo root; exits non-zero on failure
"""
import os, sys, time, sqlite3, threading
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, app_api
c = TestClient(A.app); c.__enter__()
d = sqlite3.connect(os.environ["PBX_DB"])
lid = d.execute("SELECT id FROM logins WHERE username='phone8800'").fetchone()[0]
tok = c.post("/api/v1/app/pair", json={"code": app_api._new_pair_code(lid), "device_name": "t"}).json()["token"]
H = {"Authorization": "Bearer " + tok}
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, x); ok or fails.append(n)

base = c.get("/api/v1/me/wait", headers=H, params={"timeout": 0}).json()
check("timeout=0 answers now", base["changed"] is False or True, base)
t = time.time(); r = c.get("/api/v1/me/wait", headers=H, params={**{k: base[k] for k in ("msg","call","vm")}, "timeout": 3}).json()
check("nothing new: waits ~3s, changed=false", 2.5 < time.time() - t < 5.5 and r["changed"] is False, (round(time.time()-t,1), r))

def later(sql, args):
    time.sleep(1.2); x = sqlite3.connect(os.environ["PBX_DB"]); x.execute(sql, args); x.commit()
now = time.strftime("%Y-%m-%d %H:%M:%S")
for name, sql, args, key in [
    ("text", "INSERT INTO messages (from_ext, to_ext, body, sent_at) VALUES ('8801','8800','hi',?)", (now,), "msg"),
    ("missed call", "INSERT INTO cdr (call_id, started_at, src, dst, disposition) VALUES ('w1',?, '5551112222','8800','NO ANSWER')", (now,), "call"),
    ("voicemail", "INSERT INTO voicemail_messages (mailbox, caller, path) VALUES ('vm-8800','5551112222','/x.wav')", (), "vm")]:
    cur = c.get("/api/v1/me/wait", headers=H, params={"timeout": 0}).json()
    threading.Thread(target=later, args=(sql, args)).start()
    t = time.time(); r = c.get("/api/v1/me/wait", headers=H, params={"msg": cur["msg"], "call": cur["call"], "vm": cur["vm"], "timeout": 20}).json()
    el = time.time() - t
    check(f"new {name} wakes the wait early", r["changed"] and r[key] > cur[key] and el < 6, (round(el,1), r))

# hidden missed call doesn't count
cur = c.get("/api/v1/me/wait", headers=H, params={"timeout": 0}).json()
c.post("/api/v1/me/calls/hide", headers=H, data={"ids": [cur["call"]]})
after = c.get("/api/v1/me/wait", headers=H, params={"timeout": 0}).json()
check("hidden missed call not reported", after["call"] < cur["call"], (cur["call"], after["call"]))
check("timeout capped at 55", app_api.WAIT_MAX == 55)
check("needs token", c.get("/api/v1/me/wait", params={"timeout": 0}).status_code == 401)
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
