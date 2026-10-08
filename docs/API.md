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

# safety lock: when ON, routine mutations (API + panel) are rejected with 403
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"locked":true}' http://127.0.0.1:8001/api/v1/safety/lock

# kill switch: hangs up all active calls, renders an empty pjsip.conf
# (no registrations, no trunk) and reloads. Bypasses the safety lock.
# The brain also hangs up any new channel while the switch is on.
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"engaged":true}' http://127.0.0.1:8001/api/v1/safety/kill-switch
# release when the emergency is over (config re-renders, trunk re-registers)
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"engaged":false}' http://127.0.0.1:8001/api/v1/safety/kill-switch
```

Both are also on the dashboard: a status banner with Kill switch and
Lock/Unlock buttons.

