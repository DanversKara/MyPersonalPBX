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

## Files
- `db/schema.sql` — five guest columns on `logins` and `invites`
  (fresh installs).
- `pbx-api/app.py` — idempotent migration for existing DBs; Logins list
  Type column + editor account-type/rules; Invites generator picker +
  redeem carries the flags; inbound voip.ms webhook drops texts for
  guests with texting off.
- `pbx-api/ucp.py` — portal/app send path refuses texts for blocked
  guests (sender or recipient).
- `pbx-api/e911_ui.py` — "Guest extensions" section + 911 states.
- `pbx-api/app_api.py` — `me/settings` exposes `is_guest` + the four
  rule flags (additive; older apps ignore them).
- `pbx-brain/app.py` — the gates: 911 fast-fail for blocked guests,
  outbound deny (dial path + `handle_outbound` for DISA/forwards),
  PSTN ring skip in ring groups + voicemail path for direct rings,
  `send_message_api` texting gates.
- `pbx-brain/agi_server.py` — SIP MESSAGE (Zoiper) texting gates.
- `README.md`, `dev/test-brain-guest.py`, `dev/test-guest-api.py`.

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
python3 dev/test-brain-guest.py    # 21 checks: 911 / outbound / inbound / sms matrix
python3 dev/test-guest-api.py      # 20 checks: migration, convert, invites, e911 page, app gate
sqlite3 /var/lib/pbx/pbx.db "PRAGMA table_info(logins);" | grep guest   # 5 rows on the live DB
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
