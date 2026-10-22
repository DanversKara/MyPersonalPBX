#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Storefront against a fake Stripe, in-process on a throwaway DB: order ->
card saved (not charged) -> admin approves (charge, account, number) or
rejects; declined card; bot and rate-limit guards.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-store.py      # exits non-zero on failure
"""
import os, sys, re, json, time, sqlite3, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, billing, store_ui, bcrypt
A.init_db()
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)
d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
def kv(k, v): d.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES (?,?)", (k, v)); d.commit()
kv("stripe_secret", "sk_test_" + "x" * 20)
pid = d.execute("INSERT INTO billing_plans (name, price_cents, voicemail, call_minutes, feature_codes, stripe_mode, stripe_price_id, active)"
                " VALUES ('Pro', 999, 1, 500, '*97', 'test', 'price_pro', 1)").lastrowid
d.execute("INSERT INTO inbound_routes (did, ring_extens) VALUES ('15550000002', '[\"8801\"]')")   # taken
d.commit()
kv("store_enabled", "1"); kv("store_dids", "15550000001,15550000002,15550000003")
CALLS = []; DECLINE = []
class FakeStripe:
    def post(self, path, params=None, idempotency_key=None):
        CALLS.append(("POST", path, params))
        if path == "/v1/customers": return {"id": "cus_1"}
        if path == "/v1/checkout/sessions": return {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"}
        if path == "/v1/subscriptions":
            if DECLINE: raise billing.StripeError("Your card was declined.")
            return {"id": "sub_1", "status": "active", "customer": "cus_1",
                    "items": {"data": [{"id": "si_1", "price": {"id": "price_pro"}, "current_period_end": int(time.time()) + 86400 * 30}]}}
        if path.startswith("/v1/subscriptions/"): return {"id": "sub_1"}
        raise AssertionError(path)
    def get(self, path, params=None):
        if path == "/v1/checkout/sessions/cs_1": return {"id": "cs_1", "status": "complete", "setup_intent": "seti_1"}
        if path == "/v1/setup_intents/seti_1": return {"payment_method": "pm_1"}
        raise AssertionError(path)
billing._stripe = lambda: FakeStripe()
SENT = []
store_ui._mail = lambda to, subj, text: SENT.append((to, subj, text)) or True

pub = TestClient(A.app, follow_redirects=False)
t = pub.get("/store").text
check("store lists the plan with features and star codes", "Pro" in t and "$9.99" in t and "Star codes: *97" in t)
check("only free numbers offered", "(555) 000-0001" in t and "(555) 000-0003" in t and "000-0002" not in t, re.findall(r"\(555\)[^<]*", t))
tok = re.search(r'name="t" value="([^"]+)"', t).group(1)
form = {"t": tok, "plan": str(pid), "did": "15550000001", "name": "Jane Doe", "email": "jane@example.com"}
r = pub.post("/store/order", data=dict(form))
check("instant post refused (bot guard)", r.status_code == 200 and "expired" in r.text)
store_ui._form_token_ok = lambda tk: True
A._login_failures.clear()
r = pub.post("/store/order", data=dict(form, website="spam"))
check("honeypot refused", "expired" in r.text)
A._login_failures.clear()
r = pub.post("/store/order", data=dict(form))
check("order goes to Stripe checkout", r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.com"), (r.status_code, r.text[-200:]))
sess = [p for m, path, p in CALLS if path == "/v1/checkout/sessions"][0]
check("checkout is setup mode (no charge)", sess["mode"] == "setup" and "line_items" not in sess)
o = d.execute("SELECT * FROM store_orders ORDER BY id DESC").fetchone()
check("order started, number held", o["status"] == "started" and "15550000001" not in store_ui.available_numbers())
r = pub.get(f"/store/thanks?o={o['token']}")
o = d.execute("SELECT * FROM store_orders WHERE id=?", (o["id"],)).fetchone()
check("back from Stripe: awaiting approval, card saved", o["status"] == "awaiting" and o["payment_method"] == "pm_1" and "haven't been charged" in r.text)
check("customer told they're not charged yet", any("haven't been charged" in x[2] for x in SENT if x[0] == "jane@example.com"))
check("no charge yet", not any(path == "/v1/subscriptions" for m, path, p in CALLS))
A._login_failures.clear()
r = pub.post("/store/order", data=dict(form, did=""))
check("second order for same email blocked while one waits", "already an order" in r.text)
check("webhook path is idempotent", store_ui.on_webhook("checkout.session.completed", {"metadata": {"pbx_store_order": o["id"]}, "status": "complete"}) is True)

adm = d.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
d.execute("UPDATE logins SET pwhash=? WHERE username=?", (bcrypt.hashpw(b"pw-admin-1", bcrypt.gensalt()).decode(), adm)); d.commit()
A._login_failures.clear()
ac = TestClient(A.app, client=("192.168.1.50", 5555), follow_redirects=False)
ac.post("/login", data={"username": adm, "password": "pw-admin-1"})
page = ac.get("/storefront").text
check("admin sees the waiting order", "Jane Doe" in page and "Awaiting approval" in page)
csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
check("admin page office-only", TestClient(A.app, follow_redirects=False).get("/storefront").status_code in (302, 303, 403))

DECLINE.append(1)
r = ac.post(f"/storefront/{o['id']}/approve", data={"csrf_token": csrf})
o2 = d.execute("SELECT * FROM store_orders WHERE id=?", (o["id"],)).fetchone()
check("declined card: order failed, nothing created", o2["status"] == "failed" and o2["login_id"] is None
      and not d.execute("SELECT 1 FROM logins WHERE display_name='Jane Doe'").fetchone())
DECLINE.clear()
d.execute("UPDATE store_orders SET status='awaiting', note='' WHERE id=?", (o["id"],)); d.commit()
A._best_effort_apply = lambda: None
r = ac.post(f"/storefront/{o['id']}/approve", data={"csrf_token": csrf})
o3 = d.execute("SELECT * FROM store_orders WHERE id=?", (o["id"],)).fetchone()
u = d.execute("SELECT * FROM logins WHERE id=?", (o3["login_id"],)).fetchone()
check("approved: login created on a free extension", o3["status"] == "approved" and u and u["username"] == u["exten"] and u["role"] == "user", dict(o3))
sub = [p for m, path, p in CALLS if path == "/v1/subscriptions"][-1]
check("charged with the saved card", sub["default_payment_method"] == "pm_1" and sub["items"][0]["price"] == "price_pro")
route = d.execute("SELECT ring_extens FROM inbound_routes WHERE did='15550000001'").fetchone()
check("number routed to the new extension", route and json.loads(route[0]) == [u["exten"]])
check("texts routed too", d.execute("SELECT dest_exten FROM did_sms_routes WHERE did='15550000001'").fetchone()[0] == u["exten"])
bc = d.execute("SELECT * FROM billing_customers WHERE login_id=?", (u["id"],)).fetchone()
check("plan active on the account", bc and bc["status"] == "active" and bc["plan_id"] == pid)
import entitlements as E
check("plan features on (voicemail, star code)", E.quota(d, u["id"], "voicemail") == 1 and E.code_access(d, dict(u), "*97")[0])
mail = [x for x in SENT if "ready" in x[1]]
check("customer emailed their login", mail and f"Username: {u['exten']}" in mail[-1][2] and "Password: " in mail[-1][2])
pw = re.search(r"Password: (\S+)", mail[-1][2]).group(1)
A._login_failures.clear()
r = TestClient(A.app, follow_redirects=False).post("/login", data={"username": u["exten"], "password": pw})
check("customer can sign in with it", r.status_code == 302 and r.headers["location"] == "/ucp")
check("number no longer for sale", "15550000001" not in store_ui.available_numbers())

A._login_failures.clear()
r = pub.post("/store/order", data={"t": "x", "plan": str(pid), "did": "", "name": "Bob", "email": "bob@example.com"})
ob = d.execute("SELECT * FROM store_orders WHERE email='bob@example.com'").fetchone()
d.execute("UPDATE store_orders SET status='awaiting' WHERE id=?", (ob["id"],)); d.commit()
n_logins = d.execute("SELECT COUNT(*) FROM logins").fetchone()[0]
r = ac.post(f"/storefront/{ob['id']}/reject", data={"csrf_token": csrf, "reason": "Out of area"})
check("reject: no account, no charge, customer told", d.execute("SELECT status FROM store_orders WHERE id=?", (ob["id"],)).fetchone()[0] == "rejected"
      and d.execute("SELECT COUNT(*) FROM logins").fetchone()[0] == n_logins and any("Out of area" in x[2] and "not charged" in x[2] for x in SENT))

A._login_failures.clear()
for _ in range(10): A._login_failed("testclient", "store")
check("rate limit", pub.post("/store/order", data=form).status_code == 429)
kv("store_enabled", "0")
check("closed store: not available", pub.get("/store").status_code == 404)
print("ALL PASSED" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
