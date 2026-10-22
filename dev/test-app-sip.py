#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""GET /api/v1/me/sip (app's built-in phone settings), in-process on a throwaway DB.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-app-sip.py      # exits non-zero on failure
"""
import os, sys, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, app_api
c = TestClient(A.app); c.__enter__()
d = sqlite3.connect(os.environ["PBX_DB"])
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, x); ok or fails.append(n)
def token(user):
    lid = d.execute("SELECT id FROM logins WHERE username=?", (user,)).fetchone()[0]
    return {"Authorization": "Bearer " + c.post("/api/v1/app/pair", json={"code": app_api._new_pair_code(lid)}).json()["token"]}
H = token("phone8800")

d.execute("DELETE FROM kv_settings WHERE key='sip_domain'"); d.commit()
r = c.get("/api/v1/me/sip", headers=H).json()
check("no edge domain: disabled, no password", r["enabled"] is False and r["password"] == "" and "Network" in r["reason"], r)

d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('sip_domain','sip.example.com')"); d.commit()
r = c.get("/api/v1/me/sip", headers=H).json()
check("enabled with own creds", r["enabled"] and r["username"] == "phone8800-app" and r["main_username"] == "phone8800" and r["register"] is True and r["password"] == "secret-8800"
      and r["domain"] == "sip.example.com" and r["port"] == 5061 and r["exten"] == "8800", r)
check("other user gets their own", c.get("/api/v1/me/sip", headers=token("phone8801")).json()["password"] == "secret-8801")
check("needs token", c.get("/api/v1/me/sip").status_code == 401)
d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('sip_tls_port','5071')"); d.commit()
check("custom port", c.get("/api/v1/me/sip", headers=H).json()["port"] == 5071)
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
