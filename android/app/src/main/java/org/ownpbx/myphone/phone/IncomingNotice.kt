// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.phone

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.ownpbx.myphone.MainActivity
import org.ownpbx.R
import org.ownpbx.myphone.ui.prettyNumber

/**
 * The "Incoming call" notification: full-screen over the lock screen, with Answer / Decline. The
 * ringtone itself is played by Linphone.
 */
object IncomingNotice {
    private const val CH = "incoming_calls"
    private const val ID = 12
    const val EXTRA_CALL = "org.ownpbx.myphone.SHOW_CALL"

    fun show(ctx: Context, number: String, name: String, ptt: Boolean = false) {
        if (Build.VERSION.SDK_INT >= 26) {
            ctx.getSystemService(NotificationManager::class.java)
                .createNotificationChannel(
                    NotificationChannel(CH, "Incoming calls", NotificationManager.IMPORTANCE_HIGH)
                        .apply {
                            description = "Ringing for calls to your extension"
                            setSound(null, null) // the ringtone comes from the call itself
                            setShowBadge(false)
                            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
                        }
                )
        }
        val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        val open =
            PendingIntent.getActivity(
                ctx,
                ID,
                Intent(ctx, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra(EXTRA_CALL, true),
                flags,
            )
        val answer =
            PendingIntent.getForegroundService(
                ctx,
                ID + 1,
                Intent(ctx, CallService::class.java).setAction(CallService.ACTION_ANSWER),
                flags,
            )
        val decline =
            PendingIntent.getService(
                ctx,
                ID + 2,
                Intent(ctx, CallService::class.java).setAction(CallService.ACTION_DECLINE),
                flags,
            )
        val who = name.ifBlank { prettyNumber(number) }
        val n =
            NotificationCompat.Builder(ctx, CH)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(if (ptt) "Push-to-talk page" else "Incoming call")
                .setContentText(
                    if (ptt) "${who} is paging you — Accept to connect"
                    else if (name.isNotBlank()) "$name · ${prettyNumber(number)}" else who
                )
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                .setOngoing(true)
                .setAutoCancel(false)
                .setContentIntent(open)
                .setFullScreenIntent(open, true)
                .addAction(0, if (ptt) "No accept" else "Decline", decline)
                .addAction(0, if (ptt) "Accept" else "Answer", answer)
                .build()
        try {
            NotificationManagerCompat.from(ctx).notify(ID, n)
        } catch (_: SecurityException) {}
    }

    fun cancel(ctx: Context) {
        NotificationManagerCompat.from(ctx).cancel(ID)
    }
}
