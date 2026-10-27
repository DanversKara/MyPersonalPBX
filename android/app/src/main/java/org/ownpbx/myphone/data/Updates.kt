// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.data

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import java.io.File
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * Self-update: ask GitHub whether a newer My Phone release exists, and if so download its .apk and
 * hand it to the system installer.
 *
 * The repo's releases are all marked prerelease, so /releases/latest never returns one — the list
 * endpoint is used and the newest android-v* tag wins. Everything here fails silently: being
 * offline (or GitHub being unreachable) must never disturb the phone features.
 */
object Updates {
    data class Offer(val version: String, val apkUrl: String)

    private const val RELEASES_URL =
        "https://api.github.com/repos/DanversKara/MyPersonalPBX/releases?per_page=10"
    private const val CHECK_INTERVAL_MS = 60 * 60_000L

    /** The running app's version, from the build (never hardcoded). */
    fun installedVersion(): String = org.ownpbx.myphone.BuildConfig.VERSION_NAME

    /** Numeric component-wise compare: >0 when [a] is newer than [b]. */
    fun compareVersions(a: String, b: String): Int {
        val pa = a.split('.').map { it.toIntOrNull() ?: 0 }
        val pb = b.split('.').map { it.toIntOrNull() ?: 0 }
        for (i in 0 until maxOf(pa.size, pb.size)) {
            val d = (pa.getOrNull(i) ?: 0) - (pb.getOrNull(i) ?: 0)
            if (d != 0) return d
        }
        return 0
    }

    /** "android-v0.7.11" -> "0.7.11"; null for tags that aren't app releases. */
    private fun versionOfTag(tag: String): String? {
        if (!tag.startsWith("android-v")) return null
        val v = tag.removePrefix("android-v").substringBefore('-')
        return v.takeIf { it.isNotBlank() && it.all { ch -> ch.isDigit() || ch == '.' } }
    }

    /**
     * The offer already known (checked earlier, still newer than this app). A version the user
     * tapped "Later" on is NOT filtered out: the dialog comes back the next time the app is opened,
     * so an update can't get buried by one stray tap — it only goes away by being installed.
     */
    fun pendingOffer(store: Store): Offer? {
        val v = store.updateAvailable
        if (v.isBlank()) return null
        if (compareVersions(v, installedVersion()) <= 0) return null
        val url = store.updateApkUrl
        return Offer(v, url).takeIf { url.isNotBlank() }
    }

    /**
     * True when this install came from Google Play (installer package com.android.vending). An
     * unknown installer, or a lookup failure, means "not Play": sideloaded installs keep the GitHub
     * check.
     */
    fun isPlayInstall(ctx: Context): Boolean {
        return try {
            val installer =
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    ctx.packageManager.getInstallSourceInfo(ctx.packageName).installingPackageName
                } else {
                    @Suppress("DEPRECATION")
                    ctx.packageManager.getInstallerPackageName(ctx.packageName)
                }
            installer == "com.android.vending"
        } catch (e: Exception) {
            false
        }
    }

    /**
     * Check GitHub (at most once a day unless [force]), remember the result, and return the current
     * offer if any. Network and parse errors are swallowed by design.
     */
    suspend fun refresh(ctx: Context, force: Boolean = false): Offer? {
        // Google Play never self-updates: the Play build has no updater at all, and a
        // GitHub-built APK that was installed through Play (or restored by it) is left
        // to Play too, since a GitHub .apk can't install over a Play-signed app.
        if (!org.ownpbx.myphone.BuildConfig.SELF_UPDATE || isPlayInstall(ctx)) return null
        val store = Store(ctx)
        if (!store.updateCheck) return null
        val now = System.currentTimeMillis()
        if (!force && now - store.updateLastCheckAt < CHECK_INTERVAL_MS) return pendingOffer(store)
        store.updateLastCheckAt = now
        val found =
            try {
                fetchNewest()
            } catch (e: Exception) {
                null
            }
        if (found != null && compareVersions(found.version, installedVersion()) > 0) {
            store.updateAvailable = found.version
            store.updateApkUrl = found.apkUrl
        } else if (found != null) {
            store.updateAvailable = ""
            store.updateApkUrl = ""
        }
        return pendingOffer(store)
    }

    private suspend fun fetchNewest(): Offer? =
        withContext(Dispatchers.IO) {
            val req =
                Request.Builder()
                    .url(RELEASES_URL)
                    .header("Accept", "application/vnd.github+json")
                    .build()
            Api.http.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return@withContext null
                val body = r.body?.string() ?: return@withContext null
                val arr = Api.json.parseToJsonElement(body) as? JsonArray ?: return@withContext null
                var best: Offer? = null
                for (el in arr) {
                    val rel = el as? JsonObject ?: continue
                    if (rel["draft"]?.jsonPrimitive?.booleanOrNull == true) continue
                    val v =
                        versionOfTag(rel["tag_name"]?.jsonPrimitive?.contentOrNull ?: "")
                            ?: continue
                    val assets = rel["assets"] as? JsonArray ?: continue
                    val apk =
                        assets.filterIsInstance<JsonObject>().firstOrNull {
                            it["name"]?.jsonPrimitive?.contentOrNull?.endsWith(".apk") == true
                        } ?: continue
                    val url = apk["browser_download_url"]?.jsonPrimitive?.contentOrNull ?: continue
                    if (best == null || compareVersions(v, best.version) > 0) best = Offer(v, url)
                }
                best
            }
        }

    /** Where a downloaded update waits for the installer (the FileProvider's cache path). */
    fun apkFile(ctx: Context, version: String): File =
        File(File(ctx.cacheDir, "updates"), "my-phone-$version.apk")

    /** Download the .apk for [offer]; null on any failure. Safe to call again (it re-downloads). */
    suspend fun download(ctx: Context, offer: Offer): File? =
        withContext(Dispatchers.IO) {
            try {
                val http: OkHttpClient =
                    Api.http
                        .newBuilder()
                        .readTimeout(10, TimeUnit.MINUTES)
                        .callTimeout(15, TimeUnit.MINUTES)
                        .build()
                val dest = apkFile(ctx, offer.version)
                dest.parentFile?.mkdirs()
                val tmp = File(dest.path + ".part")
                http.newCall(Request.Builder().url(offer.apkUrl).build()).execute().use { r ->
                    if (!r.isSuccessful) return@withContext null
                    val body = r.body ?: return@withContext null
                    body.byteStream().use { inp -> tmp.outputStream().use { inp.copyTo(it) } }
                }
                if (!tmp.renameTo(dest)) {
                    tmp.copyTo(dest, overwrite = true)
                    tmp.delete()
                }
                dest.takeIf { it.length() > 0 }
            } catch (e: Exception) {
                null
            }
        }

    /** Android 8+: the user must have allowed installs from this app. */
    fun canInstall(ctx: Context): Boolean =
        Build.VERSION.SDK_INT < 26 || ctx.packageManager.canRequestPackageInstalls()

    /** Send the user to the system switch for allowing installs from My Phone. */
    fun openInstallSettings(ctx: Context) {
        try {
            ctx.startActivity(
                Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:" + ctx.packageName),
                    )
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
        } catch (e: Exception) {}
    }

    /** Hand a downloaded .apk to the system installer. */
    fun install(ctx: Context, file: File) {
        val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".fileprovider", file)
        ctx.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        )
    }
}
