#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""GET /api/v1/me/features: which services are on, as on/off only. Used by the
app's "Features" screen (shown instead of "Plan & usage" in App store mode),
so it must never carry numbers, limits, plan names or prices.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-features-api.py      # exits non-zero on failure
"""
import json, os, re, sys, sqlite3
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, app_api
A.init_db()
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)

d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
lid = d.execute("SELECT id FROM logins WHERE username='phone8800'").fetchone()[0]
d.execute("UPDATE logins SET app_hide_billing=1, ptt_allowed=0 WHERE id=?", (lid,))
d.execute("DELETE FROM user_entitlements WHERE login_id=?", (lid,))
for k, v in (("call_minutes", "0"), ("messages", "50"), ("voicemail", "1"), ("recording", "0"),
             ("ivr_menus", "0"), ("push_to_talk", "0"), ("fax", "0")):
    d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES (?,?)", (f"default_quota_{k}", v))
d.execute("INSERT INTO user_entitlements (login_id, feature, quota, source) VALUES (?, 'fax', 1, 'admin')", (lid,))
d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES ('app_account_site','ownpbx.org')")
d.commit()

c = TestClient(A.app)
H = {"Authorization": "Bearer " + c.post("/api/v1/app/pair", json={"code": app_api._new_pair_code(lid)}).json()["token"]}
r = c.get("/api/v1/me/features", headers=H)
check("works in App store mode", r.status_code == 200, r.status_code)
j = r.json()
on = {f["key"]: f["on"] for f in j["features"]}
check("on/off match the account", on == {"calls": False, "messages": True, "voicemail": True, "push_to_talk": False,
                                          "recording": False, "ivr_menus": False, "fax": True}, on)
check("admin Fax override shows as on", on["fax"] is True)
check("account note in store mode", j["account_note"] == "For account details, visit ownpbx.org", j["account_note"])
raw = json.dumps(j)
check("no numbers, limits, plan or price words",
      not re.search(r"\d", json.dumps(j["features"])) and not re.search(r"plan|price|limit|left|quota|billing", raw, re.I), raw)
check("usage endpoint still closed in store mode", c.get("/api/v1/me/usage", headers=H).status_code == 404)
check("needs sign-in", c.get("/api/v1/me/features").status_code in (401, 403))

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
