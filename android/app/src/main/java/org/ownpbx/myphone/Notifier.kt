// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit
import org.ownpbx.myphone.data.ApiException
import org.ownpbx.myphone.data.Store
import org.ownpbx.myphone.ui.prettyNumber

/**
 * Notifications for new texts, missed calls and voicemail.
 *
 * A background job asks the PBX what's new (every 15 minutes, Android's minimum for background
 * work) and posts a notification for anything newer than what was already notified or seen in the
 * app. Leaving the app counts as "seen", so nothing you already looked at pops up later.
 */
object Notifier {
    const val CH_MESSAGES = "messages"
    const val CH_CALLS = "missed_calls"
    const val CH_VOICEMAIL = "voicemail"
    const val CH_UPDATES = "updates"
    const val CH_FAX = "faxes"
    const val EXTRA_OPEN = "org.ownpbx.myphone.OPEN" // "messages" | "calls" | "voicemail" | "faxes"
    const val EXTRA_PEER = "org.ownpbx.myphone.PEER"
    private const val ID_CALLS = 2
    private const val ID_VOICEMAIL = 3
    private const val ID_UPDATE = 4
    private const val ID_FAX = 5
    private const val ID_TEST = 9
    private const val ID_MSG_BASE = 1000
    private const val GROUP_MESSAGES = "messages"
    private const val WORK_PERIODIC = "pbx-check"

    /**
     * True while the app is on screen: the job then only records what's new (the user can see it)
     * instead of notifying.
     */
    @Volatile var appVisible = false

    fun createChannels(ctx: Context) {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannels(
            listOf(
                NotificationChannel(CH_MESSAGES, "Texts", NotificationManager.IMPORTANCE_HIGH)
                    .apply { description = "New text messages" },
                NotificationChannel(
                        CH_CALLS,
                        "Missed calls",
                        NotificationManager.IMPORTANCE_DEFAULT,
                    )
                    .apply { description = "Calls you didn't answer" },
                NotificationChannel(
                        CH_VOICEMAIL,
                        "Voicemail",
                        NotificationManager.IMPORTANCE_DEFAULT,
                    )
                    .apply { description = "New voicemail" },
                NotificationChannel(
                        CH_UPDATES,
                        "App updates",
                        NotificationManager.IMPORTANCE_DEFAULT,
                    )
                    .apply { description = "A newer My Phone release is available" },
                NotificationChannel(CH_FAX, "Faxes", NotificationManager.IMPORTANCE_DEFAULT)
                    .apply { description = "Faxes received on your numbers" },
            )
        )
    }

    /** Start (or keep) the periodic check. Safe to call often. */
    fun schedule(ctx: Context) {
        val store = Store(ctx)
        val wm = WorkManager.getInstance(ctx)
        // Keep the periodic job while incoming calls are on even if
        // notifications are off: CheckWorker is also LiveService's watchdog.
        if (store.token == null || (!store.notifyOn && !store.incomingCalls)) {
            wm.cancelUniqueWork(WORK_PERIODIC)
            return
        }
        createChannels(ctx)
        val req =
            PeriodicWorkRequestBuilder<CheckWorker>(15, TimeUnit.MINUTES)
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
                )
                .build()
        wm.enqueueUniquePeriodicWork(WORK_PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req)
    }

    /**
     * The app showed the user items up to these ids: don't notify them later. (Only moves forward,
     * and only once a first check has set a baseline.)
     */
    fun seen(store: Store, msgId: Long = 0, missedCallId: Long = 0, vmId: Long = 0, faxId: Long = 0) {
        // Screens keep refreshing for a while after the app goes to the
        // background; that must not count as the user seeing anything (it
        // used to swallow every new text before it could be notified).
        if (!appVisible) return
        if (msgId > store.notifMsgId && store.notifMsgId != 0L) store.notifMsgId = msgId
        if (missedCallId > store.notifCallId && store.notifCallId != 0L)
            store.notifCallId = missedCallId
        if (vmId > store.notifVmId && store.notifVmId != 0L) store.notifVmId = vmId
        if (faxId > store.notifFaxId && store.notifFaxId != 0L) store.notifFaxId = faxId
    }

    fun stop(ctx: Context) {
        ctx.stopService(Intent(ctx, LiveService::class.java))
        WorkManager.getInstance(ctx).cancelUniqueWork(WORK_PERIODIC)
        NotificationManagerCompat.from(ctx).cancelAll()
    }

    /** Clear notifications for what the user is now looking at. */
    fun clear(ctx: Context, what: String, peer: String? = null) {
        val nm = NotificationManagerCompat.from(ctx)
        when (what) {
            "calls" -> nm.cancel(ID_CALLS)
            "voicemail" -> nm.cancel(ID_VOICEMAIL)
            "faxes" -> nm.cancel(ID_FAX)
            "messages" ->
                if (peer != null) nm.cancel(msgId(peer))
                else
                    nm.activeNotifications
                        .filter { it.id >= ID_MSG_BASE || it.notification.group == GROUP_MESSAGES }
                        .forEach { nm.cancel(it.id) }
        }
    }

    private fun msgId(peer: String) = ID_MSG_BASE + (peer.hashCode() and 0xffff)

    private fun canNotify(ctx: Context) =
        Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) ==
                PackageManager.PERMISSION_GRANTED

    private fun open(ctx: Context, what: String, peer: String?, req: Int): PendingIntent {
        val i =
            Intent(ctx, MainActivity::class.java)
                .setAction("org.ownpbx.myphone.OPEN_$what")
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
                .putExtra(EXTRA_OPEN, what)
        if (peer != null) i.putExtra(EXTRA_PEER, peer)
        return PendingIntent.getActivity(
            ctx,
            req,
            i,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    private fun base(ctx: Context, channel: String) =
        NotificationCompat.Builder(ctx, channel)
            .setSmallIcon(R.drawable.ic_notification)
            .setAutoCancel(true)
            // Lock screen shows only "My Phone - New ..." unless the phone allows private content.
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)

    /**
     * Ask the PBX what's new and notify (quietly record it when [seenOnly] or the app is on screen,
     * unless [force]). Saves a one-line status for Settings and returns it.
     */
    suspend fun run(ctx: Context, seenOnly: Boolean, force: Boolean = false): String {
        val store = Store(ctx)
        if (store.token == null) {
            stop(ctx)
            return "Signed out"
        }
        val nm = NotificationManagerCompat.from(ctx)
        val blocked = !canNotify(ctx) || !nm.areNotificationsEnabled()
        val quiet = seenOnly || (appVisible && !force) || !store.notifyOn || blocked
        val firstRun = store.notifMsgId == 0L || store.notifCallId == 0L || store.notifVmId == 0L
        val api = store.api()
        var posted = 0
        var found = 0

        // ---- texts
        val ov = api.overview()
        if (store.notifMsgId == 0L) {
            store.notifMsgId = ov.latest_message_id.coerceAtLeast(1)
            store.notifBaseMsgId = store.notifMsgId
        } else if (ov.latest_message_id > store.notifMsgId) {
            val fresh =
                api.since(store.notifMsgId).messages.filter { !it.mine && it.id > store.notifMsgId }
            store.notifMsgId = maxOf(store.notifMsgId, ov.latest_message_id)
            found += fresh.size
            if (!quiet)
                fresh
                    .groupBy { it.peer }
                    .forEach { (peer, newOnes) ->
                        val who = prettyNumber(peer)
                        val showText = store.notifyShowText
                        // Count every text from this person not yet read in the app (same as
                        // the unread dot), not just the ones found in this check; otherwise
                        // 4 texts arriving across separate checks showed as "1".
                        val msgs =
                            try {
                                    val from = maxOf(store.seenFor(peer), store.notifBaseMsgId)
                                    api.thread(peer).messages.filter { !it.mine && it.id > from }
                                } catch (e: Exception) {
                                    emptyList()
                                }
                                .ifEmpty { newOnes }
                        val lines =
                            msgs.sortedBy { it.id }.map { if (showText) it.body else "New message" }
                        val b =
                            base(ctx, CH_MESSAGES)
                                .setContentTitle(who)
                                .setContentText(
                                    if (showText) lines.last()
                                    else if (msgs.size == 1) "New message"
                                    else "${msgs.size} new messages"
                                )
                                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                                .setGroup(GROUP_MESSAGES)
                                .setNumber(msgs.size)
                                .setContentIntent(open(ctx, "messages", peer, msgId(peer)))
                                .setPublicVersion(
                                    base(ctx, CH_MESSAGES)
                                        .setContentTitle("My Phone")
                                        .setContentText("New message")
                                        .build()
                                )
                        if (showText && lines.size > 1)
                            b.setStyle(
                                NotificationCompat.InboxStyle().also { st ->
                                    lines.takeLast(6).forEach { st.addLine(it) }
                                }
                            )
                        else if (showText)
                            b.setStyle(NotificationCompat.BigTextStyle().bigText(lines.last()))
                        nm.notify(msgId(peer), b.build())
                        posted++
                    }
        }

        // ---- missed calls (hidden calls are already left out by the PBX).
        // A call nobody answered usually rolls to voicemail; the PBX lists
        // those as "vm", and they're missed calls too.
        val missed = api.calls("missed").calls.filter { it.kind == "missed" || it.kind == "vm" }
        val topCall = missed.maxOfOrNull { it.id } ?: 0
        if (store.notifCallId == 0L) {
            store.notifCallId = topCall.coerceAtLeast(1)
        } else {
            val fresh = missed.filter { it.id > store.notifCallId }
            store.notifCallId = maxOf(store.notifCallId, topCall)
            found += fresh.size
            // Calls the app rang for and already notified about itself.
            val unannounced = fresh.filterNot {
                alreadyNotified(store, it.other, callTime(it.started_at))
            }
            if (!quiet && unannounced.isNotEmpty()) {
                posted++
                // All missed calls not yet looked at in the app (the PBX's per-phone count).
                val n = maxOf(ov.missed_new ?: 0, fresh.size)
                postMissed(ctx, missed.take(n).map { it.other to callTime(it.started_at) }, n)
            }
        }

        // ---- voicemail
        val vms = api.voicemail("INBOX").voicemail
        val topVm = vms.maxOfOrNull { it.id } ?: 0
        if (store.notifVmId == 0L) {
            store.notifVmId = topVm.coerceAtLeast(1)
        } else {
            val fresh = vms.filter { it.id > store.notifVmId && it.is_read == 0 }
            store.notifVmId = maxOf(store.notifVmId, topVm)
            found += fresh.size
            if (!quiet && fresh.isNotEmpty()) {
                posted++
                // All unheard voicemail, not just what arrived since the last check.
                val n = maxOf(vms.count { it.is_read == 0 }, fresh.size)
                val first = fresh.maxBy { it.id }
                val from = prettyNumber(first.caller.ifEmpty { "Unknown caller" })
                nm.notify(
                    ID_VOICEMAIL,
                    base(ctx, CH_VOICEMAIL)
                        .setContentTitle(if (n == 1) "New voicemail" else "$n new voicemails")
                        .setContentText(if (n == 1) "From $from" else "Latest from $from")
                        .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                        .setNumber(n)
                        .setContentIntent(open(ctx, "voicemail", null, ID_VOICEMAIL))
                        .setPublicVersion(
                            base(ctx, CH_VOICEMAIL)
                                .setContentTitle("My Phone")
                                .setContentText("New voicemail")
                                .build()
                        )
                        .build(),
                )
            }
        }
        // ---- faxes (only on PBXes with fax, and only for users a fax number rings)
        if (ov.fax_enabled) {
            try {
                val got = api.faxes().received
                val topFax = got.maxOfOrNull { it.id } ?: 0
                if (store.notifFaxId == 0L) {
                    store.notifFaxId = topFax.coerceAtLeast(1)
                } else {
                    val fresh = got.filter { it.id > store.notifFaxId && !it.seen && it.status == "ok" }
                    store.notifFaxId = maxOf(store.notifFaxId, topFax)
                    found += fresh.size
                    if (!quiet && fresh.isNotEmpty()) {
                        posted++
                        val n = maxOf(got.count { !it.seen && it.status == "ok" }, fresh.size)
                        val first = fresh.maxBy { it.id }
                        val from =
                            first.caller_name.ifBlank {
                                first.remote_station.ifBlank { prettyNumber(first.caller.ifEmpty { "Unknown sender" }) }
                            }
                        val pages = if (first.pages == 1) "1 page" else "${first.pages} pages"
                        nm.notify(
                            ID_FAX,
                            base(ctx, CH_FAX)
                                .setContentTitle(if (n == 1) "New fax" else "$n new faxes")
                                .setContentText(if (n == 1) "From $from · $pages" else "Latest from $from")
                                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                                .setNumber(n)
                                .setContentIntent(open(ctx, "faxes", null, ID_FAX))
                                .setPublicVersion(
                                    base(ctx, CH_FAX).setContentTitle("My Phone").setContentText("New fax").build()
                                )
                                .build(),
                        )
                    }
                }
            } catch (e: Exception) {
                // An older PBX without fax sending: no fax notifications.
            }
        }
        val status =
            when {
                firstRun -> "First check: noted what's already there (only newer things notify)"
                found == 0 -> "Nothing new"
                blocked -> "$found new, but Android is blocking this app's notifications"
                !store.notifyOn -> "$found new (notifications are off)"
                quiet -> "$found new, already shown in the app"
                else -> "$found new, $posted notification(s)"
            }
        record(store, status)
        return status
    }

    // ------------------------------------------------------------ missed calls

    /** Server call times are the PBX's local time, "yyyy-MM-dd HH:mm:ss". */
    fun callTime(ts: String): Long =
        try {
            java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", java.util.Locale.US).parse(ts)?.time
                ?: 0L
        } catch (e: Exception) {
            0L
        }

    /** "2:14 PM" today, "Mon 2:14 PM" this week, "Oct 3, 2:14 PM" older. */
    fun whenText(ctx: Context, at: Long): String {
        if (at <= 0) return ""
        val d = java.util.Date(at)
        val time = android.text.format.DateFormat.getTimeFormat(ctx).format(d)
        val day = java.text.SimpleDateFormat("yyyyMMdd", java.util.Locale.US)
        val age = System.currentTimeMillis() - at
        return when {
            day.format(d) == day.format(java.util.Date()) -> time
            age < 6 * 86400_000L ->
                java.text.SimpleDateFormat("EEE", java.util.Locale.getDefault()).format(d) +
                    " " +
                    time
            else ->
                java.text.SimpleDateFormat("MMM d", java.util.Locale.getDefault()).format(d) +
                    ", " +
                    time
        }
    }

    private fun digits(n: String) = n.filter { it.isDigit() }.takeLast(10)

    private fun alreadyNotified(store: Store, number: String, at: Long): Boolean =
        store.localMissed.split(";").any { e ->
            val num = e.substringBefore("@")
            val t = e.substringAfter("@", "").toLongOrNull() ?: return@any false
            num.isNotEmpty() &&
                digits(num) == digits(number) &&
                (at == 0L || kotlin.math.abs(at - t) < 5 * 60_000L)
        }

    /**
     * The app rang for a call nobody answered: say so right away, without waiting for the PBX
     * (which only lists it once the caller hangs up).
     */
    fun missedNow(ctx: Context, number: String, name: String, at: Long) {
        val store = Store(ctx)
        store.localMissed =
            (store.localMissed.split(";").filter { it.isNotBlank() }.takeLast(9) +
                    "${number.replace(";", "").replace("@", "")}@$at")
                .joinToString(";")
        if (store.token == null || !store.notifyOn) return
        createChannels(ctx)
        val nm = NotificationManagerCompat.from(ctx)
        if (!canNotify(ctx) || !nm.areNotificationsEnabled()) return
        // Add it to any missed calls already showing.
        val showing = nm.activeNotifications.firstOrNull { it.id == ID_CALLS }?.notification
        val before = showing?.extras?.getInt("myphone.count", 0) ?: 0
        val lines = showing?.extras?.getStringArrayList("myphone.lines").orEmpty()
        postMissed(
            ctx,
            listOf((if (name.isNotBlank()) "$name · " else "") + number to at),
            before + 1,
            lines,
        )
    }

    /** One "Missed call" notification: who and when, newest first. */
    private fun postMissed(
        ctx: Context,
        calls: List<Pair<String, Long>>,
        total: Int,
        older: List<String> = emptyList(),
    ) {
        if (calls.isEmpty()) return
        val nm = NotificationManagerCompat.from(ctx)
        val lines =
            (calls.map { (who, at) ->
                    val w =
                        if (who.contains(" · "))
                            who.substringBefore(" · ") +
                                " · " +
                                prettyNumber(who.substringAfter(" · "))
                        else prettyNumber(who.ifEmpty { "Unknown" })
                    val time = whenText(ctx, at)
                    if (time.isEmpty()) w else "$w · $time"
                } + older)
                .distinct()
                .take(maxOf(total, calls.size))
        val n = maxOf(total, lines.size)
        val newest = calls.first().second
        val b =
            base(ctx, CH_CALLS)
                .setContentTitle(if (n == 1) "Missed call" else "$n missed calls")
                .setContentText(if (n == 1) lines.first() else "Latest: " + lines.first())
                .setCategory(NotificationCompat.CATEGORY_MISSED_CALL)
                .setNumber(n)
                .setContentIntent(open(ctx, "calls", null, ID_CALLS))
                .setPublicVersion(
                    base(ctx, CH_CALLS)
                        .setContentTitle("My Phone")
                        .setContentText(if (n == 1) "Missed call" else "$n missed calls")
                        .build()
                )
                .addExtras(
                    android.os.Bundle().apply {
                        putInt("myphone.count", n)
                        putStringArrayList("myphone.lines", ArrayList(lines.take(6)))
                    }
                )
        if (newest > 0) b.setWhen(newest).setShowWhen(true)
        if (lines.size > 1)
            b.setStyle(
                NotificationCompat.InboxStyle().also { st ->
                    lines.take(6).forEach { st.addLine(it) }
                }
            )
        try {
            nm.notify(ID_CALLS, b.build())
        } catch (_: SecurityException) {}
    }

    fun record(store: Store, status: String) {
        store.lastCheckAt = System.currentTimeMillis()
        store.lastCheckInfo = status
    }

    /** A background check found a newer release: say so once per version. */
    fun updateAvailable(ctx: Context, version: String) {
        createChannels(ctx)
        val nm = NotificationManagerCompat.from(ctx)
        if (!canNotify(ctx) || !nm.areNotificationsEnabled()) return
        val openApp =
            PendingIntent.getActivity(
                ctx,
                ID_UPDATE,
                Intent(ctx, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
        try {
            nm.notify(
                ID_UPDATE,
                base(ctx, CH_UPDATES)
                    .setContentTitle("My Phone $version is available")
                    .setContentText("Open the app to update")
                    .setContentIntent(openApp)
                    .build(),
            )
        } catch (_: SecurityException) {}
    }

    /** Settings > Send a test notification. */
    fun test(ctx: Context): Boolean {
        createChannels(ctx)
        val nm = NotificationManagerCompat.from(ctx)
        if (!canNotify(ctx) || !nm.areNotificationsEnabled()) return false
        nm.notify(
            ID_TEST,
            base(ctx, CH_MESSAGES)
                .setContentTitle("Test notification")
                .setContentText("Notifications from My Phone are working.")
                .setContentIntent(open(ctx, "messages", null, ID_TEST))
                .build(),
        )
        return true
    }
}

/**
 * Background check run by WorkManager. Never retries in a loop: the next periodic run is soon
 * enough.
 */
class CheckWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val store = Store(applicationContext)
        try {
            Notifier.run(applicationContext, inputData.getBoolean("seen_only", false))
        } catch (e: ApiException) {
            if (e.status == 401) Notifier.stop(applicationContext) // signed out on the PBX
            Notifier.record(store, "PBX answered with error ${e.status}")
        } catch (e: Exception) {
            // Offline or PBX unreachable: try again next time.
            Notifier.record(store, "Couldn't reach the PBX (${e.javaClass.simpleName})")
        }
        // New-release check, throttled inside Updates to once a day: a
        // background find posts one notification per version, and opening
        // the app shows the update dialog.
        try {
            val offer = org.ownpbx.myphone.data.Updates.refresh(applicationContext)
            if (
                offer != null &&
                    offer.version != store.updateDismissed &&
                    offer.version != store.updateNotified
            ) {
                Notifier.updateAvailable(applicationContext, offer.version)
                store.updateNotified = offer.version
            }
        } catch (_: Exception) {}
        // Watchdog: if the connection service was killed (Motorola /
        // Android battery savers kill even foreground services after a
        // few hours), this 15-minute job is the only thing still running
        // that can bring it back — previously nothing restarted it until
        // the user reopened the app, so the "Connected" icon vanished
        // and calls stopped reaching the phone. Android may still refuse
        // a background start; LiveService.sync already swallows that.
        try {
            LiveService.sync(applicationContext)
        } catch (_: Exception) {}
        return Result.success()
    }
}
