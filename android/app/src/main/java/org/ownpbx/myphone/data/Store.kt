// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Everything the app keeps, encrypted with a hardware-backed Android Keystore key. Excluded from
 * backups (see data_extraction_rules.xml), so copying the app's files to another phone doesn't copy
 * the sign-in.
 */
class Store(context: Context) {
    private val cacheDir = context.applicationContext.cacheDir
    private val prefs: SharedPreferences =
        EncryptedSharedPreferences.create(
            context,
            "myphone_secure",
            MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )

    var server: String
        get() =
            prefs.getString("server", null)?.takeIf { it.isNotBlank() }
                ?: org.ownpbx.myphone.BuildConfig.DEFAULT_SERVER
        set(v) = prefs.edit().putString("server", v).apply()

    /**
     * "Sign in with my browser" in progress: the state sent, the server it went to and when. Only a
     * link carrying this state connects by itself.
     */
    var browserState: String
        get() = prefs.getString("browser_state", "") ?: ""
        set(v) = prefs.edit().putString("browser_state", v).apply()

    var browserServer: String
        get() = prefs.getString("browser_server", "") ?: ""
        set(v) = prefs.edit().putString("browser_server", v).apply()

    var browserAt: Long
        get() = prefs.getLong("browser_at", 0)
        set(v) = prefs.edit().putLong("browser_at", v).apply()

    var token: String?
        get() = prefs.getString("token", null)
        set(v) = prefs.edit().putString("token", v).apply()

    var username: String
        get() = prefs.getString("username", "") ?: ""
        set(v) = prefs.edit().putString("username", v).apply()

    var exten: String
        get() = prefs.getString("exten", "") ?: ""
        set(v) = prefs.edit().putString("exten", v).apply()

    // PIN lock
    var pinHash: String?
        get() = prefs.getString("pin_hash", null)
        set(v) = prefs.edit().putString("pin_hash", v).apply()

    var pinLength: Int
        get() = prefs.getInt("pin_len", 6)
        set(v) = prefs.edit().putInt("pin_len", v).apply()

    var pinFails: Int
        get() = prefs.getInt("pin_fails", 0)
        set(v) = prefs.edit().putInt("pin_fails", v).apply()

    var pinLockedUntil: Long
        get() = prefs.getLong("pin_until", 0)
        set(v) = prefs.edit().putLong("pin_until", v).apply()

    var lockAfterSec: Int
        get() = prefs.getInt("lock_after", 0)
        set(v) = prefs.edit().putInt("lock_after", v).apply()

    var biometric: Boolean
        get() = prefs.getBoolean("biometric", false)
        set(v) = prefs.edit().putBoolean("biometric", v).apply()

    var secureScreen: Boolean
        get() = prefs.getBoolean("secure_screen", true)
        set(v) = prefs.edit().putBoolean("secure_screen", v).apply()

    // Notifications: on/off, whether texts show their words, and the newest
    // item already notified (or seen in the app) for each kind. 0 = not set
    // yet; the first check only records where things stand.
    var notifyOn: Boolean
        get() = prefs.getBoolean("notify_on", true)
        set(v) = prefs.edit().putBoolean("notify_on", v).apply()

    var notifyShowText: Boolean
        get() = prefs.getBoolean("notify_text", true)
        set(v) = prefs.edit().putBoolean("notify_text", v).apply()

    /**
     * How often to look for new texts/calls/voicemail: 0 = instant (stay connected), else seconds.
     * 900+ uses only the battery-friendly background job (no "Connected" notification).
     */
    var checkEvery: Int
        get() = prefs.getInt("check_every", 0)
        set(v) = prefs.edit().putInt("check_every", v).apply()

    /**
     * Calling: optional phone login typed in by the user. Blank = use what the PBX gives (/me/sip).
     * Server is "host" or "host:port" (TLS).
     */
    var sipServerOverride: String
        get() = prefs.getString("sip_server", "") ?: ""
        set(v) = prefs.edit().putString("sip_server", v).apply()

    var sipUserOverride: String
        get() = prefs.getString("sip_user", "") ?: ""
        set(v) = prefs.edit().putString("sip_user", v).apply()

    var sipPassOverride: String
        get() = prefs.getString("sip_pass", "") ?: ""
        set(v) = prefs.edit().putString("sip_pass", v).apply()

    /**
     * Push-to-talk flags, cached from the last /me/settings load (Settings screen and LiveService
     * refresh them). Phone reads these on the incoming-call path, where it runs on the Linphone
     * thread and can't do network I/O; defaults keep PTT off until the PBX says otherwise.
     */
    var pttAllowed: Boolean
        get() = prefs.getBoolean("ptt_allowed", false)
        set(v) = prefs.edit().putBoolean("ptt_allowed", v).apply()

    var userPtt: Boolean
        get() = prefs.getBoolean("user_ptt", false)
        set(v) = prefs.edit().putBoolean("user_ptt", v).apply()

    /** My effective PTT state: the admin allows it AND I turned it on. */
    val pttOn: Boolean
        get() = pttAllowed && userPtt

    /** Ring this phone for calls to the extension (the app line). */
    var incomingCalls: Boolean
        get() = prefs.getBoolean("incoming_calls", true)
        set(v) = prefs.edit().putBoolean("incoming_calls", v).apply()

    var notifyAsked: Boolean
        get() = prefs.getBoolean("notify_asked", false)
        set(v) = prefs.edit().putBoolean("notify_asked", v).apply()

    /**
     * SystemClock.elapsedRealtime() when the user left the app unlocked (0 = locked / not set).
     * Kept here, not in memory, so "lock after 15 minutes" still holds when Android closes the app
     * in between. elapsedRealtime restarts at boot, so a reboot always locks.
     */
    var leftUnlockedAt: Long
        get() = prefs.getLong("left_unlocked_at", 0)
        set(v) = prefs.edit().putLong("left_unlocked_at", v).apply()

    var lastCheckAt: Long
        get() = prefs.getLong("last_check_at", 0)
        set(v) = prefs.edit().putLong("last_check_at", v).apply()

    var lastCheckInfo: String
        get() = prefs.getString("last_check_info", "") ?: ""
        set(v) = prefs.edit().putString("last_check_info", v).apply()

    /** Newest text when notifications first started: older ones never count. */
    var notifBaseMsgId: Long
        get() = prefs.getLong("notif_base_msg", 0)
        set(v) = prefs.edit().putLong("notif_base_msg", v).apply()

    var notifMsgId: Long
        get() = prefs.getLong("notif_msg", 0)
        set(v) = prefs.edit().putLong("notif_msg", v).apply()

    var notifCallId: Long
        get() = prefs.getLong("notif_call", 0)
        set(v) = prefs.edit().putLong("notif_call", v).apply()

    /**
     * Missed calls the app already notified itself ("number@epochMs;..."), so the PBX's copy of the
     * same call doesn't ring a second time.
     */
    var localMissed: String
        get() = prefs.getString("local_missed", "") ?: ""
        set(v) = prefs.edit().putString("local_missed", v).apply()

    var notifVmId: Long
        get() = prefs.getLong("notif_vm", 0)
        set(v) = prefs.edit().putLong("notif_vm", v).apply()

    var lastSeenMessageId: Long
        get() = prefs.getLong("last_seen_msg", 0)
        set(v) = prefs.edit().putLong("last_seen_msg", v).apply()

    /** Newest message id read in each conversation (for unread dots). */
    fun seenFor(peer: String): Long = prefs.getLong("seen_$peer", 0)

    fun markSeen(peer: String, id: Long) {
        if (id > seenFor(peer)) prefs.edit().putLong("seen_$peer", id).apply()
    }

    fun api() = Api(server, token)

    /** Sign out locally: forget the token and the PIN (server row is removed separately). */
    fun wipe() {
        val keepServer = server
        prefs.edit().clear().apply()
        server = keepServer
        // Downloaded voicemail audio belongs to the signed-in user.
        java.io.File(cacheDir, "voicemail").deleteRecursively()
    }
}
