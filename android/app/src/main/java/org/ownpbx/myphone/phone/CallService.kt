// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.phone

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import org.ownpbx.myphone.MainActivity
import org.ownpbx.myphone.R
import org.ownpbx.myphone.ui.prettyNumber

/** Keeps a call (and the microphone) going while the app is in the
 *  background, with an "On a call" notification and a Hang up button. */
class CallService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_HANG_UP -> { Phone.hangUp(); stopSelf(); return START_NOT_STICKY }
            ACTION_DECLINE -> { Phone.decline(); stopSelf(); return START_NOT_STICKY }
        }
        val number = intent?.getStringExtra(EXTRA_NUMBER) ?: Phone.ui?.number.orEmpty()
        try {
            ServiceCompat.startForeground(this, ID, notification(this, number),
                if (Build.VERSION.SDK_INT >= 30) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0)
        } catch (e: Exception) {
            // Without the foreground service the call still works while the app is open.
            stopSelf()
        }
        // Answer from the notification: the service is foreground first, so the
        // microphone keeps working with the screen off.
        if (intent?.action == ACTION_ANSWER && !Phone.answer()) stopSelf()
        return START_NOT_STICKY
    }

    companion object {
        private const val CH = "calls_ongoing"
        private const val ID = 11
        private const val EXTRA_NUMBER = "number"
        private const val ACTION_HANG_UP = "org.ownpbx.myphone.HANG_UP"
        const val ACTION_ANSWER = "org.ownpbx.myphone.ANSWER"
        const val ACTION_DECLINE = "org.ownpbx.myphone.DECLINE"

        /** Answer from inside the app (foreground): go foreground, then answer. */
        fun answer(ctx: Context) {
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, CallService::class.java).setAction(ACTION_ANSWER))
            } catch (_: Exception) { Phone.answer() }
        }

        fun start(ctx: Context, number: String) {
            try {
                ContextCompat.startForegroundService(ctx,
                    Intent(ctx, CallService::class.java).putExtra(EXTRA_NUMBER, number))
            } catch (_: Exception) {}
        }

        fun stop(ctx: Context) { ctx.stopService(Intent(ctx, CallService::class.java)) }

        private fun notification(ctx: Context, number: String): android.app.Notification {
            if (Build.VERSION.SDK_INT >= 26) {
                ctx.getSystemService(NotificationManager::class.java).createNotificationChannel(
                    NotificationChannel(CH, "Ongoing call", NotificationManager.IMPORTANCE_LOW).apply {
                        description = "Shown during a call made from My Phone"
                        setShowBadge(false)
                    })
            }
            val open = PendingIntent.getActivity(ctx, ID, Intent(ctx, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val hang = PendingIntent.getService(ctx, ID + 1, Intent(ctx, CallService::class.java).setAction(ACTION_HANG_UP),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            return NotificationCompat.Builder(ctx, CH)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle("On a call")
                .setContentText(prettyNumber(number))
                .setOngoing(true)
                .setUsesChronometer(true)
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setContentIntent(open)
                .addAction(0, "Hang up", hang)
                .build()
        }
    }
}
