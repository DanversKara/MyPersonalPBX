#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Call hiding, missed-call badge and app voicemail audio, in-process on a throwaway DB.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-app-calls.py      # writes a test wav under /var/spool/pbx/voicemail
Exits non-zero on any failure.
"""
import os, sys, sqlite3, wave, struct, time
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DB = os.environ["PBX_DB"]
sys.path.insert(0, os.path.join(REPO, "pbx-api"))
from fastapi.testclient import TestClient
import app as A
import app_api

c = TestClient(A.app); c.__enter__()
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra)
    if not cond: fails.append(name)

d = sqlite3.connect(DB); d.row_factory = sqlite3.Row
lid = d.execute("SELECT id FROM logins WHERE username='phone8800'").fetchone()[0]
lid2 = d.execute("SELECT id FROM logins WHERE username='phone8801'").fetchone()[0]

# A call from before the phone pairs: must not count on the new phone's badge.
now = time.strftime("%Y-%m-%d %H:%M:%S")
d.execute("INSERT INTO cdr (call_id, started_at, src, dst, disposition) VALUES ('old1', ?, '5550001111', '8800', 'NO ANSWER')", (now,))
d.commit()

# pair (pairing code path, like the app)
code = app_api._new_pair_code(lid)
r = c.post("/api/v1/app/pair", json={"code": code, "device_name": "Pixel"})
check("pair ok", r.status_code == 200, r.text[:120])
H = {"Authorization": "Bearer " + r.json()["token"]}
r2 = c.post("/api/v1/app/pair", json={"code": code, "device_name": "x"})
check("pair code works once", r2.status_code == 401)

ov = c.get("/api/v1/me/overview", headers=H).json()
check("new phone: old missed call not in badge", ov["missed_new"] == 0 and ov["missed_24h"] == 0, ov)

# new calls after pairing
rows = [("m1", "5551112222", "8800", "NO ANSWER"), ("m2", "5553334444", "8800", "BUSY"),
        ("i1", "5555556666", "8800", "ANSWERED"), ("o1", "8800", "5557778888", "ANSWERED"),
        ("x1", "8801", "5550009999", "ANSWERED")]  # someone else's call
for cid, src, dst, disp in rows:
    d.execute("INSERT INTO cdr (call_id, started_at, src, dst, disposition) VALUES (?,?,?,?,?)", (cid, now, src, dst, disp))
d.commit()
ids = {r["call_id"]: r["id"] for r in d.execute("SELECT id, call_id FROM cdr")}

ov = c.get("/api/v1/me/overview", headers=H).json()
check("badge counts 2 new missed", ov["missed_new"] == 2 and ov["missed_24h"] == 2, ov)

allc = c.get("/api/v1/me/calls", headers=H).json()
check("all view lists 5 own calls", len(allc["calls"]) == 5, [x["id"] for x in allc["calls"]])
check("other user's call absent", ids["x1"] not in [x["id"] for x in allc["calls"]])

# hide one missed call + try to hide someone else's call
r = c.post("/api/v1/me/calls/hide", headers=H, data={"ids": [ids["m1"], ids["x1"]]})
check("hide returns count 1 (foreign id ignored)", r.status_code == 200 and r.json()["count"] == 1, r.text)
allc = c.get("/api/v1/me/calls", headers=H).json()["calls"]
check("hidden call gone from All", ids["m1"] not in [x["id"] for x in allc])
mis = c.get("/api/v1/me/calls", headers=H, params={"view": "missed"}).json()["calls"]
check("hidden call gone from Missed", [x["id"] for x in mis].count(ids["m1"]) == 0 and len(mis) == 2, [x["id"] for x in mis])
hid = c.get("/api/v1/me/calls", headers=H, params={"view": "hidden"}).json()["calls"]
check("Hidden view shows it", [x["id"] for x in hid] == [ids["m1"]] and hid[0]["hidden"] is True, hid)
check("legacy ?missed=1 still works", len(c.get("/api/v1/me/calls", headers=H, params={"missed": 1}).json()["calls"]) == 2)
ov = c.get("/api/v1/me/overview", headers=H).json()
check("hidden missed call leaves badge", ov["missed_new"] == 1, ov)

# seen clears badge
r = c.post("/api/v1/me/calls/seen", headers=H)
check("seen ok", r.status_code == 200, r.text)
ov = c.get("/api/v1/me/overview", headers=H).json()
check("badge cleared after seen", ov["missed_new"] == 0 and ov["missed_24h"] == 0, ov)
d.execute("INSERT INTO cdr (call_id, started_at, src, dst, disposition) VALUES ('m3', ?, '5551231234', '8800', 'NO ANSWER')", (now,))
d.commit()
check("next missed call shows again", c.get("/api/v1/me/overview", headers=H).json()["missed_new"] == 1)

# unhide
r = c.post("/api/v1/me/calls/hide", headers=H, data={"ids": [ids["m1"]], "unhide": "1"})
check("unhide ok", r.status_code == 200 and r.json()["count"] == 1)
check("Hidden empty again", c.get("/api/v1/me/calls", headers=H, params={"view": "hidden"}).json()["calls"] == [])

# hiding is per user: 8801 is unaffected; still in DB
c.post("/api/v1/me/calls/hide", headers=H, data={"ids": [ids["i1"]]})
check("call row not deleted", d.execute("SELECT COUNT(*) FROM cdr WHERE id=?", (ids["i1"],)).fetchone()[0] == 1)

# 90-day purge removes hidden marks with the calls
d.execute("UPDATE cdr SET started_at='2000-01-01 00:00:00' WHERE id=?", (ids["i1"],)); d.commit()
A._purge_cdr()
check("purge clears call_hidden", d.execute("SELECT COUNT(*) FROM call_hidden WHERE cdr_id=?", (ids["i1"],)).fetchone()[0] == 0)

# no token -> 401
check("hide needs token", c.post("/api/v1/me/calls/hide", data={"ids": [1]}).status_code == 401)

# voicemail audio served to the app
os.makedirs("/var/spool/pbx/voicemail/vm-8800", exist_ok=True)
p = "/var/spool/pbx/voicemail/vm-8800/test.wav"
with wave.open(p, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
    w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(8000)))
d.execute("INSERT INTO voicemail_messages (mailbox, caller, duration_sec, path) VALUES ('vm-8800','5551112222',1,?)", (p,))
d.commit()
vm = c.get("/api/v1/me/voicemail", headers=H).json()["voicemail"][0]
a = c.get(f"/api/v1/me/voicemail/{vm['id']}/audio", headers=H)
check("vm audio 200 + wav", a.status_code == 200 and a.content[:4] == b"RIFF", (a.status_code, a.headers.get("content-type")))
check("vm audio needs token", c.get(f"/api/v1/me/voicemail/{vm['id']}/audio").status_code == 401)

print("\nFAILED:" if fails else "\nALL PASSED", fails)
sys.exit(1 if fails else 0)
