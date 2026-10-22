// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.ApiException
import org.ownpbx.myphone.data.Store
import org.ownpbx.myphone.phone.Phone

/**
 * Keeps My Phone in touch with the PBX for fast notifications.
 *
 * "Instant": a long poll (GET /me/wait) that the PBX answers within ~2 s of
 * a new text, missed call or voicemail. Otherwise it checks every N seconds.
 * Android only lets an app do this while it shows an ongoing notification,
 * so there is a quiet "Connected" one (its channel can be hidden in
 * Android's settings). The 15-minute background job keeps running as a
 * fallback.
 */
class LiveService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var job: Job? = null
    @Volatile private var refreshCalls = true
    private var callsCheckedAt = 0L
    @Volatile private var waitMissing = false
    @Volatile private var waitMissingSince = 0L
    /** Poked when the phone gets a connection back: check right away. */
    private val wake = kotlinx.coroutines.channels.Channel<Unit>(kotlinx.coroutines.channels.Channel.CONFLATED)
    private var netCallback: android.net.ConnectivityManager.NetworkCallback? = null

    override fun onCreate() {
        super.onCreate()
        // Back online after being offline (no signal, airplane mode, PBX
        // unreachable): catch up at once, so missed calls and texts notify
        // as soon as the phone can reach the PBX.
        try {
            val cm = getSystemService(android.net.ConnectivityManager::class.java)
            val cb = object : android.net.ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: android.net.Network) {
                    refreshCalls = true
                    wake.trySend(Unit)
                }
            }
            cm.registerDefaultNetworkCallback(cb)
            netCallback = cb
        } catch (_: Exception) {}
    }

    /** Wait [ms], or less if the connection comes back. */
    private suspend fun pause(ms: Long) {
        wake.tryReceive()                 // forget an old poke
        kotlinx.coroutines.withTimeoutOrNull(ms) { wake.receive() }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val store = Store(this)
        if (!wanted(store)) { stopSelf(); return START_NOT_STICKY }
        try {
            ServiceCompat.startForeground(this, ID_ONGOING, ongoing(this, store),
                if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_REMOTE_MESSAGING else 0)
        } catch (e: Exception) {
            stopSelf(); return START_NOT_STICKY       // Android refused (e.g. started from the background)
        }
        refreshCalls = true          // settings may have changed: re-check the phone sign-in now
        if (job?.isActive != true) job = scope.launch { loop() }
        return START_STICKY
    }

    override fun onDestroy() {
        try { netCallback?.let { getSystemService(android.net.ConnectivityManager::class.java).unregisterNetworkCallback(it) } } catch (_: Exception) {}
        scope.cancel()
        // No service = nothing keeps the app alive to ring: sign out of calls.
        if (Phone.ui == null || Phone.ui?.ended == true) Phone.stopListening()
        super.onDestroy()
    }

    /** Swiped away from Recents (Motorola kills apps there): try to come
     *  straight back, while this process is still able to start itself. */
    override fun onTaskRemoved(rootIntent: Intent?) {
        try { sync(applicationContext) } catch (_: Exception) {}
        super.onTaskRemoved(rootIntent)
    }

    /** Keep the built-in phone signed in on the app line (incoming calls),
     *  or signed out when they're off. Re-checked every 10 minutes while
     *  the sign-in is healthy; an unregistered phone re-checks on every
     *  loop pass instead, so a dropped registration recovers in under a
     *  minute rather than sitting dead for up to 10 minutes. */
    private suspend fun ensureCalls(store: Store) {
        val now = android.os.SystemClock.elapsedRealtime()
        if (!refreshCalls && (!store.incomingCalls || Phone.registered) && now - callsCheckedAt < 10 * 60_000L) return
        refreshCalls = false
        callsCheckedAt = now
        if (!store.incomingCalls) {
            kotlinx.coroutines.withContext(Dispatchers.Main) { org.ownpbx.myphone.phone.Phone.stopListening() }
            return
        }
        val pbx = try { store.api().sip() } catch (e: ApiException) {
            if (e.status == 401) throw e
            null
        } catch (e: CancellationException) { throw e } catch (e: Exception) { null }
        val sip = org.ownpbx.myphone.ui.applySipOverrides(store, pbx) ?: return
        if (sip.enabled) kotlinx.coroutines.withContext(Dispatchers.Main) {
            org.ownpbx.myphone.phone.Phone.startListening(this@LiveService, sip)
        }
    }

    private suspend fun loop() {
        var backoff = 5_000L
        while (scope.isActive) {
            val store = Store(this)
            if (!wanted(store)) { stopSelf(); return }
            try {
                ensureCalls(store)
                val every = store.checkEvery
                if (!notifying(store)) {
                    // Only here for incoming calls: the 15-minute job does the notifications.
                    pause(30_000)
                } else if (every == 0 && waitMissing) {
                    // Older PBX without /me/wait: check every minute until it's updated.
                    Notifier.run(this, seenOnly = false)
                    Notifier.record(store, store.lastCheckInfo.substringBefore(" ·") +
                        " · Instant needs a PBX update (run pbx-update); checking every minute")
                    delay(60_000)
                    if (System.currentTimeMillis() - waitMissingSince > 15 * 60_000L) waitMissing = false   // try again
                } else if (every == 0) {
                    // First run sets the baselines; after that wait for something newer.
                    if (store.notifMsgId == 0L || store.notifCallId == 0L || store.notifVmId == 0L) {
                        Notifier.run(this, seenOnly = false)
                    }
                    val w = store.api().wait(store.notifMsgId, store.notifCallId, store.notifVmId, 50)
                    if (w.changed) {
                        Notifier.run(this, seenOnly = false)
                        delay(1_000)                 // never spin, even if ids disagree
                    }
                } else {
                    Notifier.run(this, seenOnly = false)
                    pause(every * 1000L)
                }
                backoff = 5_000L
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiException) {
                if (e.status == 401) { Notifier.stop(this); stopSelf(); return }   // signed out on the PBX
                if (e.status == 404 || e.status == 405) {
                    waitMissing = true; waitMissingSince = System.currentTimeMillis(); continue
                }
                Notifier.record(store, "PBX answered with error ${e.status}; retrying")
                pause(backoff); backoff = (backoff * 2).coerceAtMost(120_000L)
            } catch (e: Exception) {
                Notifier.record(store, "Couldn't reach the PBX (${e.javaClass.simpleName}); retrying")
                if (wake.tryReceive().isSuccess) backoff = 5_000L   // network just came back
                pause(backoff); backoff = (backoff * 2).coerceAtMost(120_000L)
            }
        }
    }

    companion object {
        // "connection" (0.5.0) counted toward the launcher's dot; a channel's
        // badge setting can't be changed later, so it's replaced by "live".
        const val CH_ONGOING = "live"
        private const val CH_OLD = "connection"
        private const val ID_ONGOING = 7

        /** Fast notifications (Instant / 30 s - 5 min) need this service. */
        fun notifying(store: Store) = store.notifyOn && store.checkEvery < 900
        /** Runs for fast notifications and/or to ring for incoming calls. */
        fun wanted(store: Store) = store.token != null && (notifying(store) || store.incomingCalls)

        /** Start or stop to match the settings. Call from the app (foreground)
         *  or at boot; Android may refuse a start from the background. */
        fun sync(ctx: Context) {
            val store = Store(ctx)
            val i = Intent(ctx, LiveService::class.java)
            if (wanted(store)) {
                try { ContextCompat.startForegroundService(ctx, i) } catch (_: Exception) {}
            } else {
                ctx.stopService(i)
            }
        }

        fun describe(every: Int) = when (every) {
            0 -> "Instant"
            in 1..59 -> "Every $every seconds"
            in 60..899 -> "Every ${every / 60} minute" + if (every >= 120) "s" else ""
            else -> "Every 15 minutes (saves battery)"
        }

        private fun ongoing(ctx: Context, store: Store): android.app.Notification {
            if (Build.VERSION.SDK_INT >= 26) {
                val nm = ctx.getSystemService(NotificationManager::class.java)
                nm.deleteNotificationChannel(CH_OLD)
                nm.createNotificationChannel(
                    NotificationChannel(CH_ONGOING, "Connection to your PBX", NotificationManager.IMPORTANCE_MIN).apply {
                        description = "Shown while My Phone stays connected for fast notifications. You can hide it."
                        setShowBadge(false)          // never puts a dot / number on the app icon
                    })
            }
            val open = PendingIntent.getActivity(ctx, ID_ONGOING, Intent(ctx, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            return NotificationCompat.Builder(ctx, CH_ONGOING)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("My Phone is connected")
                .setContentText(when {
                    store.incomingCalls && notifying(store) -> "Ready for calls · notifications: " + describe(store.checkEvery).lowercase()
                    store.incomingCalls -> "Ready for incoming calls"
                    store.checkEvery == 0 -> "New texts, calls and voicemail notify right away"
                    else -> describe(store.checkEvery)
                })
                .setOngoing(true)
                .setShowWhen(false)
                .setNumber(0)
                .setBadgeIconType(NotificationCompat.BADGE_ICON_NONE)
                .setPriority(NotificationCompat.PRIORITY_MIN)
                .setContentIntent(open)
                .build()
        }
    }
}

/** After a restart (or an app update), reconnect without opening the app. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> {
                Notifier.schedule(ctx)
                LiveService.sync(ctx)
            }
        }
    }
}
