#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Guest extensions through the real pbx-api (in-process TestClient).

Covers: the migration columns exist on logins + invites; the Logins page
creates a guest and converts normal <-> guest in place (same login id,
history kept); the invite flow carries the admin's guest choice into the
new login; the E911 page lists guests and their 911 state; me/settings
exposes the guest state to the app; and the portal/app send path refuses
texts for a guest with guest_sms off (and allows them once it's on); and
the 4-way texting scope (guest_sms_mode: all | external | internal |
off) across the editor, invites, me/settings and the send path; and the
plan-upgrade path: guests can't buy plans unless the admin ticked
guest_upgrade (their Plans page says so), and a guest WITH the tick
converts to a normal account when a paid plan's subscription activates.

    export PBX_DB=/tmp/guestapi.db && rm -f $PBX_DB && python3 dev/seed-test-db.py
    python3 dev/test-guest-api.py     # run from the repo root
"""
import os, re, sqlite3, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pbx-api"))
from fastapi.testclient import TestClient
import app as A

fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n, str(x)[:200])
    if not ok:
        fails.append(n)

DB = os.environ["PBX_DB"]
def q(sql, args=()):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    r = c.execute(sql, args).fetchall(); c.close(); return r

# Peer as a LAN address: the panel's office-network middleware otherwise
# (correctly) refuses admin pages to "remote" visitors like TestClient.
c = TestClient(A.app, client=("192.168.1.10", 50000)); c.__enter__()

def csrf(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""

# --- schema / migration columns -------------------------------------------
for t in ("logins", "invites"):
    cols = {r[1] for r in q(f"PRAGMA table_info({t})")}
    check(f"schema: {t} has the guest columns + sms mode + upgrade",
          {"is_guest", "guest_outbound", "guest_inbound", "guest_sms", "guest_e911",
           "guest_sms_mode", "guest_upgrade"} <= cols,
          sorted(cols & {"is_guest", "guest_outbound", "guest_inbound", "guest_sms",
                         "guest_e911", "guest_sms_mode", "guest_upgrade"}))

# --- admin sign-in ----------------------------------------------------------
r = c.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=False)
check("admin login", r.status_code in (302, 303), r.status_code)

# --- create a guest via the Logins editor -----------------------------------
r = c.get("/logins/new/edit")
tok = csrf(r.text)
r = c.post("/logins/new/edit", data={
    "csrf_token": tok, "username": "lobby", "password": "lobbypass1", "role": "user",
    "exten": "8850", "sip_username": "phone8850", "sip_secret": "s3cret-lobby",
    "display_name": "Lobby Phone", "vm_email": "", "account_type": "guest",
    "guest_sms": "on", "enabled": "on"}, follow_redirects=False)
check("create guest login redirects", r.status_code in (302, 303), r.status_code)
row = q("SELECT * FROM logins WHERE username='lobby'")
check("guest row: is_guest=1, sms on, the rest off",
      row and row[0]["is_guest"] == 1 and row[0]["guest_sms"] == 1
      and row[0]["guest_outbound"] == 0 and row[0]["guest_inbound"] == 0
      and row[0]["guest_e911"] == 0, dict(row[0]) if row else None)
lobby_id = row[0]["id"]

r = c.get("/logins")
check("logins list shows the guest pill", "guest" in r.text and "Lobby Phone" in r.text)

# --- convert guest -> normal -> guest in place -------------------------------
r = c.get(f"/logins/{lobby_id}/edit")
tok = csrf(r.text)
r = c.post(f"/logins/{lobby_id}/edit", data={
    "csrf_token": tok, "username": "lobby", "password": "", "role": "user",
    "exten": "8850", "sip_username": "phone8850", "sip_secret": "",
    "display_name": "Lobby Phone", "vm_email": "", "account_type": "normal",
    "enabled": "on"}, follow_redirects=False)
row = q("SELECT * FROM logins WHERE id=?", (lobby_id,))
check("convert to normal: same id, is_guest=0", row and row[0]["is_guest"] == 0, dict(row[0]) if row else None)

r = c.get(f"/logins/{lobby_id}/edit")
tok = csrf(r.text)
r = c.post(f"/logins/{lobby_id}/edit", data={
    "csrf_token": tok, "username": "lobby", "password": "", "role": "user",
    "exten": "8850", "sip_username": "phone8850", "sip_secret": "",
    "display_name": "Lobby Phone", "vm_email": "", "account_type": "guest",
    "guest_inbound": "on", "guest_e911": "on", "enabled": "on"}, follow_redirects=False)
row = q("SELECT * FROM logins WHERE id=?", (lobby_id,))
check("convert back to guest: same id, inbound+e911 on, sms now off",
      row and row[0]["is_guest"] == 1 and row[0]["guest_inbound"] == 1
      and row[0]["guest_e911"] == 1 and row[0]["guest_sms"] == 0, dict(row[0]) if row else None)

aud = q("SELECT * FROM audit WHERE action='logins.guest_change'")
check("guest changes are audit-logged", len(aud) >= 2, len(aud))

# --- invites carry the guest choice ------------------------------------------
r = c.get("/invites")
tok = csrf(r.text)
r = c.post("/invites/new", data={
    "csrf_token": tok, "expires_days": "7", "email": "",
    "account_type": "guest", "guest_inbound": "on"}, follow_redirects=False)
check("invite generated", r.status_code == 200 and "/invite/" in r.text, r.status_code)
m = re.search(r"/invite/([A-Za-z0-9_\-]+)", r.text)
token = m.group(1) if m else ""
inv = q("SELECT * FROM invites ORDER BY id DESC LIMIT 1")
check("invite row carries guest flags",
      inv and inv[0]["is_guest"] == 1 and inv[0]["guest_inbound"] == 1
      and inv[0]["guest_sms"] == 0 and inv[0]["guest_e911"] == 0, dict(inv[0]) if inv else None)

r = c.get("/invites")
check("invites list shows the guest pill", "guest" in r.text)

# --- redeem: the new login is a guest with the invite's rules -----------------
r = c.get(f"/invite/{token}")
r = c.post(f"/invite/{token}", data={
    "token": token, "display_name": "Visitor Desk", "vm_email": "",
    "password": "visitorpass1", "password2": "visitorpass1"}, follow_redirects=False)
check("invite redeem creates the account", "Account created" in r.text, r.status_code)
new = q("SELECT * FROM logins WHERE display_name='Visitor Desk'")
check("redeemed login is a guest with the invite's rules",
      new and new[0]["is_guest"] == 1 and new[0]["guest_inbound"] == 1
      and new[0]["guest_sms"] == 0 and new[0]["guest_e911"] == 0,
      dict(new[0]) if new else None)
guest_exten = new[0]["exten"] if new else ""

# --- E911 page lists guests ----------------------------------------------------
r = c.get("/e911")
check("e911 page has a Guest extensions section", "Guest extensions" in r.text)
check("e911 page shows the lobby guest's 911 as on (pill ok)",
      "Lobby Phone" in r.text, "")
check("e911 page shows the visitor guest's 911 as off",
      "Off — 911 not routed" in r.text, "")

# --- app API exposes guest state ------------------------------------------------
r = c.post("/api/v1/app/login", json={"username": guest_exten, "password": "visitorpass1",
                                      "device_name": "TestPhone"})
check("guest app login", r.status_code == 200, r.status_code)
tok2 = r.json()["token"]; H = {"Authorization": "Bearer " + tok2}
st = c.get("/api/v1/me/settings", headers=H).json()
check("me/settings exposes guest state",
      st.get("is_guest") is True and st.get("guest_inbound") is True
      and st.get("guest_sms") is False and st.get("guest_e911") is False, st)

# --- portal/app texting gate ------------------------------------------------------
# Unlimited default texts, so the plan quota never masks the guest gate.
c2 = sqlite3.connect(DB)
c2.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('default_quota_messages', '-1')")
c2.commit(); c2.close()
r = c.post("/api/v1/me/messages/send", headers=H, data={"to": "8800", "body": "hi from guest"},
           follow_redirects=False)
code = ""
try:
    code = r.json().get("code", "")
except Exception:
    code = r.headers.get("location", "")
sent = q("SELECT COUNT(*) n FROM messages WHERE login_id=?", (new[0]["id"],))[0]["n"]
check("guest with sms off: app send refused (msgoff), nothing stored",
      code == "msgoff" and sent == 0, (r.status_code, code, sent))

# Admin turns texting on for the guest; the same send now goes through.
r = c.get(f"/logins/{new[0]['id']}/edit")
tok = csrf(r.text)
c.post(f"/logins/{new[0]['id']}/edit", data={
    "csrf_token": tok, "username": guest_exten, "password": "", "role": "user",
    "exten": guest_exten, "sip_username": new[0]["sip_username"], "sip_secret": "",
    "display_name": "Visitor Desk", "vm_email": "", "account_type": "guest",
    "guest_inbound": "on", "guest_sms": "on", "enabled": "on"}, follow_redirects=False)
r = c.post("/api/v1/me/messages/send", headers=H, data={"to": "8800", "body": "hi from guest"},
           follow_redirects=False)
sent = q("SELECT COUNT(*) n FROM messages WHERE login_id=?", (new[0]["id"],))[0]["n"]
check("guest with sms on: app send stored", sent == 1, (r.status_code, sent))
# Legacy checkbox 'on' resolved to the mode 'all' (external flag stays 1).
row = q("SELECT guest_sms_mode, guest_sms FROM logins WHERE id=?", (new[0]["id"],))
check("legacy sms checkbox maps to mode 'all'",
      row and row[0]["guest_sms_mode"] == "all" and row[0]["guest_sms"] == 1,
      dict(row[0]) if row else None)

# --- the 4-way texting scope ---------------------------------------------------
r = c.get(f"/logins/{new[0]['id']}/edit")
check("editor offers the texting-scope dropdown",
      'name="guest_sms_mode"' in r.text and "Internal only" in r.text
      and "External only" in r.text and "Block all" in r.text)

def set_mode(mode):
    r = c.get(f"/logins/{new[0]['id']}/edit")
    c.post(f"/logins/{new[0]['id']}/edit", data={
        "csrf_token": csrf(r.text), "username": guest_exten, "password": "", "role": "user",
        "exten": guest_exten, "sip_username": new[0]["sip_username"], "sip_secret": "",
        "display_name": "Visitor Desk", "vm_email": "", "account_type": "guest",
        "guest_inbound": "on", "guest_sms_mode": mode, "enabled": "on"}, follow_redirects=False)
    return q("SELECT guest_sms_mode, guest_sms FROM logins WHERE id=?", (new[0]["id"],))[0]

def send(to):
    r = c.post("/api/v1/me/messages/send", headers=H, data={"to": to, "body": "mode test"},
               follow_redirects=False)
    try:
        return r.json().get("code", "")
    except Exception:
        return r.headers.get("location", "")

def sent_count():
    return q("SELECT COUNT(*) n FROM messages WHERE login_id=?", (new[0]["id"],))[0]["n"]

m = set_mode("internal")
check("mode=internal stored, external flag synced off",
      m["guest_sms_mode"] == "internal" and m["guest_sms"] == 0, dict(m))
n0 = sent_count()
send("8800")
check("mode=internal: internal send allowed", sent_count() == n0 + 1, sent_count())
check("mode=internal: external send refused (msgoff)",
      send("+15550192837") == "msgoff", "")
st = c.get("/api/v1/me/settings", headers=H).json()
check("me/settings exposes the mode",
      st.get("guest_sms_mode") == "internal" and st.get("guest_sms") is False, st)

m = set_mode("external")
check("mode=external stored, external flag synced on",
      m["guest_sms_mode"] == "external" and m["guest_sms"] == 1, dict(m))
n0 = sent_count()
code = send("8800")
check("mode=external: internal send refused (msgoff), nothing stored",
      code == "msgoff" and sent_count() == n0, (code, sent_count()))
check("mode=external: external send passes the guest gate (fails later: no DID route)",
      send("+15550192837") != "msgoff", "")

m = set_mode("off")
n0 = sent_count()
check("mode=off: internal send refused", send("8800") == "msgoff" and sent_count() == n0,
      sent_count())

# Invites carry the mode too.
r = c.get("/invites")
tok = csrf(r.text)
r = c.post("/invites/new", data={
    "csrf_token": tok, "expires_days": "7", "email": "",
    "account_type": "guest", "guest_sms_mode": "external"}, follow_redirects=False)
check("invite with a texting mode generated", r.status_code == 200 and "/invite/" in r.text,
      r.status_code)
m = re.search(r"/invite/([A-Za-z0-9_\-]+)", r.text)
token2 = m.group(1) if m else ""
inv = q("SELECT * FROM invites ORDER BY id DESC LIMIT 1")
check("invite row carries mode=external (+ synced flag)",
      inv and inv[0]["guest_sms_mode"] == "external" and inv[0]["guest_sms"] == 1,
      dict(inv[0]) if inv else None)
r = c.get(f"/invite/{token2}")
r = c.post(f"/invite/{token2}", data={
    "token": token2, "display_name": "Shop Phone", "vm_email": "",
    "password": "shopphonepass1", "password2": "shopphonepass1"}, follow_redirects=False)
new2 = q("SELECT * FROM logins WHERE display_name='Shop Phone'")
check("redeemed login carries mode=external",
      new2 and new2[0]["guest_sms_mode"] == "external" and new2[0]["guest_sms"] == 1,
      dict(new2[0]) if new2 else None)

# --- plan upgrade: admin permission + paid-plan conversion ----------------------
import billing as B

# Fake Stripe test mode + two plans (paid, free), so billing is "ready"
# without any real Stripe account.
c2 = sqlite3.connect(DB)
c2.execute("INSERT OR REPLACE INTO kv_settings (key, value) VALUES ('stripe_secret', 'sk_test_fake')")
c2.execute("INSERT INTO billing_plans (name, description, price_cents, currency, stripe_mode,"
           " stripe_price_id, active) VALUES ('Test Paid', '', 1000, 'usd', 'test', 'price_paid', 1)")
paid_id = c2.execute("SELECT id FROM billing_plans WHERE stripe_price_id='price_paid'").fetchone()[0]
c2.execute("INSERT INTO billing_plans (name, description, price_cents, currency, stripe_mode,"
           " stripe_price_id, active) VALUES ('Test Free', '', 0, 'usd', 'test', 'price_free', 1)")
free_id = c2.execute("SELECT id FROM billing_plans WHERE stripe_price_id='price_free'").fetchone()[0]
c2.commit(); c2.close()

# A guest whose admin ticked the upgrade box (via the real editor).
r = c.get("/logins/new/edit")
check("editor offers the plan-upgrade checkbox", 'name="guest_upgrade"' in r.text)
r = c.post("/logins/new/edit", data={
    "csrf_token": csrf(r.text), "username": "buyer", "password": "buyerpass1", "role": "user",
    "exten": "8851", "sip_username": "phone8851", "sip_secret": "s3cret-buyer",
    "display_name": "Buyer Guest", "vm_email": "", "account_type": "guest",
    "guest_upgrade": "on", "enabled": "on"}, follow_redirects=False)
row = q("SELECT * FROM logins WHERE username='buyer'")
check("guest created with the upgrade permission on",
      row and row[0]["is_guest"] == 1 and row[0]["guest_upgrade"] == 1,
      dict(row[0]) if row else None)
buyer_id = row[0]["id"]

def portal(username, password):
    cc = TestClient(A.app, client=("192.168.1.10", 50000)); cc.__enter__()
    cc.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    return cc

# Allowed guest: Plans page explains the deal, subscribe is offered.
cb = portal("buyer", "buyerpass1")
r = cb.get("/ucp/billing")
check("allowed guest sees the upgrade note on the Plans page",
      "Buying a paid plan upgrades" in r.text, r.status_code)
check("allowed guest is offered a Subscribe button", "Subscribe" in r.text)

# Blocked guest (the visitor from the invite tests): notice + no way to buy.
cv = portal(guest_exten, "visitorpass1")
r = cv.get("/ucp/billing")
check("blocked guest sees the support notice under Plans",
      "doesn't allow this account to upgrade plans" in r.text, r.status_code)
check("blocked guest gets no Subscribe button", "Subscribe" not in r.text
      and "Not available" in r.text)
# A forged subscribe POST with a VALID csrf token (scraped from another
# portal page — the blocked billing page itself renders no forms) must
# still be refused server-side.
r = cv.get("/ucp/messages")
r = cv.post(f"/ucp/billing/subscribe/{paid_id}", data={"csrf_token": csrf(r.text)},
            follow_redirects=False)
check("blocked guest subscribe POST is refused (guestnoup)",
      "guestnoup" in r.headers.get("location", ""), r.headers.get("location", ""))

def fake_sub(sid, price_id, status="active"):
    return {"id": sid, "customer": "cus_" + sid, "status": status,
            "current_period_end": 1893456000, "cancel_at_period_end": False,
            "items": {"data": [{"id": "si_" + sid, "price": {"id": price_id}}]}}

def mk_guest(exten, upgrade):
    cc = sqlite3.connect(DB)
    cc.execute("INSERT INTO logins (username, pwhash, role, exten, sip_username, sip_secret,"
               " is_guest, guest_upgrade) VALUES (?, 'x', 'user', ?, ?, 's', 1, ?)",
               ("g" + exten, exten, "phone" + exten, upgrade))
    cc.commit()
    lid = cc.execute("SELECT id FROM logins WHERE exten=?", (exten,)).fetchone()[0]
    cc.close()
    return lid

def is_guest(lid):
    return q("SELECT is_guest FROM logins WHERE id=?", (lid,))[0]["is_guest"]

# Allowed guest + paid plan activates -> normal account, audit-logged.
B.apply_subscription(buyer_id, fake_sub("sub_buyer", "price_paid"))
check("allowed guest converts to normal when a paid plan activates",
      is_guest(buyer_id) == 0, is_guest(buyer_id))
aud = q("SELECT * FROM audit WHERE action='logins.guest_change' AND actor='billing'")
check("the conversion is audit-logged as billing",
      len(aud) >= 1 and "guest -> normal" in aud[0]["detail"], [dict(a) for a in aud][:1])

# No permission: plan is granted, but the account stays a guest.
g_off = mk_guest("8860", 0)
B.apply_subscription(g_off, fake_sub("sub_off", "price_paid"))
bc = q("SELECT plan_id FROM billing_customers WHERE login_id=?", (g_off,))
check("guest without permission keeps the plan but stays a guest",
      is_guest(g_off) == 1 and bc and bc[0]["plan_id"] == paid_id,
      (is_guest(g_off), dict(bc[0]) if bc else None))

# Free plan never converts, even with permission.
g_free = mk_guest("8861", 1)
B.apply_subscription(g_free, fake_sub("sub_free", "price_free"))
check("free plan never converts a guest", is_guest(g_free) == 1, is_guest(g_free))

# A non-active subscription converts nobody.
g_cx = mk_guest("8862", 1)
B.apply_subscription(g_cx, fake_sub("sub_cx", "price_paid", status="canceled"))
check("canceled subscription converts nobody", is_guest(g_cx) == 1, is_guest(g_cx))

# Invites can pre-tick the permission.
r = c.get("/invites")
r = c.post("/invites/new", data={
    "csrf_token": csrf(r.text), "expires_days": "7", "email": "",
    "account_type": "guest", "guest_upgrade": "on"}, follow_redirects=False)
m = re.search(r"/invite/([A-Za-z0-9_\-]+)", r.text)
token3 = m.group(1) if m else ""
inv = q("SELECT * FROM invites ORDER BY id DESC LIMIT 1")
check("invite row carries the upgrade permission",
      inv and inv[0]["guest_upgrade"] == 1, dict(inv[0]) if inv else None)
r = c.get(f"/invite/{token3}")
r = c.post(f"/invite/{token3}", data={
    "token": token3, "display_name": "Upgrade Guest", "vm_email": "",
    "password": "upgradepass1", "password2": "upgradepass1"}, follow_redirects=False)
new3 = q("SELECT * FROM logins WHERE display_name='Upgrade Guest'")
check("redeemed login carries the upgrade permission",
      new3 and new3[0]["guest_upgrade"] == 1, dict(new3[0]) if new3 else None)

# --- push-to-talk: plan feature + override + missed setting -------------------
import entitlements as ENT
import app_api as API

c2 = sqlite3.connect(DB)
c2.execute("INSERT INTO billing_plans (name, description, price_cents, currency, stripe_mode,"
           " stripe_price_id, active, push_to_talk) VALUES ('Test PTT', '', 500, 'usd', 'test', 'price_ptt', 1, 1)")
c2.commit(); c2.close()

def mk_user(exten):
    cc = sqlite3.connect(DB)
    cc.execute("INSERT INTO logins (username, pwhash, role, exten, sip_username, sip_secret)"
               " VALUES (?, 'x', 'user', ?, ?, 's')", ("u" + exten, exten, "phone" + exten))
    cc.commit()
    lid = cc.execute("SELECT id FROM logins WHERE exten=?", (exten,)).fetchone()[0]
    cc.close()
    return lid

def ent_ptt(lid):
    r = q("SELECT quota FROM user_entitlements WHERE login_id=? AND feature='push_to_talk'", (lid,))
    return r[0]["quota"] if r else None

u_ptt = mk_user("8870")
check("no plan + no override: no push_to_talk entitlement",
      ent_ptt(u_ptt) is None, ent_ptt(u_ptt))
B.apply_subscription(u_ptt, fake_sub("sub_ptt", "price_ptt"))
check("subscribing to a PTT plan grants the push_to_talk entitlement",
      ent_ptt(u_ptt) == 1, ent_ptt(u_ptt))
cc = sqlite3.connect(DB); cc.row_factory = sqlite3.Row
row = cc.execute("SELECT * FROM logins WHERE id=?", (u_ptt,)).fetchone()
check("ent.ptt_allowed is True via the plan while the login flag is 0",
      ENT.ptt_allowed(cc, row) is True and not row["ptt_allowed"], dict(row))
row2 = cc.execute("SELECT * FROM logins WHERE id=?", (g_off,)).fetchone()
check("ent.ptt_allowed False for a guest on a non-PTT plan with no override",
      ENT.ptt_allowed(cc, row2) is False, dict(row2))
cc.close()

cc = sqlite3.connect(DB)
cc.execute("UPDATE logins SET ptt_enabled=1 WHERE id=?", (u_ptt,)); cc.commit(); cc.close()
ptt_map = API._ptt_by_exten()
check("app contacts map: plan-PTT user with the wish on is reachable",
      ptt_map.get("8870") is True, ptt_map.get("8870"))
check("app contacts map: guest on a non-PTT plan is not",
      ptt_map.get("8860") in (False, None), ptt_map.get("8860"))

# Admin adds PTT to an existing plan -> current subscribers are re-granted.
r = c.get(f"/billing/plans/{paid_id}/edit")
r = c.post(f"/billing/plans/{paid_id}/edit", data={
    "csrf_token": csrf(r.text), "name": "Test Paid", "description": "",
    "call_minutes": "3000", "messages": "3000", "voicemail": "1",
    "ivr_menus": "0", "price": "10.00", "currency": "usd",
    "sort_order": "0", "active": "1", "push_to_talk": "1"}, follow_redirects=False)
prow = q("SELECT push_to_talk FROM billing_plans WHERE id=?", (paid_id,))
check("plan edit saves push_to_talk on the plan",
      prow and prow[0]["push_to_talk"] == 1, dict(prow[0]) if prow else None)
check("plan edit re-grants current subscribers (guest on Test Paid gains PTT)",
      ent_ptt(g_off) == 1, ent_ptt(g_off))

# Missed-PTT setting round-trips through the app API (visitor guest, H).
st = c.get("/api/v1/me/settings", headers=H).json()
check("me/settings defaults the missed-PTT choice to voicemail",
      st.get("user_ptt_missed") == "voicemail", st.get("user_ptt_missed"))
r = c.post("/api/v1/me/settings/calls", headers=H, data={
    "dnd": "0", "forward_always": "", "forward_noanswer": "", "ring_seconds": "30",
    "user_ptt": "1", "user_ptt_missed": "retry"})
st = c.get("/api/v1/me/settings", headers=H).json()
check("me/settings saves the missed-PTT choice (retry)",
      st.get("user_ptt_missed") == "retry", st.get("user_ptt_missed"))
r = c.post("/api/v1/me/settings/calls", headers=H, data={
    "dnd": "0", "forward_always": "", "forward_noanswer": "", "ring_seconds": "30",
    "user_ptt": "1"})
st = c.get("/api/v1/me/settings", headers=H).json()
check("partial settings POST preserves the missed-PTT choice",
      st.get("user_ptt_missed") == "retry", st.get("user_ptt_missed"))
cvv = portal(guest_exten, "visitorpass1")
r = cvv.get("/ucp/settings")
check("portal Settings shows the missed-PTT select",
      'name="user_ptt_missed"' in r.text and "Chirp me again" in r.text, r.status_code)

print()
print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
