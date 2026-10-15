#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Star codes by plan: new accounts start with none, a plan turns on the codes
it includes, existing users keep what they had. Runs the API (migration, admin
pages) and pbx-brain's feature_allowed() on one throwaway DB.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-codes-by-plan.py      # exits non-zero on failure
"""
import os, sys, sqlite3
here = os.path.dirname(os.path.abspath(__file__))
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
# Simulate a PBX from before this change: codes open to all, no migration flag.
d.execute("DELETE FROM kv_settings WHERE key='mig_codes_by_plan'")
try:
    d.execute("UPDATE feature_codes SET default_access='all' WHERE code != '*555'")
except sqlite3.OperationalError:
    pass
d.commit()
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
import app as A, entitlements as E, billing
A.init_db()
sys.path.insert(0, os.path.join(here, "..", "pbx-brain"))
sys.modules.pop("app", None)
import importlib; B = importlib.import_module("app")
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
def login(name): return dict(d.execute("SELECT * FROM logins WHERE username=?", (name,)).fetchone())
def can(name, code): return B.feature_allowed(code, login(name))

acc = {r["code"]: r["default_access"] for r in d.execute("SELECT code, default_access FROM feature_codes")}
check("codes open to all became 'By plan'", all(v == "plan" for k, v in acc.items() if k != "*555"), acc)
check("*555 stays admins only", acc["*555"] == "admin")
check("existing user keeps *97", can("phone8801", "*97"))
check("existing user still can't *555", not can("phone8801", "*555"))

d.execute("INSERT INTO logins (username, pwhash, role, exten, sip_username, sip_secret, enabled)"
          " VALUES ('newbie', 'x', 'user', '8899', 'phone8899', 's', 1)"); d.commit()
check("new account: *97 off", not can("newbie", "*97"))
check("new account: *60 off", not can("newbie", "*60"))
adm = d.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
check("admin: always on", can(adm, "*97") and can(adm, "*555"))

pid = d.execute("INSERT INTO billing_plans (name, price_cents, feature_codes) VALUES ('Pro', 999, '*60,*97')").lastrowid
nid = login("newbie")["id"]
d.execute("INSERT INTO billing_customers (login_id, plan_id, status) VALUES (?,?, 'active')", (nid, pid)); d.commit()
check("plan with *97,*60: those on", can("newbie", "*97") and can("newbie", "*60"))
check("plan without *70: off", not can("newbie", "*70"))
check("admin page says why", E.code_access(d, login("newbie"), "*97") == (True, "in plan Pro"), E.code_access(d, login("newbie"), "*97"))
d.execute("UPDATE billing_customers SET status='canceled' WHERE login_id=?", (nid,)); d.commit()
check("plan lapsed: off again", not can("newbie", "*97"))
d.execute("UPDATE kv_settings SET value='*97' WHERE key='default_feature_codes'"); d.commit()
check("no-plan set *97: on", can("newbie", "*97") and not can("newbie", "*60"))
d.execute("UPDATE kv_settings SET value='' WHERE key='default_feature_codes'"); d.commit()
d.execute("UPDATE billing_customers SET status='active' WHERE login_id=?", (nid,))
d.execute("INSERT INTO login_feature_access (login_id, code, allowed) VALUES (?, '*60', 0)", (nid,)); d.commit()
check("Deny override beats the plan", not can("newbie", "*60") and can("newbie", "*97"))
d.execute("UPDATE feature_codes SET enabled=0 WHERE code='*97'"); d.commit()
check("code turned off: off for everyone", not can("newbie", "*97") and not can(adm, "*97"))
d.execute("UPDATE feature_codes SET enabled=1 WHERE code='*97'"); d.commit()

check("plan description lists star codes", "Star codes: *60, *97" in billing.plan_features(dict(d.execute("SELECT * FROM billing_plans WHERE id=?", (pid,)).fetchone())))
A.init_db()
check("migration runs once (newbie gets no override)", d.execute("SELECT COUNT(*) FROM login_feature_access WHERE login_id=? AND allowed=1", (nid,)).fetchone()[0] == 0)

# admin pages render
from fastapi.testclient import TestClient
import bcrypt
d.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(b"pw-admin-1", bcrypt.gensalt()).decode(), adm)); d.commit()
c = TestClient(A.app, client=("192.168.1.50", 5555))
c.post("/login", data={"username": adm, "password": "pw-admin-1"})
t = c.get("/features/*97/edit").text
check("feature edit page: By plan option and Now column", "By plan (users whose plan" in t and "in plan Pro" in t and "Allow override" in t, t[-500:])
check("features list says By plan", "By plan" in c.get("/features").text)
t = c.get(f"/billing/plans/{pid}/edit").text
check("plan form has star code checkboxes", 'name="codes" value="*97" checked' in t and 'name="codes" value="*70" ' in t)
check("billing access shows no-plan star codes", "Star codes (no plan)" in c.get("/billing").text)
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
