# Guest extensions — deploy notes

## What this is
A restricted account type for own-pbx: **guest extensions**. Internal
calling always works; outside calling, receiving PSTN calls, SMS/MMS and
911 are each OFF for a guest unless the admin turns that rule on for that
extension. Set the type + rules when creating a login or generating an
invite, or convert any existing login either way under Logins → Edit
(same account, extension and history; takes effect immediately). The
E911 page lists every guest and its 911 state; type/rule changes are
audit-logged (`logins.guest_change`).

## Update: 4-way texting scope
The SMS/MMS rule is now a dropdown — **Both external and internal**,
**External only**, **Internal only**, **Block all** — stored as
`logins.guest_sms_mode` / `invites.guest_sms_mode`
(`all | external | internal | off`). `guest_sms` remains as the synced
"external allowed" flag, so the external gates (voip.ms webhook, AGI
external route, portal external send) are unchanged; the internal paths
(portal/app send, brain `send_message_api`, AGI internal route) check
the mode. The boot migration adds the column and backfills existing
guests (texting on -> `all`, off -> `off`), so upgrading changes
nobody's behavior. Deploy is the usual `pbx-update`; no app change.

## Files
- `db/schema.sql` — the guest columns on `logins` and `invites`
  (fresh installs), incl. `guest_sms_mode`.
- `pbx-api/app.py` — idempotent migrations for existing DBs; Logins list
  Type column + editor account-type/rules (texting = the scope
  dropdown); Invites generator picker + redeem carries the flags/mode;
  inbound voip.ms webhook drops texts for guests whose scope excludes
  external numbers.
- `pbx-api/ucp.py` — portal/app send path refuses texts outside the
  guest's scope (sender or recipient, internal and external branches).
- `pbx-api/e911_ui.py` — "Guest extensions" section + 911 states.
- `pbx-api/app_api.py` — `me/settings` exposes `is_guest` + the four
  rule flags + `guest_sms_mode` (additive; older apps ignore them).
- `pbx-brain/app.py` — the gates: 911 fast-fail for blocked guests,
  outbound deny (dial path + `handle_outbound` for DISA/forwards),
  PSTN ring skip in ring groups + voicemail path for direct rings,
  `send_message_api` texting gates.
- `pbx-brain/agi_server.py` — SIP MESSAGE (Zoiper) texting gates.
- `README.md`, `dev/test-brain-guest.py` (30 checks), `dev/test-guest-api.py`
  (34 checks), `dev/test-guest-agi.py` (9 checks: the AGI internal route
  across all four modes).

No Android app change is needed: enforcement is entirely server-side,
and the app API only gained additive fields.

## Deploy
Normal flow: push to GitHub, then run `pbx-update` on the PBX (it pulls
the repo, restarts pbx-api/pbx-brain; the API's boot migration adds the
columns — all default 0, so every existing login stays a normal,
unrestricted extension). Or copy the files over `/opt/pbx/` manually and
restart `pbx-api` and `pbx-brain`.

## Verify on the PBX (one block)
```sh
cd /opt/pbx   # or the repo checkout
python3 -m py_compile pbx-api/app.py pbx-api/ucp.py pbx-api/e911_ui.py \
  pbx-api/app_api.py pbx-brain/app.py pbx-brain/agi_server.py && echo COMPILE-OK
export PBX_DB=/tmp/guesttest.db && rm -f $PBX_DB
python3 dev/seed-test-db.py
python3 dev/test-brain-guest.py    # 30 checks: 911 / outbound / inbound / sms mode matrix
python3 dev/test-guest-api.py      # 34 checks: migration, convert, invites, modes, e911 page, app gate
python3 dev/test-guest-agi.py      # 9 checks: AGI msg-route across the four modes
sqlite3 /var/lib/pbx/pbx.db "PRAGMA table_info(logins);" | grep guest   # 6 rows on the live DB
```
Then in the panel: Logins shows a Type column; edit any login →
Account type → Guest extension; the E911 page lists it under
Guest extensions.

## Behavior notes / edges
- Guests with outbound ON still need outside minutes (plan or the
  no-plan default) — guest rules are caps on top of plans, never grants.
- A blocked guest in a DID ring group is skipped like a DND member; if
  everyone is skipped, the group's normal no-answer path runs
  (voicemail/forward), same as the existing no-outside-minutes rule.
- Direct (IVR transfer) inbound rings to a blocked guest go to the
  guest's voicemail path without ringing, mirroring the plan check.
- A guest's own "ring my cell" screening leg on internal/group calls is
  unchanged user-pref behavior; PSTN callers never reach it (inbound off
  means the guest isn't rung at all).
- Blocked 911 attempts are logged by pbx-brain
  (`guest <ext> dialed 911: 911 routing is off ...`) and play the
  standard cannot-complete announcement before hanging up.
