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

Push notifications (UnifiedPush), Quick Settings DND tile, recordings
screen, release signing + F-Droid, then the built-in SIP phone.
