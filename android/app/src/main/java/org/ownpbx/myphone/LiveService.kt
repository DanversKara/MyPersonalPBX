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
        if (job?.isActive != true) job = scope.launch { loop() }
        return START_STICKY
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private suspend fun loop() {
        var backoff = 5_000L
        while (scope.isActive) {
            val store = Store(this)
            if (!wanted(store)) { stopSelf(); return }
            try {
                val every = store.checkEvery
                if (every == 0) {
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
                    delay(every * 1000L)
                }
                backoff = 5_000L
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiException) {
                if (e.status == 401) { Notifier.stop(this); stopSelf(); return }   // signed out on the PBX
                Notifier.record(store, "PBX answered with error ${e.status}; retrying")
                delay(backoff); backoff = (backoff * 2).coerceAtMost(120_000L)
            } catch (e: Exception) {
                Notifier.record(store, "Couldn't reach the PBX (${e.javaClass.simpleName}); retrying")
                delay(backoff); backoff = (backoff * 2).coerceAtMost(120_000L)
            }
        }
    }

    companion object {
        const val CH_ONGOING = "connection"
        private const val ID_ONGOING = 7

        fun wanted(store: Store) = store.token != null && store.notifyOn && store.checkEvery < 900

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
                ctx.getSystemService(NotificationManager::class.java).createNotificationChannel(
                    NotificationChannel(CH_ONGOING, "Connection to your PBX", NotificationManager.IMPORTANCE_MIN)
                        .apply { description = "Shown while My Phone stays connected for fast notifications. You can hide it." })
            }
            val open = PendingIntent.getActivity(ctx, ID_ONGOING, Intent(ctx, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            return NotificationCompat.Builder(ctx, CH_ONGOING)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("My Phone is connected")
                .setContentText(if (store.checkEvery == 0) "New texts, calls and voicemail notify right away"
                    else describe(store.checkEvery))
                .setOngoing(true)
                .setShowWhen(false)
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
