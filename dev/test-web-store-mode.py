#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""App store mode, web switch (Billing -> App store mode, "Hide in web portal"):
the web My Phone portal hides the Billing tab, "Your plan this month" and every
bit of plan / money wording, and /ucp/billing is closed. Per user, for everyone,
and from the login edit page. Other users are unchanged.

    export PBX_DB=/tmp/apptest.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-web-store-mode.py      # exits non-zero on failure
"""
import os, re, sys, sqlite3
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A, bcrypt
A.init_db()
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, "" if ok else x); ok or fails.append(n)

d = sqlite3.connect(os.environ["PBX_DB"]); d.row_factory = sqlite3.Row
adm = d.execute("SELECT username FROM logins WHERE role='admin' LIMIT 1").fetchone()[0]
pw = lambda p: bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
d.execute("UPDATE logins SET pwhash=? WHERE username=?", (pw("pw-admin-1"), adm))
for u in ("phone8800", "phone8801"):
    d.execute("UPDATE logins SET pwhash=?, web_hide_billing=0, app_hide_billing=0 WHERE username=?", (pw("pw-user-1"), u))
# Plan limits that make the portal show its "not in your plan" notes.
for k, v in (("messages", "0"), ("recording", "0"), ("voicemail", "0"), ("call_minutes", "0"), ("ivr_menus", "0")):
    d.execute("INSERT OR REPLACE INTO kv_settings (key,value) VALUES (?,?)", (f"default_quota_{k}", v))
d.execute("DELETE FROM kv_settings WHERE key IN ('web_hide_billing_all','app_hide_billing_all')")
d.commit()
def lid(u): return d.execute("SELECT id FROM logins WHERE username=?", (u,)).fetchone()[0]

A._login_failures.clear()
def client(user, pwd, ip):
    cl = TestClient(A.app, client=(ip, 5555), follow_redirects=False)
    cl.post("/login", data={"username": user, "password": pwd})
    return cl
ac = client(adm, "pw-admin-1", "192.168.1.50")
u0 = client("phone8800", "pw-user-1", "192.168.1.60")
u1 = client("phone8801", "pw-user-1", "192.168.1.61")
csrf = lambda cl, url: re.search(r'name="csrf_token" value="([^"]+)"', cl.get(url).text).group(1)

PAGES = ["/ucp", "/ucp/calls", "/ucp/voicemail", "/ucp/recordings", "/ucp/messages",
         "/ucp/contacts", "/ucp/ivr", "/ucp/settings", "/ucp/app", "/ucp/faxes"]
MONEY = re.compile(r"\b(plans?|billing|upgrade|price[sd]?|subscri\w*|payment|stripe|invoice)\b", re.I)

def visible(html):
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html)

def money_words(cl):
    hits = {}
    for p in PAGES:
        r = cl.get(p)
        if r.status_code != 200:
            continue
        found = sorted({m.group(0) for m in MONEY.finditer(visible(r.text))})
        if found:
            ctx = [visible(r.text)[max(0, m.start() - 50):m.end() + 30] for m in MONEY.finditer(visible(r.text))][:3]
            hits[p] = (found, ctx)
    return hits

# ---- off by default
t = u0.get("/ucp").text
check("off: Overview shows 'Your plan this month'", "Your plan this month" in t)
check("off: Billing tab shown", 'href="/ucp/billing"' in t)
check("off: /ucp/billing opens", u0.get("/ucp/billing").status_code == 200)

# ---- admin page
t = ac.get("/billing").text
check("admin: App store mode has the web column + all-users switch",
      'name="web_ids"' in t and 'name="web_all"' in t and "Hide in web portal" in t)

# ---- turn on for one user (web only)
r = ac.post("/billing/app-store-mode", data={"csrf_token": csrf(ac, "/billing"), "web_ids": str(lid("phone8800")),
                                             "site": "myphone.example.com"})
row = d.execute("SELECT app_hide_billing, web_hide_billing FROM logins WHERE id=?", (lid("phone8800"),)).fetchone()
check("saved: web on, app untouched", r.status_code == 303 and row["web_hide_billing"] == 1 and row["app_hide_billing"] == 0, dict(row))

t = u0.get("/ucp").text
check("on: no 'Your plan this month' / 'This month (resets'", "Your plan this month" not in t and "resets on the 1st" not in t)
check("on: no Billing tab or link anywhere", "/ucp/billing" not in t and ">Billing<" not in t)
r = u0.get("/ucp/billing")
check("on: /ucp/billing sends them to the Overview", r.status_code == 303 and r.headers["location"] == "/ucp", r.status_code)
r = u0.post("/ucp/billing/cancel", data={"csrf_token": csrf(u0, "/ucp")})
check("on: billing actions refused", r.status_code == 404, r.status_code)
hits = money_words(u0)
check("on: no plan/money words on any My Phone page", not hits, hits)
t = u0.get("/ucp/messages").text
check("on: no 'texts left this month'", "left this month" not in t)
check("on: texting still explains it's unavailable", "isn't available" in visible(t) or "isn&#x27;t available" in t)

# ---- other user unchanged
t = u1.get("/ucp").text
check("other user: unchanged", "Your plan this month" in t and 'href="/ucp/billing"' in t)

# ---- all users
ac.post("/billing/app-store-mode", data={"csrf_token": csrf(ac, "/billing"), "web_all": "1", "site": ""})
t = u1.get("/ucp").text
check("all users: everyone hidden", "Your plan this month" not in t and "/ucp/billing" not in t)
ac.post("/billing/app-store-mode", data={"csrf_token": csrf(ac, "/billing"), "site": ""})
check("all off again", "Your plan this month" in u1.get("/ucp").text and "Your plan this month" in u0.get("/ucp").text)

# ---- login edit page
t = ac.get(f"/logins/{lid('phone8801')}/edit").text
check("login edit page has the web tick", 'name="web_hide_billing"' in t)

# ---- admin pages keep their billing wording
check("admin Billing page untouched", "Plans" in ac.get("/billing").text)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
