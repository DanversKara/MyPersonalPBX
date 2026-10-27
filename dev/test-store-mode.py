#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""App store mode (Billing): users whose My Phone app shows no plan / billing
info. Per user and for everyone; overview flags, usage hidden, plan wording
replaced by "not available" + where to find account details.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-store-mode.py      # exits non-zero on failure
"""
import os, re, sys, sqlite3
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, app_api, bcrypt
A.init_db()
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
adm = d.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
d.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(b"pw-admin-1", bcrypt.gensalt()).decode(), adm))
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_messages','50')")
d.commit()
def lid(u): return d.execute("SELECT id FROM logins WHERE username=?", (u,)).fetchone()[0]
c = TestClient(A.app)
def tok(u): return {"Authorization": "Bearer " + c.post("/api/v1/app/pair", json={"code": app_api._new_pair_code(lid(u))}).json()["token"]}
H, H2 = tok("phone8800"), tok("phone8801")
A._login_failures.clear()
ac = TestClient(A.app, client=("192.168.1.50", 5555), follow_redirects=False)
ac.post("/login", data={"username": adm, "password": "pw-admin-1"})
csrf = lambda url: re.search(r'name="csrf_token" value="([^"]+)"', ac.get(url).text).group(1)

ov = c.get("/api/v1/me/overview", headers=H).json()
check("off by default: usage and texts left shown", ov["hide_billing"] is False and ov["texts_left"] == 50
      and c.get("/api/v1/me/usage", headers=H).status_code == 200, ov)
t = ac.get("/billing").text
check("Billing has App store mode", "App store mode" in t and 'action="/billing/app-store-mode"' in t)
r = ac.post("/billing/app-store-mode", data={"csrf_token": csrf("/billing"), "ids": str(lid("phone8800")), "site": "myphone.dox.box"})
check("saved for one user", r.status_code == 303 and d.execute("SELECT app_hide_billing FROM logins WHERE id=?", (lid("phone8800"),)).fetchone()[0] == 1)
ov = c.get("/api/v1/me/overview", headers=H).json()
check("that user: no texts left, flag + account note", ov["hide_billing"] is True and ov["texts_left"] is None
      and ov["account_note"] == "For account details, visit myphone.dox.box", ov)
check("that user: usage hidden", c.get("/api/v1/me/usage", headers=H).status_code == 404)
check("other user unchanged", c.get("/api/v1/me/overview", headers=H2).json()["hide_billing"] is False)
check("plan wording replaced", app_api.neutral({"id": lid("phone8800")}, "You have no texts left on your plan this month.")
      == "This isn't available on your account. For account details, visit myphone.dox.box.")
check("other wording kept", app_api.neutral({"id": lid("phone8800")}, "Saved.") == "Saved.")
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_messages','0')"); d.commit()
r = c.post("/api/v1/me/messages/send", headers=H, data={"to": "8801", "body": "hi"}).json()
check("a refused text says 'not available', no plan", "plan" not in r.get("message", "").lower(), r)
r = ac.post("/billing/app-store-mode", data={"csrf_token": csrf("/billing"), "all": "1", "site": "myphone.dox.box"})
check("all users: everyone hidden", c.get("/api/v1/me/overview", headers=H2).json()["hide_billing"] is True)
t = ac.get(f"/logins/{lid('phone8801')}/edit").text
check("login edit page has the per-user tick", 'name="app_hide_billing"' in t)
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
