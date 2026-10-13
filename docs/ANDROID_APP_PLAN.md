# My Phone for Android: idea, plan and route

<!-- SPDX-License-Identifier: GPL-2.0-or-later -->

Status: **plan only, nothing built yet.** This is the roadmap for the
"3. Android App?" line in `Future_Plans.txt`.

## 1. The idea in one paragraph

A free, open-source (FOSS) Android app that is **My Phone (`/ucp`) made
properly for a phone**. You sign in once and stay signed in for weeks. The
home screen is a messages inbox that looks and feels like Google Messages:
conversation list, chat bubbles, swipe to delete, long-press to select many.
Calls, voicemail and recordings sit one tap away in a bottom bar. Settings
are plain switches. A 6- or 12-digit PIN locks the app. Later, a built-in
SIP phone so the app can make and take calls itself.

## 2. Ground rules

| Rule | What it means in practice |
|---|---|
| **FOSS** | App source in this repo (`android/`), GPL-2.0-or-later like the rest. Only FOSS libraries. **No Google Play Services, no Firebase, no analytics, no ads.** Builds reproducibly so F-Droid can accept it. |
| **"Google look"** | Material Design 3 (Material You): dynamic colours from the wallpaper, light/dark theme, standard Android navigation and gestures. It should feel like a Google app, without Google code. |
| **Cheap** | $0 tools: Android Studio, Kotlin, GitHub Actions to build the APK. Ship through **GitHub Releases + Obtainium** and later **F-Droid** (both free). Google Play costs a one-time $25 and is optional, not needed. |
| **The server stays the boss** | The app never talks to Asterisk directly. It only calls `pbx-api`, and every rule the web panel enforces (own data only, safety lock, plan limits, quotas, rate limits) is enforced by the same server code. |

## 3. Tech choices

| Part | Choice | Why |
|---|---|---|
| Language / UI | **Kotlin + Jetpack Compose + Material 3** | Google's own modern toolkit, all Apache-2.0, gives the "Google APK" look for free. |
| Min Android | Android 8.0 (API 26) | Covers ~97% of phones, has the Keystore features we need. |
| Networking | Retrofit + OkHttp + kotlinx.serialization | Standard, FOSS, small. |
| Local cache | Room (SQLite) | Inbox opens instantly and works offline; syncs in the background. |
| Secure storage | Android Keystore + EncryptedFile / Tink | Sign-in token and PIN hash are encrypted with a hardware-backed key. |
| Background sync | WorkManager (periodic) | Fallback when push isn't set up. |
| Push (new text / missed call) | **UnifiedPush** (e.g. the `ntfy` app or a self-hosted ntfy server) | FOSS replacement for Firebase push; F-Droid friendly. If no distributor is installed, WorkManager polls every 15 min. |
| SIP phone (later) | **Linphone SDK** (GPLv3) *or* **baresip** (BSD) | Both FOSS. Linphone is the most complete on Android; using it makes the combined app GPLv3, which GPL-2.0-*or-later* allows. |

## 4. Server work needed first (pbx-api)

Today `/ucp` is server-rendered HTML with 12-hour in-memory cookie sessions
(a server restart signs everyone out). The app needs a JSON API and a
long-lived, revocable sign-in. This is the most important part and it lives
in this repo.

### 4.1 Long sign-in: device tokens

Reuse the existing `api_keys` table idea (only a sha256 of the token is
stored) with a new `app_devices` table:

```sql
CREATE TABLE IF NOT EXISTS app_devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login_id INTEGER NOT NULL REFERENCES logins(id) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT '',          -- "Pixel 8"
  token_hash TEXT NOT NULL UNIQUE,        -- sha256 of the bearer token
  pwsig TEXT NOT NULL,                    -- password signature; password change = signed out
  push_endpoint TEXT NOT NULL DEFAULT '', -- UnifiedPush URL (optional)
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT,
  expires_at TEXT NOT NULL                -- sliding: now + 60 days on each use
);
```

- **Sliding expiry**: each use pushes `expires_at` out to 60 days, so an app
  you use weekly never asks you to sign in again; an untouched phone is
  signed out after 60 days.
- **Revocable**: My Phone > Settings gets a "Signed-in devices" list with a
  **Sign out** button per device. Changing the panel password signs out all
  devices (same rule as web sessions today).
- **Two ways to sign in**: username + password (rate-limited like the web
  login, audited into `signins`), or **scan a QR code** shown in My Phone >
  Settings > "Add the app" (a one-time code valid for 5 minutes).
- Admin logins should **not** get app tokens at first (user panel only).

### 4.2 JSON endpoints (`/api/v1/me/...`)

All act only on the token's own login, exactly like `/ucp` does. Every list
supports `?before=<id>&limit=` paging; every delete accepts **a list of ids**
so multi-select delete is one request.

| Area | Endpoints |
|---|---|
| Auth | `POST /api/v1/app/login`, `POST /api/v1/app/pair` (QR code), `POST /api/v1/app/logout`, `GET/DELETE /api/v1/me/devices[/{id}]` |
| Overview | `GET /api/v1/me/overview` (status, DND, counts of new voicemail/missed calls/unread texts, plan usage) |
| Messages | `GET /me/threads`, `GET /me/threads/{peer}`, `POST /me/messages` (send), `POST /me/messages/delete` `{ids:[]}`, `POST /me/threads/delete` `{peers:[]}`, `POST /me/threads/{peer}/read`, `GET /me/messages/since/{id}` |
| Calls | `GET /me/calls`, `POST /me/calls/hide` `{ids:[]}` (keeps the 90-day lock) |
| Voicemail | `GET /me/voicemail`, `GET /me/voicemail/{id}/audio`, `POST /me/voicemail/delete` `{ids:[]}`, `.../heard`, `.../move` |
| Recordings | `GET /me/recordings`, `GET .../{id}/audio`, `POST /me/recordings/delete` `{ids:[]}` |
| Block / report | `POST /me/block`, `DELETE /me/block/{number}`, `POST /me/report` |
| Settings | `GET /me/settings`, `PATCH /me/settings` (dnd, forwarding, ring time, record-my-calls with legal ack, text on/off, emails, IVR) |
| Push | `PUT /me/devices/current/push` (save UnifiedPush endpoint). pbx-brain / `voipms_sms.py` POST a tiny "something new" ping; the app then fetches. **No message text goes through the push server.** |

Implementation note: move the logic out of the `/ucp` HTML handlers into
plain functions (e.g. `ucp_core.py`) so the HTML pages and the JSON API call
the same code. That way the safety lock, quotas and permission checks can't
drift apart.

## 5. The app: screens and UX

Bottom navigation (thumb-reachable): **Messages · Calls · Voicemail · More**.
"More" holds Recordings, IVR, Billing (opens the web page) and Settings.

### Messages (the home screen)
- Conversation list like Google Messages: avatar circle with initials,
  name/number, last line, time, bold + dot when unread. Search bar on top.
- **Swipe left** on a conversation = delete (with **Undo** snackbar).
  **Swipe right** = mark read/unread.
- **Long-press** = selection mode: the top bar turns into "3 selected" with
  **Select all**, **Delete**, **Mark read**, **Block**. Tap more items to add.
- Blue **Compose** button (FAB) bottom-right: type an extension or number,
  pick from recent contacts.
- Chat screen: bubbles (mine right / theirs left), day separators, "Sent /
  Failed - tap to retry", text box that grows, send button. Long-press a
  bubble to copy, delete, report. Pull to load older.
- Shows which DID a text came through (`via_did`) when there is more than one.

### Calls
- List with icons for missed / incoming / outgoing; filter chip "Missed".
- Tap = details (time, length, recording if any). Actions: **Call back**
  (Phase 4), **Text**, **Block**, **Report**. Multi-select to hide.

### Voicemail
- Card per message with a play button, seek bar, speed (1x / 1.5x / 2x),
  speaker/earpiece toggle. New ones on top in bold.
- Swipe to delete, long-press to multi-select, "Move to Old/Saved".
- Greeting: record in the app or upload a file.

### Settings
Grouped Material 3 list with **switches** on the right, nothing hidden in
sub-menus unless it has to be:
- *Calls*: Do Not Disturb (also a Quick Settings tile), Forward all calls
  (switch + number), Forward when unanswered, Ring time (picker),
  Record my calls (switch, shows the legal notice once).
- *Texts*: Receive texts, Send texts, Email me texts.
- *Call filtering*: blocked numbers with per-number action.
- *App security*: App lock on/off, change PIN, PIN length 6 or 12, use
  fingerprint/face as a shortcut, lock after (immediately / 1 min / 5 min),
  hide content in recent-apps screen.
- *Notifications*: push on/off, which events.
- *Account*: server address, signed-in devices, change panel password,
  sign out.
- *About*: version, licence, source code link.

### Look and feel
- Material You dynamic colour, dark mode by default following the system.
- Big tap targets (48dp+), one-hand reachable actions, edge-to-edge.
- Snackbars with **Undo** instead of "are you sure?" dialogs for normal
  deletes; a dialog only for bulk deletes over 20 items.
- Haptic tick on long-press and swipe.

## 6. PIN lock (6 or 12 digits)

- On first sign-in the app asks you to **choose a 6-digit or 12-digit PIN**
  (can be changed later; a 12-digit PIN is the "extra safe" option).
- Stored only on the phone: salted **PBKDF2-HMAC-SHA256 (≥210k rounds)** or
  Argon2id hash, inside Keystore-encrypted storage. Never sent to the server.
- Locks on app start and after the chosen idle time. Biometric unlock is an
  optional shortcut; the PIN always works.
- Wrong PINs: growing delay (30 s, 1 min, 5 min ...). After **10 wrong PINs
  the app wipes its token and cache** and you must sign in again (the
  server account is untouched).
- `FLAG_SECURE` on screens so texts don't show in screenshots or the
  recent-apps preview (switchable).
- The device token itself is wrapped by a Keystore key, so copying the app's
  files to another phone doesn't copy the sign-in.

## 7. Built-in SIP phone (later, optional)

Phase 4, only after the rest is solid:
- The app registers as an **extra device of your extension** through the
  Kamailio edge (TLS 5061 + SRTP), so your desk phone and the app ring
  together. The SIP password is fetched once over the API (rotate-secret
  flow) and kept in Keystore.
- Android `ConnectionService` / Telecom integration so calls look like
  normal phone calls (lock-screen answer, Bluetooth, car).
- Incoming calls while the app sleeps: UnifiedPush wake-up from pbx-brain
  before ringing the app's contact (no permanent background connection =
  better battery). Needs a small "push before ring" step in pbx-brain.
- Dial pad, feature codes (`*97` voicemail etc.), call back from history.
- Engine: Linphone SDK (most complete) or baresip (lighter, BSD).

## 8. Route: phases

| Phase | What | Done when |
|---|---|---|
| **0. Server API** | `app_devices` table, login + QR pairing, `/api/v1/me/*` JSON endpoints with bulk delete, shared `ucp_core.py`, devices list in My Phone, tests | All My Phone actions work with `curl` and a Bearer device token |
| **1. App MVP** | Project skeleton (`android/`), sign-in (password + QR), PIN lock, Messages (list, chat, send, swipe/multi-select delete), Settings switches, background polling | You can text and manage settings from the app daily |
| **2. Everything else** | Calls, Voicemail player, Recordings, Block/Report, Quick Settings DND tile, offline cache | The app replaces the web panel on the phone |
| **3. Push + release** | UnifiedPush in pbx-api, notifications with reply-from-notification, GitHub Actions signed APK builds, GitHub Releases + Obtainium, F-Droid submission | Instant notifications; installable by anyone |
| **4. SIP phone** | Linphone/baresip, ConnectionService, push-before-ring in pbx-brain | Calls ring on the app reliably on Wi-Fi and mobile data |

Each phase is shippable on its own. Phase 0 is server-only and is the best
first step: it is useful even without the app (Home Assistant from
`Future_Plans.txt` could use the same endpoints).

## 9. Repo layout

```
android/                 Gradle project (Kotlin, Compose)
  app/src/main/...       ui/ (screens), data/ (API + Room), security/ (PIN, Keystore)
pbx-api/ucp_core.py      shared My Phone logic (HTML + JSON both call it)
pbx-api/app_api.py       /api/v1/app/* and /api/v1/me/* routes
db/schema.sql            + app_devices
docs/ANDROID_APP_PLAN.md this file
.github/workflows/android.yml   build + sign APK on tag
```

## 10. Risks and open questions

- **Security**: a long-lived token on a phone is powerful. Mitigations above
  (Keystore wrap, PIN, per-device revoke, sliding expiry, password change
  kills all devices, sign-in audit). Keep the API behind HTTPS only.
- **Reachability**: the app needs `pbx-api` reachable from outside home
  (the published address from README section 16, or a VPN like WireGuard).
- **Push without Google**: users need a UnifiedPush distributor (ntfy app).
  Polling fallback keeps it working without one, just slower.
- **SIP on Android** is the hardest part (battery, Doze, NAT); that's why it
  is last.
- Open: allow admin logins in the app later? Contacts/address book sync?
  MMS/pictures (depends on VoIP.ms MMS support)?
