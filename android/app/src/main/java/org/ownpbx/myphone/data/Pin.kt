// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.data

import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec

/**
 * 6- or 12-digit app PIN. Only a salted PBKDF2 hash is kept (in the
 * encrypted Store), never the PIN, and it never leaves the phone.
 * Wrong PINs: growing wait; MAX_FAILS wrong in a row signs the app out.
 */
object Pin {
    private const val ITER = 120_000
    const val MAX_FAILS = 10

    private fun derive(pin: String, salt: ByteArray): ByteArray {
        val spec = PBEKeySpec(pin.toCharArray(), salt, ITER, 256)
        return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).encoded
    }

    suspend fun hash(pin: String): String = withContext(Dispatchers.Default) {
        val salt = ByteArray(16).also { SecureRandom().nextBytes(it) }
        val b64 = { b: ByteArray -> Base64.encodeToString(b, Base64.NO_WRAP) }
        "${b64(salt)}:${b64(derive(pin, salt))}"
    }

    suspend fun check(pin: String, stored: String): Boolean = withContext(Dispatchers.Default) {
        val parts = stored.split(":")
        if (parts.size != 2) return@withContext false
        val salt = Base64.decode(parts[0], Base64.NO_WRAP)
        MessageDigest.isEqual(derive(pin, salt), Base64.decode(parts[1], Base64.NO_WRAP))
    }

    /** Seconds to wait after this many wrong PINs in a row. */
    fun delayFor(fails: Int): Long = when {
        fails >= 9 -> 300
        fails >= 7 -> 60
        fails >= 5 -> 30
        else -> 0
    }

    /** Refuse PINs that are trivially guessable (all same digit, straight runs). */
    fun tooSimple(pin: String): Boolean {
        if (pin.toSet().size == 1) return true
        val up = "0123456789012345678901234567890"
        val down = up.reversed()
        return up.contains(pin) || down.contains(pin)
    }
}
