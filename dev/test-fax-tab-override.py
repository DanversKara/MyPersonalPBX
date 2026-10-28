#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Faxes tab, admin override: when the admin turns Fax ON for a user
(Billing -> User access), the Faxes tab shows in the web portal and the app
even if no phone number is routed to them yet, including in App store mode
(app + web). Turning it back to default hides it again.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-fax-tab-override.py      # exits non-zero on failure
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
pw = lambda p: bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
d.execute("UPDATE logins SET pwhash=? WHERE username=?", (pw("pw-admin-1"), adm))
d.execute("UPDATE logins SET pwhash=?, app_hide_billing=1, web_hide_billing=1 WHERE username='phone8800'", (pw("pw-user-1"),))
d.execute("DELETE FROM user_entitlements WHERE feature IN ('fax','faxes')")
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('default_quota_fax','0')")
# no number rings 8800, so it has nothing to fax from
d.execute("UPDATE inbound_routes SET enabled=0")
d.commit()
lid = d.execute("SELECT id FROM logins WHERE username='phone8800'").fetchone()[0]

A._login_failures.clear()
def client(user, pwd, ip):
    cl = TestClient(A.app, client=(ip, 5555), follow_redirects=False)
    cl.post("/login", data={"username": user, "password": pwd})
    return cl
ac = client(adm, "pw-admin-1", "192.168.1.50")
uc = client("phone8800", "pw-user-1", "192.168.1.60")
api = TestClient(A.app)
H = {"Authorization": "Bearer " + api.post("/api/v1/app/pair", json={"code": app_api._new_pair_code(lid)}).json()["token"]}
csrf = lambda cl, url: re.search(r'name="csrf_token" value="([^"]+)"', cl.get(url).text).group(1)
def access(fax):
    return ac.post(f"/billing/access/{lid}", data={"csrf_token": csrf(ac, "/billing"), "fax": fax, "faxes": ""})

check("before: no Faxes tab (web)", "/ucp/faxes" not in uc.get("/ucp").text)
check("before: app fax_enabled false", api.get("/api/v1/me/overview", headers=H).json()["fax_enabled"] is False)

r = access("1")
check("admin turns Fax on", r.status_code == 303, r.status_code)
t = uc.get("/ucp").text
check("on: Faxes tab shows in the web portal (store mode)", 'href="/ucp/faxes"' in t)
ov = api.get("/api/v1/me/overview", headers=H).json()
check("on: app shows the Faxes tab (store mode)", ov["fax_enabled"] is True and ov["hide_billing"] is True, ov)
r = uc.get("/ucp/faxes")
check("on: Faxes page opens", r.status_code == 200, r.status_code)
shown = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)\b.*?</\1>", " ", r.text, flags=re.S | re.I))
check("on: page says what's missing, no plan words",
      "No phone number is set up" in shown and not re.search(r"\bplans?\b", shown, re.I))
j = api.get("/api/v1/me/faxes", headers=H).json()
check("on: app fax list works, says what's missing", j["can_send"] is False and "number" in j["why_not"].lower(), j.get("why_not"))

r = access("")
check("back to default: tab hidden again", "/ucp/faxes" not in uc.get("/ucp").text
      and api.get("/api/v1/me/overview", headers=H).json()["fax_enabled"] is False)

# a plan-granted fax (not an admin override) still needs a number, as before
d.execute("INSERT OR REPLACE INTO user_entitlements (login_id, feature, quota, source) VALUES (?, 'fax', 1, 'plan:1')", (lid,))
d.commit()
check("plan fax with no number: unchanged (no tab)", "/ucp/faxes" not in uc.get("/ucp").text)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
