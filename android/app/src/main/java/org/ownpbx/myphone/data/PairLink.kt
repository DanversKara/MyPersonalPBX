// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.data

import android.net.Uri

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

    private fun cleanCode(c: String?): String? =
        c?.uppercase()?.filter { it.isLetterOrDigit() }?.takeIf { it.length in 6..12 }
}
