# My Phone for Android

<!-- SPDX-License-Identifier: GPL-2.0-or-later -->

FOSS Android app for own-pbx's **My Phone** user panel. Plan and roadmap:
[`docs/ANDROID_APP_PLAN.md`](../docs/ANDROID_APP_PLAN.md). Server API:
[`docs/API.md`](../docs/API.md#my-phone-app-api-android).

Kotlin + Jetpack Compose + Material 3 (Material You colours). No Google Play
Services, no Firebase, no analytics.

## What works (v0.1)

- Sign in with a one-time pairing code from My Phone > App on the web (the
  app never sees your panel password): tap **Scan QR code**, or type the code.
  The QR code holds `myphone://pair?s=<server>&c=<code>`, so the phone's own
  camera app can open it too. Stays signed in 60 days after last use.
- The server box starts with `myphone.defaultServer` from `gradle.properties`
  (override with `-Pmyphone.defaultServer=...`).
- App PIN: 6 or 12 digits (PBKDF2 hash in Keystore-encrypted storage),
  optional fingerprint/face, growing wait after wrong PINs, sign-out after 10.
  Lock immediately / after 1, 5 or 15 minutes away. Screenshots blocked
  (switchable).
- Messages: inbox with search, unread dots, swipe to delete with Undo,
  long-press to multi-select (select all, delete), start a chat, chat
  bubbles, copy/delete messages, block, auto-refresh.
- Calls: all / missed / hidden. Swipe or long-press to hide calls (they
  stay under Hidden until the PBX's 90-day purge); the missed-call badge
  clears when you open Calls. Text back, block.
- Voicemail: new / old, tap to play with a seek bar and 1x/1.5x/2x speed
  (audio is downloaded over the app's own connection, then played), text
  back, block, move between New and Old, swipe or multi-select delete with
  Undo.
- Notifications for new texts, missed calls and voicemail. Settings >
  Notifications > "Check for new things": **Instant** (default; a foreground
  service long-polls `/api/v1/me/wait`, which answers within ~2 s of anything
  new), every 30 s / 1 min / 5 min (same service, timed), or every 15 min
  (WorkManager only, no ongoing "Connected" notification). The 15-minute job
  always runs as a fallback; the service restarts after reboot. Tapping a
  notification opens that tab or chat; things already seen in the app aren't
  notified again. "Let it run in the background" asks Android to exempt the
  app from battery optimisation.
- Calls (outgoing): dial pad on the Calls tab, Call from call history, Call
  back from voicemail, call button in a chat. Built-in phone = Linphone SDK
  (voice-only build, GPL-3.0). The app doesn't register: each call goes to
  the PBX through the edge like a remote Zoiper (TLS 5061, SRTP SDES,
  ulaw/alaw/G.722) with the extension's phone login from `/api/v1/me/sip`,
  so other phones on the extension are unaffected. Mute, speaker, keypad
  (DTMF), hang up; an "On a call" notification keeps the call going in the
  background.
- Calls (incoming): the app signs in (registers) on the extension's own app
  line, `<sip_username>-app` (generated for every extension in pjsip.conf,
  same password and caller ID), never on the main line, so the desk phone /
  Zoiper stay signed in. pbx-brain rings the app line together with the main
  line when it's online; first answer wins. The stay-connected service keeps
  the sign-in alive; ringing uses the phone's ringtone, a full-screen
  notification with Answer / Decline, and the call screen shows over the PIN
  lock. Settings > Calling > Incoming calls turns it off. A typed-in main-line
  login is used for outgoing calls only.
- Phone login: Settings > Calling > Phone login (scan the QR from My Phone >
  App > "Show phone login QR", `myphone://sip?s=&u=&p=`, or type it). A
  camera-opened phone-login link asks before it's used.
- The open tab, chat, Settings dialog and a half-typed phone login survive
  the PIN lock (e.g. switching to a password manager); the app's own QR
  scanner doesn't count as leaving the app.
- "Lock the app after N minutes" survives Android closing the app in the
  background (the time is stored, not kept in memory); a reboot always locks.
- Settings with switches: DND, forwarding, ring time, record my calls,
  texts in/out, email, blocked numbers, PIN, password, signed-in phones,
  sign out.

## Build

Get the APK from GitHub: **Actions > Android app > latest run > Artifacts**.

Or locally with Android Studio (open the `android/` folder), or:

```bash
gradle -p android assembleDebug    # Gradle 8.9+, JDK 17, Android SDK 34
```

## Next

Optional UnifiedPush (ntfy) instead of the stay-connected service, Quick Settings DND tile, recordings
screen, release signing + F-Droid, then the built-in SIP phone.
