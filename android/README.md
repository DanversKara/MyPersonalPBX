# My Phone for Android

<!-- SPDX-License-Identifier: GPL-2.0-or-later -->

FOSS Android app for own-pbx's **My Phone** user panel. Plan and roadmap:
[`docs/ANDROID_APP_PLAN.md`](../docs/ANDROID_APP_PLAN.md). Server API:
[`docs/API.md`](../docs/API.md#my-phone-app-api-android).

Kotlin + Jetpack Compose + Material 3 (Material You colours). No Google Play
Services, no Firebase, no analytics.

## What works (v0.1)

- Sign in with username + password, or a one-time pairing code from
  My Phone > App on the web. Stays signed in 60 days after last use.
- App PIN: 6 or 12 digits (PBKDF2 hash in Keystore-encrypted storage),
  optional fingerprint/face, growing wait after wrong PINs, sign-out after 10.
  Lock immediately / after 1, 5 or 15 minutes away. Screenshots blocked
  (switchable).
- Messages: inbox with search, unread dots, swipe to delete with Undo,
  long-press to multi-select (select all, delete), start a chat, chat
  bubbles, copy/delete messages, block, auto-refresh.
- Calls: all / missed, text back, block.
- Voicemail: new / old, play in the app, swipe or multi-select delete with
  Undo, move to Old.
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
