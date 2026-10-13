// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Everything the app keeps, encrypted with a hardware-backed Android Keystore
 * key. Excluded from backups (see data_extraction_rules.xml), so copying the
 * app's files to another phone doesn't copy the sign-in.
 */
class Store(context: Context) {
    private val cacheDir = context.applicationContext.cacheDir
    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        context, "myphone_secure",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var server: String
        get() = prefs.getString("server", "") ?: ""
        set(v) = prefs.edit().putString("server", v).apply()
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
