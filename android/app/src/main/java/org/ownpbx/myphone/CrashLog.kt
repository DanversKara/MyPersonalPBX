// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone

import android.content.Context
import android.os.Build
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Saves the last crash to a file so the next launch can show it (no adb
 *  needed to report a bug). Only the stack trace, app version and phone
 *  model are written; it stays on the phone until the user copies it. */
object CrashLog {
    private var installed = false
    private fun file(ctx: Context) = File(ctx.filesDir, "last_crash.txt")

    fun install(ctx: Context) {
        if (installed) return
        installed = true
        val app = ctx.applicationContext
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { t, e ->
            try {
                val sw = StringWriter()
                e.printStackTrace(PrintWriter(sw))
                file(app).writeText(
                    "My Phone ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})\n" +
                    "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}), ${Build.MANUFACTURER} ${Build.MODEL}\n" +
                    "${SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())}, thread ${t.name}\n\n" +
                    sw.toString().take(20_000))
            } catch (_: Throwable) {}
            previous?.uncaughtException(t, e)
        }
    }

    fun read(ctx: Context): String? = file(ctx).takeIf { it.exists() }?.readText()
    fun clear(ctx: Context) { file(ctx).delete() }
}
