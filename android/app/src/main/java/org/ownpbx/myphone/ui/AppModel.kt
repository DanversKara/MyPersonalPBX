// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(
    androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.ExperimentalFoundationApi::class,
)

package org.ownpbx.myphone.ui

import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarResult
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import org.ownpbx.myphone.data.Api
import org.ownpbx.myphone.data.ApiException
import org.ownpbx.myphone.data.Store

enum class Stage {
    SignIn,
    SetPin,
    Locked,
    Ready,
}

/** App-wide state: where we are (sign in / PIN / main) and error messages. */
class AppModel(val store: Store, context: android.content.Context) {
    /** Offline copy of PBX contacts (emergency access without internet). */
    val contactCache = org.ownpbx.myphone.data.ContactCache(context)
    var stage by mutableStateOf(initialStage())
    /** One-shot message for the snackbar. */
    var notice by mutableStateOf<String?>(null)
    /**
     * Server + code from a pairing link the phone's camera opened; the sign-in screen fills them in
     * (the user still taps Connect).
     */
    var pendingPair by mutableStateOf<org.ownpbx.myphone.data.PairInfo?>(null)
    /**
     * Where a tapped notification wants to go: "messages" | "calls" | "voicemail", plus a chat
     * peer.
     */
    var openTarget by mutableStateOf<Pair<String, String?>?>(null)
    /**
     * Where the user was: kept here (not in the screen) so it survives the PIN lock, e.g. after
     * switching to a password manager.
     */
    var tab by mutableStateOf(Tab.Messages)
    var chatWith by mutableStateOf<String?>(null)
    var settingsDialog by mutableStateOf<String?>(null)
    /** Phone login dialog text being typed (null = not open / not started). */
    var sipDraft by mutableStateOf<org.ownpbx.myphone.data.SipLogin?>(null)
    /** A phone-login link opened from the camera, waiting for "Use it?". */
    var pendingSip by mutableStateOf<org.ownpbx.myphone.data.SipLogin?>(null)

    /** A newer release the user hasn't dismissed (AppRoot shows the update dialog). */
    var updateOffer by mutableStateOf<org.ownpbx.myphone.data.Updates.Offer?>(null)
        private set

    /** True while an update .apk is downloading. */
    var updateWorking by mutableStateOf(false)
        private set

    /**
     * Look for a newer release (network-throttled inside Updates unless [force]) and, if there is
     * one, offer it. Run every time the app comes to the front — so an available update is on
     * screen the moment the app is opened, never buried in Settings — and from Settings' "Check
     * now". Never disturbs anything else when it fails.
     */
    fun checkUpdates(ctx: android.content.Context, force: Boolean = false) {
        bg.launch {
            val offer =
                try {
                    org.ownpbx.myphone.data.Updates.refresh(ctx, force)
                } catch (e: Exception) {
                    null
                }
            if (offer != null) updateOffer = offer
            else if (force)
                notice = "You're up to date (${org.ownpbx.myphone.data.Updates.installedVersion()})"
        }
    }

    /**
     * "Later": hide the dialog for now only. The offer is still there, so the dialog comes back the
     * next time the app is opened — an update is never dismissed for good by a stray tap.
     */
    fun dismissUpdate() {
        updateOffer = null
    }

    /**
     * Open the offered update's .apk in the browser: it downloads there and the browser hands it to
     * the system installer. (The app itself can't install packages: Google Play doesn't allow that
     * permission, so the Play build and the GitHub build share one manifest without it.)
     */
    fun startUpdate(ctx: android.content.Context) {
        val offer = updateOffer ?: return
        try {
            ctx.startActivity(
                android.content.Intent(android.content.Intent.ACTION_VIEW, android.net.Uri.parse(offer.apkUrl))
                    .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            updateOffer = null
            notice = "Downloading ${offer.version} in your browser. Open it when it's done to install."
        } catch (e: Exception) {
            notice = "No web browser found to download the update."
        }
    }

    /** A screen asked to call this number (MainScreen handles mic permission + the call). */
    var callRequest by mutableStateOf<String?>(null)
    /**
     * A screen asked for a push-to-talk call to this extension (MainScreen handles mic permission,
     * then dials *88 + the extension).
     */
    var pttCallRequest by mutableStateOf<String?>(null)
    /** Dial pad open (with this number pre-filled); null = closed. */
    var dialPad by mutableStateOf<String?>(null)
    /** Phone settings from the PBX, fetched on the first call. */
    var sip: org.ownpbx.myphone.data.SipInfo? = null

    /** Set by the activity: stop background checks and clear notifications. */
    var onSignedOut: () -> Unit = {}
    var onSecureChanged: () -> Unit = {}

    /**
     * Outlives every screen: deletes/hides started here finish even if the user switches tabs or
     * leaves the app while the Undo bar is up. (Screen scopes are cancelled when the screen goes
     * away, which used to drop the change, so deleted voicemail / hidden calls came back.)
     */
    val bg = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    /**
     * Items removed on screen while their change is pending ("vm:12", "call:5", "conv:8801").
     * Screens filter these out, so a freshly opened screen doesn't flash them back before the
     * server catches up.
     */
    var gone by mutableStateOf(setOf<String>())

    /**
     * Show "<text> · Undo"; unless Undo is tapped, run [commit] on the PBX. The bar times out on
     * its own so a queued one can't hold a change back.
     */
    fun undoable(
        snack: SnackbarHostState,
        text: String,
        keys: Set<String>,
        commit: suspend (Api) -> org.ownpbx.myphone.data.Result,
        after: suspend () -> Unit = {},
    ) {
        gone = gone + keys
        bg.launch {
            val r =
                withTimeoutOrNull(8_000) {
                    snack.showSnackbar(
                        text,
                        actionLabel = "Undo",
                        duration = SnackbarDuration.Short,
                    )
                }
            if (r != SnackbarResult.ActionPerformed) {
                change(commit)
                after()
            }
            gone = gone - keys
        }
    }

    /**
     * Run [commit] now (reversible changes like hiding a call), then offer Undo, which runs
     * [revert].
     */
    fun doThenUndo(
        snack: SnackbarHostState,
        text: String,
        keys: Set<String>,
        commit: suspend (Api) -> org.ownpbx.myphone.data.Result,
        revert: suspend (Api) -> org.ownpbx.myphone.data.Result,
        after: suspend () -> Unit = {},
    ) {
        gone = gone + keys
        bg.launch {
            val ok = change(commit)
            after()
            gone = gone - keys
            if (!ok) return@launch
            val r =
                withTimeoutOrNull(8_000) {
                    snack.showSnackbar(
                        text,
                        actionLabel = "Undo",
                        duration = SnackbarDuration.Short,
                    )
                }
            if (r == SnackbarResult.ActionPerformed) {
                change(revert)
                after()
            }
        }
    }

    private fun initialStage() =
        when {
            store.token == null -> Stage.SignIn
            store.pinHash == null -> Stage.SetPin
            leftRecently() -> Stage.Ready
            else -> Stage.Locked
        }

    /** True if the user left the app unlocked less than "lock after" ago. */
    fun leftRecently(): Boolean {
        val at = store.leftUnlockedAt
        val now = android.os.SystemClock.elapsedRealtime()
        return at > 0 && now >= at && now - at < store.lockAfterSec * 1000L
    }

    /** Called when the app goes to the background. */
    fun leaving() {
        store.leftUnlockedAt =
            if (stage == Stage.Ready) android.os.SystemClock.elapsedRealtime() else 0
    }

    /**
     * Set just before opening one of the app's own screens (QR scanner): coming back from it within
     * 3 minutes doesn't count as leaving.
     */
    private var ownScreenAt = 0L

    fun openingOwnScreen() {
        ownScreenAt = android.os.SystemClock.elapsedRealtime()
    }

    /** Called when the app comes back: lock if away too long. */
    fun returning() {
        if (ownScreenAt > 0 && android.os.SystemClock.elapsedRealtime() - ownScreenAt < 180_000) {
            ownScreenAt = 0
            store.leftUnlockedAt = 0
            return
        }
        ownScreenAt = 0
        if (stage == Stage.Ready && store.leftUnlockedAt > 0 && !leftRecently()) lock()
        store.leftUnlockedAt = 0
    }

    fun api(): Api = store.api()

    fun lock() {
        if (stage == Stage.Ready && store.pinHash != null) stage = Stage.Locked
        store.leftUnlockedAt = 0
    }

    fun signedOut(message: String? = null) {
        onSignedOut()
        store.wipe()
        stage = Stage.SignIn
        notice = message
    }

    /** Run an API call; 401 signs out, other errors become a snackbar. */
    suspend fun <T> guard(block: suspend (Api) -> T): T? =
        try {
            block(api())
        } catch (e: ApiException) {
            if (e.status == 401) signedOut("You were signed out. Please sign in again.")
            else notice = e.message
            null
        } catch (e: kotlinx.serialization.SerializationException) {
            notice =
                "The PBX answered with something this app version doesn't understand (${e.message?.take(80)})."
            null
        } catch (e: Exception) {
            notice = "Can't reach your PBX. Check your connection."
            null
        }

    /** Like guard, but other errors stay silent (background housekeeping). */
    suspend fun <T> guardQuiet(block: suspend (Api) -> T): T? =
        try {
            block(api())
        } catch (e: ApiException) {
            if (e.status == 401) signedOut("You were signed out. Please sign in again.")
            null
        } catch (e: Exception) {
            null
        }

    /** For change calls: show the server's answer, return whether it worked. */
    suspend fun change(block: suspend (Api) -> org.ownpbx.myphone.data.Result): Boolean {
        val r = guard(block) ?: return false
        if (!r.ok || r.code !in setOf("saved", "deleted", "msgsent", "ok", "vmdel", "recdel"))
            notice = r.message
        return r.ok
    }
}
