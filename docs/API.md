# REST API (v1)

<!-- SPDX-License-Identifier: GPL-2.0-or-later -->

## Overview

Extensions are API entities — "apis are the ext". The v1 REST API manages
them programmatically with per-login Bearer tokens.

Issue a key in the panel (**API Keys** tab) or via the API itself. The raw
key is shown **once** at creation; only its sha256 is stored.

```bash
KEY="pbx_..."
AUTH="Authorization: Bearer $KEY"

# list extensions
curl -s -H "$AUTH" http://127.0.0.1:8001/api/v1/extensions

# create one (sip_secret + login password auto-generated, returned once)
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"username":"alice","exten":"8802","display_name":"Alice"}' \
  http://127.0.0.1:8001/api/v1/extensions

# read / patch / delete
curl -s -H "$AUTH" http://127.0.0.1:8001/api/v1/extensions/8802
curl -s -H "$AUTH" -H 'Content-Type: application/json' -X PATCH \
  -d '{"display_name":"Alice A.","user_record":true}' \
  http://127.0.0.1:8001/api/v1/extensions/8802
curl -s -H "$AUTH" -X DELETE http://127.0.0.1:8001/api/v1/extensions/8802

# rotate the SIP secret (returned once) / reset the login password
curl -s -H "$AUTH" -X POST http://127.0.0.1:8001/api/v1/extensions/8802/rotate-secret
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"password":"new-strong-password"}' \
  http://127.0.0.1:8001/api/v1/extensions/8802/reset-password

# keys: list / issue / revoke (admin only)
curl -s -H "$AUTH" http://127.0.0.1:8001/api/v1/api-keys
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"username":"alice","name":"provisioning"}' \
  http://127.0.0.1:8001/api/v1/api-keys
curl -s -H "$AUTH" -X DELETE http://127.0.0.1:8001/api/v1/api-keys/3
```

Rules:

- Admin keys: full access. User keys: can only read their own extension
  (`GET /api/v1/extensions`, `GET /api/v1/extensions/{own}`, `GET /api/v1/me`).
- `sip_secret` / login `password` are returned **once** at create/rotate
  time and never appear in GET responses.
- Mutations regenerate `pjsip.conf` and reload PJSIP through a root-owned
  wrapper (`/opt/pbx/bin/pbx-apply-config.sh`, sudoers-scoped to the `pbx`
  user). The response reports `"applied": true/false` — on `false`, retry
  with `POST /system/reload`.
- PATCH accepts `display_name`, `vm_email`, `record_admin`, `user_record`,
  `max_messages`, `enabled`. Identity fields (username, exten,
  sip_username, role) are set at creation; delete + recreate to change them.

### Live status (v1)

The dashboard is a thin consumer of these — same JSON any external tool
can use. The brain is the source of truth for calls (ARI events); Asterisk
for registrations. User keys only see their own device/calls.

```bash
# registered SIP devices (deduped per real device)
curl -s -H "$AUTH" http://127.0.0.1:8001/api/v1/devices

# live calls, straight from the brain
curl -s -H "$AUTH" http://127.0.0.1:8001/api/v1/calls/live

# panel sign-in history (admin)
curl -s -H "$AUTH" 'http://127.0.0.1:8001/api/v1/signins?limit=50&failed_only=1'
```

Panel sign-ins (success and failure) are audited to the `signins` table.
The dashboard (`/`) renders devices, live calls and recent sign-ins from
these endpoints and refreshes every 10s.

### Safety: kill switch + safety lock

```bash
# status
curl -s -H "$AUTH" http://127.0.0.1:8001/api/v1/safety

# status returns {"kill_switch":..,"safety_lock":..,"lock_mode":"off"|"full"|"admin"}

# safety lock, full (default): routine mutations on the admin panel, the API
# AND My Phone (UCP) are rejected with 403
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"locked":true,"mode":"full"}' http://127.0.0.1:8001/api/v1/safety/lock

# safety lock, admin only: admin panel + API are frozen, but users can keep
# using My Phone (send texts, change their settings, voicemail, ...)
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"locked":true,"mode":"admin"}' http://127.0.0.1:8001/api/v1/safety/lock

# release either lock
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"locked":false}' http://127.0.0.1:8001/api/v1/safety/lock

# kill switch: hangs up all active calls, renders an empty pjsip.conf
# (no registrations, no trunk) and reloads. Bypasses the safety lock.
# The brain also hangs up any new channel while the switch is on.
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"engaged":true}' http://127.0.0.1:8001/api/v1/safety/kill-switch
# release when the emergency is over (config re-renders, trunk re-registers)
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"engaged":false}' http://127.0.0.1:8001/api/v1/safety/kill-switch
```

Both are also on the dashboard: a status banner with Kill switch,
Lock admin / Lock everything, and Unlock buttons.


## My Phone app API (Android)

Long sign-in for the Android app; see `pbx-api/app_api.py` and
`docs/ANDROID_APP_PLAN.md`. Device tokens (`pbxd_...`) only work for the
signed-in user's own My Phone data, never for admin pages.

```bash
# sign in (or: POST /api/v1/app/pair {"code":"ABCD2345"} with a code from My Phone > App)
curl -s -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"...","device_name":"Pixel 8"}' \
  https://pbx.example/api/v1/app/login        # -> {"token":"pbxd_...","ttl_days":60,...}
AUTH="Authorization: Bearer pbxd_..."

curl -s -H "$AUTH" https://pbx.example/api/v1/me/overview
curl -s -H "$AUTH" https://pbx.example/api/v1/me/threads
curl -s -H "$AUTH" https://pbx.example/api/v1/me/threads/8801
curl -s -H "$AUTH" -d to=8801 -d body=hello https://pbx.example/api/v1/me/messages/send
curl -s -H "$AUTH" -d ids=12 -d ids=13 -d convs=8801 https://pbx.example/api/v1/me/messages/delete
```

| Reads (GET) | Changes (POST, form fields like the web forms) |
|---|---|
| `/me/overview`, `/me/threads`, `/me/threads/{peer}?before=&limit=`, `/me/messages/since?after=` | `/me/messages/send`, `/me/messages/delete` (`ids`, `convs`, or `mode=all`) |
| `/me/calls?view=all\|missed\|hidden`, `/me/voicemail?folder=Old`, `/me/voicemail/{id}/audio` | `/me/calls/hide` (`ids`; `unhide=1` to show again), `/me/calls/seen`, `/me/voicemail/delete`, `/me/voicemail/{id}/heard`, `/me/voicemail/{id}/move` |
| `/me/recordings`, `/me/recordings/{id}/audio` | `/me/recordings/delete` |
| `/me/settings`, `/me/devices` | `/me/settings/calls`, `/me/settings/prefs`, `/me/settings/messaging`, `/me/settings/password`, `/me/dnd`, `/me/block`, `/me/report` |

Changes answer `{"ok": bool, "code": "<key>", "message": "<text>"}` (HTTP 400
on a refused change, 423 when the safety lock is on). Also:
`DELETE /me/devices/{id}`, `PUT /me/push {"endpoint":"https://..."}`
(UnifiedPush), `POST /api/v1/app/logout`. Tokens expire 60 days after last
use; a password change signs out every other phone.

Calls can't be deleted by users, only hidden (per user, `call_hidden`): hidden
calls drop out of `all`/`missed` and are listed under `view=hidden` until the
90-day call-history purge removes them. `/me/overview` returns `missed_new`,
the missed calls this phone hasn't seen and the user hasn't hidden; `POST
/me/calls/seen` (the app calls it when Calls is opened) resets it to 0. The
Android app signs in with pairing codes only; `/app/login` stays for other
API clients. `GET /me/wait?msg=&call=&vm=&timeout=50` is a long poll for the app's
Instant notifications: it answers as soon as there is a text, unhidden missed
call or Inbox voicemail newer than the ids given (checked every 2 s), or after
`timeout` seconds (max 55, under Cloudflare's 100 s limit), with the newest
ids and `changed`. My Phone > App shows each code as a QR code (needs the `segno`
package; deploy.sh installs it) holding `myphone://pair?s=<server>&c=<code>`.
