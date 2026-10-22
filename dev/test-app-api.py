#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Smoke test for the My Phone app API (app_api.py), in-process, on a throwaway DB.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-app-api.py admin123      # seed password
Prints status + body for each call; read the output.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A
c = TestClient(A.app); c.__enter__()
def j(r): print(r.status_code, r.text[:300]); return r
r = j(c.post("/api/v1/app/login", json={"username":"phone8800","password":"wrong"}))
pw = sys.argv[1]
r = j(c.post("/api/v1/app/login", json={"username":"phone8800","password":pw,"device_name":"Pixel"}))
tok = r.json()["token"]; H={"Authorization":"Bearer "+tok}
r2 = c.post("/api/v1/app/login", json={"username":"phone8801","password":pw}).json()["token"]
H2={"Authorization":"Bearer "+r2}
j(c.get("/api/v1/me/overview", headers=H))
j(c.post("/api/v1/me/messages/send", headers=H, data={"to":"8801","body":"hi there"}))
j(c.post("/api/v1/me/messages/send", headers=H2, data={"to":"8800","body":"hey back"}))
j(c.get("/api/v1/me/threads", headers=H))
th = j(c.get("/api/v1/me/threads/8801", headers=H)).json()
j(c.post("/api/v1/me/messages/delete", headers=H, data={"ids":[th["messages"][0]["id"]]}))
j(c.get("/api/v1/me/threads/8801", headers=H))
j(c.get("/api/v1/me/settings", headers=H))
j(c.post("/api/v1/me/settings/calls", headers=H, data={"dnd":"1","forward_always":"","forward_noanswer":"","ring_seconds":"30"}))
j(c.post("/api/v1/me/settings/calls", headers=H, data={"dnd":"1","ring_seconds":"7"}))
j(c.post("/api/v1/me/dnd", headers=H, data={"on":"0"}))
# --- push-to-talk (*88): me_settings exposure, settings/calls round-trip
# (preserving the wish when a POST omits it, and the cell fields when it
# doesn't), and the per-contact effective flag in the contacts payload ---
import sqlite3 as _sq
_ptt_fails = []
def _ptt_check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + "ptt: " + name, extra)
    if not ok: _ptt_fails.append(name)
def _settings(h): return c.get("/api/v1/me/settings", headers=h).json()
st = _settings(H)
_ptt_check("me_settings exposes ptt_allowed/user_ptt", "ptt_allowed" in st and "user_ptt" in st, st)
_ptt_check("ptt defaults off", st.get("ptt_allowed") is False and st.get("user_ptt") is False, st)
_full = {"dnd": "0", "forward_always": "", "forward_noanswer": "", "ring_seconds": "30",
         "cell_number": "13105550100", "ring_cell": "1", "cell_delay": "15"}
j(c.post("/api/v1/me/settings/calls", headers=H, data={**_full, "user_ptt": "1"}))
st = _settings(H)
_ptt_check("user_ptt round-trips on", st.get("user_ptt") is True, st)
_ptt_check("cell fields kept when ptt set", st.get("cell_number") == "13105550100" and st.get("ring_cell") is True, st)
j(c.post("/api/v1/me/settings/calls", headers=H, data=_full))  # no user_ptt at all
st = _settings(H)
_ptt_check("user_ptt preserved when POST omits it", st.get("user_ptt") is True, st)
_ptt_check("cell fields still kept", st.get("cell_number") == "13105550100", st)
j(c.post("/api/v1/me/settings/calls", headers=H, data={**_full, "user_ptt": "0"}))
st = _settings(H)
_ptt_check("user_ptt round-trips off", st.get("user_ptt") is False, st)
_ptt_check("cell fields kept when ptt cleared", st.get("cell_number") == "13105550100", st)
_db = _sq.connect(os.environ["PBX_DB"])
_db.execute("UPDATE logins SET ptt_allowed=1, ptt_enabled=1 WHERE exten='8801'")
_db.execute("UPDATE logins SET ptt_allowed=1 WHERE exten='8800'"); _db.commit(); _db.close()
_ptt_check("ptt_allowed reflects the admin flag", _settings(H).get("ptt_allowed") is True)
j(c.post("/api/v1/me/contacts/save", headers=H, data={"name": "PTT Pal", "number": "8801"}))
cons = c.get("/api/v1/me/contacts", headers=H).json()["contacts"]
pal = [x for x in cons if x.get("number") == "8801"]
_ptt_check("contact ptt true when login allowed+enabled", bool(pal) and pal[0].get("ptt") is True, pal)
_db = _sq.connect(os.environ["PBX_DB"])
_db.execute("UPDATE logins SET ptt_enabled=0 WHERE exten='8801'"); _db.commit(); _db.close()
cons = c.get("/api/v1/me/contacts", headers=H).json()["contacts"]
pal = [x for x in cons if x.get("number") == "8801"]
_ptt_check("contact ptt false when login wish off", bool(pal) and pal[0].get("ptt") is False, pal)
if _ptt_fails: raise SystemExit("PTT CHECKS FAILED: %s" % _ptt_fails)
j(c.get("/api/v1/me/calls", headers=H)); j(c.get("/api/v1/me/voicemail", headers=H)); j(c.get("/api/v1/me/recordings", headers=H))
j(c.post("/api/v1/me/voicemail/5/heard", headers=H))
j(c.post("/api/v1/me/block", headers=H, data={"number":"5551234567"}))
j(c.get("/api/v1/me/devices", headers=H))
j(c.get("/api/v1/me/overview", headers={"Authorization":"Bearer garbage"}))
j(c.get("/ucp", headers={"Authorization":"Bearer garbage"}, follow_redirects=False))
j(c.post("/api/v1/me/settings/password", headers=H, data={"current":pw,"new":"newpass123","confirm":"newpass123"}))
j(c.get("/api/v1/me/overview", headers=H))
# web: pairing code
c.post("/login", data={"username":"phone8801","password":pw}, follow_redirects=False)
j(c.get("/api/v1/me/overview", headers=H2))
page = c.get("/ucp/app").text
import re
csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
p = c.post("/ucp/app/pair-code", data={"csrf_token":csrf}).text
code = re.search(r"letter-spacing:3px'>([A-Z0-9]+)<", p).group(1); print("code", code)
j(c.post("/api/v1/app/pair", json={"code":code,"device_name":"Tablet"}))
j(c.post("/api/v1/app/pair", json={"code":code}))
j(c.post("/api/v1/app/logout", headers=H2)); j(c.get("/api/v1/me/overview", headers=H2))
