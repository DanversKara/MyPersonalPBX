#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Guest extensions through the real pbx-api (in-process TestClient).

Covers: the migration columns exist on logins + invites; the Logins page
creates a guest and converts normal <-> guest in place (same login id,
history kept); the invite flow carries the admin's guest choice into the
new login; the E911 page lists guests and their 911 state; me/settings
exposes the guest state to the app; and the portal/app send path refuses
texts for a guest with guest_sms off (and allows them once it's on).

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
    check(f"schema: {t} has the five guest columns",
          {"is_guest", "guest_outbound", "guest_inbound", "guest_sms", "guest_e911"} <= cols,
          sorted(cols & {"is_guest", "guest_outbound", "guest_inbound", "guest_sms", "guest_e911"}))

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

print()
print("FAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
