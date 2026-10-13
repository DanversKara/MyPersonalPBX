// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone

import android.content.Intent
import android.os.Bundle
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.fragment.app.FragmentActivity
import org.ownpbx.myphone.data.PairLink
import org.ownpbx.myphone.data.Store
import org.ownpbx.myphone.ui.Stage
import org.ownpbx.myphone.ui.AppModel
import org.ownpbx.myphone.ui.AppRoot
import org.ownpbx.myphone.ui.MyPhoneTheme

/** Survives screen rotation; a fresh process always starts locked. */
object Holder {
    var model: AppModel? = null
}

class MainActivity : FragmentActivity() {
    private lateinit var model: AppModel
    private var stoppedAt = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        CrashLog.install(this)
        enableEdgeToEdge()
        model = Holder.model ?: AppModel(Store(applicationContext)).also { Holder.model = it }
        model.onSecureChanged = { applySecure() }
        model.onSignedOut = { Notifier.stop(applicationContext) }
        applySecure()
        Notifier.createChannels(this)
        if (savedInstanceState == null) { handlePairLink(intent); handleOpen(intent) }
        setContent { MyPhoneTheme { AppRoot(model, this) } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handlePairLink(intent)
        handleOpen(intent)
    }

    /** A tapped notification: open that tab (after the PIN, if locked). */
    private fun handleOpen(intent: Intent?) {
        val what = intent?.getStringExtra(Notifier.EXTRA_OPEN) ?: return
        model.openTarget = what to intent.getStringExtra(Notifier.EXTRA_PEER)
    }

    /** myphone://pair?s=..&c=.. opened from the camera app or a browser. */
    private fun handlePairLink(intent: Intent?) {
        if (intent?.action != Intent.ACTION_VIEW) return
        val info = PairLink.parse(intent.dataString) ?: return
        if (model.stage == Stage.SignIn) model.pendingPair = info
        else model.notice = "This phone is already connected. To pair again, sign out in Settings first."
    }

    /** Hide content from screenshots and the recent-apps preview (switchable). */
    private fun applySecure() {
        if (model.store.secureScreen) window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
    }

    override fun onStop() {
        super.onStop()
        if (!isChangingConfigurations) {
            stoppedAt = SystemClock.elapsedRealtime()
            Notifier.appVisible = false
        }
    }

    override fun onStart() {
        super.onStart()
        Notifier.appVisible = true
        Notifier.schedule(applicationContext)
        if (stoppedAt > 0 && SystemClock.elapsedRealtime() - stoppedAt >= model.store.lockAfterSec * 1000L) {
            model.lock()
        }
        stoppedAt = 0
    }
}
