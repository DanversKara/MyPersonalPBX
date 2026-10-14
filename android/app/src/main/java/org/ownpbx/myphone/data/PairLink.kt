// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.data

import android.net.Uri

/** Phone login from My Phone > App > Show phone login QR. */
data class SipLogin(val server: String, val user: String, val pass: String)

/** Server + pairing code from a pairing QR code / link. */
data class PairInfo(val server: String?, val code: String)

object PairLink {
    /** Accepts myphone://pair?s=<server>&c=<code> (what My Phone > App shows),
     *  or just a bare code. Returns null for anything else. */
    fun parse(text: String?): PairInfo? {
        val t = text?.trim().orEmpty()
        if (t.isEmpty()) return null
        if (t.startsWith("myphone:", ignoreCase = true)) {
            val u = try { Uri.parse(t) } catch (e: Exception) { return null }
            if (!u.host.equals("pair", ignoreCase = true)) return null
            val code = cleanCode(u.getQueryParameter("c")) ?: return null
            val server = u.getQueryParameter("s")?.trim()
                ?.takeIf { it.startsWith("https://") || it.startsWith("http://") }
            return PairInfo(server, code)
        }
        return cleanCode(t)?.let { PairInfo(null, it) }
    }

    /** myphone://sip?s=<host:port>&u=<user>&p=<password>, else null. */
    fun parseSip(text: String?): SipLogin? {
        val t = text?.trim().orEmpty()
        if (!t.startsWith("myphone:", ignoreCase = true)) return null
        val u = try { Uri.parse(t) } catch (e: Exception) { return null }
        if (!u.host.equals("sip", ignoreCase = true)) return null
        val s = u.getQueryParameter("s")?.trim().orEmpty()
        val user = u.getQueryParameter("u")?.trim().orEmpty()
        val pass = u.getQueryParameter("p").orEmpty()
        return if (s.isNotEmpty() && user.isNotEmpty() && pass.isNotEmpty()) SipLogin(s, user, pass) else null
    }

    private fun cleanCode(c: String?): String? =
        c?.uppercase()?.filter { it.isLetterOrDigit() }?.takeIf { it.length in 6..12 }
}
