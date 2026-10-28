// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(
    androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.ExperimentalFoundationApi::class,
)

package org.ownpbx.myphone.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Contacts
import androidx.compose.material.icons.filled.Fax
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Voicemail
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import org.ownpbx.myphone.Notifier
import org.ownpbx.myphone.data.Overview

enum class Tab(val label: String, val icon: ImageVector) {
    Messages("Messages", Icons.Filled.Chat),
    Calls("Calls", Icons.Filled.Call),
    Contacts("Contacts", Icons.Filled.Contacts),
    Voicemail("Voicemail", Icons.Filled.Voicemail),
    /** Only shown when the PBX says faxing is on for this user. */
    Faxes("Faxes", Icons.Filled.Fax),
    Settings("Settings", Icons.Filled.Settings),
}

/** Settings > Calling > Phone login: typed-in values win over the PBX's. */
fun applySipOverrides(
    st: org.ownpbx.myphone.data.Store,
    pbx: org.ownpbx.myphone.data.SipInfo?,
): org.ownpbx.myphone.data.SipInfo? {
    val server = st.sipServerOverride.trim().removePrefix("sips:").removePrefix("sip:").trimEnd('/')
    val host = server.substringBefore(':')
    val port = server.substringAfter(':', "").toIntOrNull()
    val user = st.sipUserOverride.trim()
    val pass = st.sipPassOverride
    if (server.isBlank() && user.isBlank() && pass.isBlank()) return pbx
    val base = pbx ?: org.ownpbx.myphone.data.SipInfo()
    val out =
        base.copy(
            domain = host.ifBlank { base.domain },
            port = port ?: if (host.isNotBlank()) 5061 else base.port,
            username = user.ifBlank { base.username },
            password = pass.ifBlank { base.password },
        )
    val ok = out.domain.isNotBlank() && out.username.isNotBlank() && out.password.isNotBlank()
    return out.copy(
        enabled = ok,
        reason =
            if (ok) ""
            else
                "Fill in server, username and password under Settings > Calling, or clear them to use your PBX's.",
    )
}

/**
 * Save the PBX's phone login as this phone's login (Settings > Calling > Phone login), the same as
 * tapping "Get from my PBX" and Save. Done by itself on first sign-in and kept current afterwards
 * (new server address, new phone password). A login the user typed for a different account is left
 * alone. Returns true when something changed.
 */
fun adoptPbxLogin(st: org.ownpbx.myphone.data.Store, pbx: org.ownpbx.myphone.data.SipInfo?): Boolean {
    if (pbx == null || !pbx.enabled || pbx.username.isBlank() || pbx.domain.isBlank()) return false
    val user = st.sipUserOverride.trim()
    val mine = user.isBlank() || user == pbx.username || user == pbx.main_username
    if (!mine) return false
    val server = "${pbx.domain}:${pbx.port}"
    if (st.sipServerOverride == server && user == pbx.username && st.sipPassOverride == pbx.password)
        return false
    st.sipServerOverride = server
    st.sipUserOverride = pbx.username
    st.sipPassOverride = pbx.password
    return true
}

@Composable
fun MainScreen(model: AppModel) {
    // Kept in the model so they survive the PIN lock (switching apps).
    var tab by model::tab
    var chatWith by model::chatWith
    var overview by remember { mutableStateOf<Overview?>(null) }
    val snack = remember { SnackbarHostState() }

    val ctx = LocalContext.current

    // Notifications: start the background check, and ask once (Android 13+).
    val askNotify =
        rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
            if (!ok)
                model.notice = "Notifications are off. You can turn them on in Android settings."
        }
    LaunchedEffect(Unit) {
        Notifier.schedule(ctx)
        // Phone login: fill it in from the PBX (first sign-in) or bring it up to date.
        model.guardQuiet { it.sip() }?.let { pbx ->
            model.sip = pbx
            if (adoptPbxLogin(model.store, pbx)) model.notice = "Phone login set up from your PBX"
        }
        org.ownpbx.myphone.LiveService.sync(ctx)
        if (
            Build.VERSION.SDK_INT >= 33 &&
                model.store.notifyOn &&
                !model.store.notifyAsked &&
                ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) !=
                    PackageManager.PERMISSION_GRANTED
        ) {
            model.store.notifyAsked = true
            try {
                askNotify.launch(Manifest.permission.POST_NOTIFICATIONS)
            } catch (_: Exception) {}
        }
    }
    // ---- calls: mic permission, phone settings, then dial.
    var pendingCall by remember { mutableStateOf<String?>(null) }
    /** The pending call (after the mic ask) is a push-to-talk call. */
    var pendingPtt by remember { mutableStateOf(false) }
    suspend fun startCall(number: String, ptt: Boolean = false) {
        val st = model.store
        val custom = st.sipUserOverride.isNotBlank() && st.sipPassOverride.isNotBlank()
        val fromPbx =
            model.sip?.takeIf { it.enabled }
                ?: (if (custom) model.guardQuiet { it.sip() } else model.guard { it.sip() })?.also {
                    model.sip = it
                }
        adoptPbxLogin(st, fromPbx)
        val sip = applySipOverrides(st, fromPbx)
        when {
            sip == null -> {} // guard already showed the error
            !sip.enabled -> model.notice = sip.reason.ifEmpty { "Calling isn't available yet." }
            else ->
                (if (ptt)
                        org.ownpbx.myphone.phone.Phone.callPtt(
                            ctx,
                            sip,
                            number,
                            keepListening = st.incomingCalls,
                        )
                    else
                        org.ownpbx.myphone.phone.Phone.call(
                            ctx,
                            sip,
                            number,
                            keepListening = st.incomingCalls,
                        ))
                    ?.let { model.notice = it }
        }
    }
    val askMic =
        rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
            val n = pendingCall
            val ptt = pendingPtt
            pendingCall = null
            pendingPtt = false
            if (ok) {
                if (ptt) model.pttCallRequest = n else model.callRequest = n
            } else model.notice = "Calls need the microphone. Allow it in Android settings."
        }
    LaunchedEffect(model.callRequest) {
        val number = model.callRequest ?: return@LaunchedEffect
        model.callRequest = null
        if (
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
        ) {
            startCall(number)
        } else {
            pendingCall = number
            pendingPtt = false
            try {
                askMic.launch(Manifest.permission.RECORD_AUDIO)
            } catch (_: Exception) {}
        }
    }
    LaunchedEffect(model.pttCallRequest) {
        val number = model.pttCallRequest ?: return@LaunchedEffect
        model.pttCallRequest = null
        if (
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
        ) {
            startCall(number, ptt = true)
        } else {
            pendingCall = number
            pendingPtt = true
            try {
                askMic.launch(Manifest.permission.RECORD_AUDIO)
            } catch (_: Exception) {}
        }
    }

    // A tapped notification: jump to that tab / chat.
    LaunchedEffect(model.openTarget) {
        val (what, who) = model.openTarget ?: return@LaunchedEffect
        model.openTarget = null
        when (what) {
            "calls" -> {
                chatWith = null
                tab = Tab.Calls
            }
            "voicemail" -> {
                chatWith = null
                tab = Tab.Voicemail
            }
            "messages" -> {
                tab = Tab.Messages
                chatWith = who
            }
            "faxes" -> {
                chatWith = null
                tab = Tab.Faxes
            }
        }
    }
    // What's on screen no longer needs a notification.
    LaunchedEffect(tab, chatWith) {
        when {
            chatWith != null -> Notifier.clear(ctx, "messages", chatWith)
            tab == Tab.Messages -> Notifier.clear(ctx, "messages")
            tab == Tab.Calls -> Notifier.clear(ctx, "calls")
            tab == Tab.Voicemail -> Notifier.clear(ctx, "voicemail")
            tab == Tab.Faxes -> Notifier.clear(ctx, "faxes")
            else -> {}
        }
    }

    LaunchedEffect(model.notice) {
        model.notice?.let {
            snack.showSnackbar(it)
            model.notice = null
        }
    }
    // Looking at Calls clears the missed-call badge (on the PBX, so it stays
    // cleared). Older PBXes without /me/calls/seen just keep the 24h count.
    suspend fun markCallsSeen() {
        if (overview?.missed_new == null) return
        model
            .guardQuiet { it.callsSeen() }
            ?.let { overview = overview?.copy(missed_new = 0, missed_24h = 0) }
    }
    LaunchedEffect(Unit) {
        while (true) {
            // Don't poll from the background: the notification service does that.
            if (!Notifier.appVisible) {
                delay(2_000)
                continue
            }
            model
                .guard { it.overview() }
                ?.let {
                    overview = it
                    // Texts are listed in the app while it's open: no notification for them later.
                    Notifier.seen(model.store, msgId = it.latest_message_id)
                }
            if (tab == Tab.Calls && (overview?.missed_new ?: 0) > 0) markCallsSeen()
            delay(30_000)
        }
    }
    LaunchedEffect(tab) { if (tab == Tab.Calls) markCallsSeen() }

    // A phone-login link opened from the camera: confirm before using it.
    model.pendingSip?.let { login ->
        AlertDialog(
            onDismissRequest = { model.pendingSip = null },
            title = { Text("Use this phone login?") },
            text = {
                Text(
                    "Calls from this app will use\n\nServer: ${login.server}\nUsername: ${login.user}\n\n" +
                        "Only say yes if this QR code came from your own My Phone page."
                )
            },
            confirmButton = {
                TextButton({
                    model.store.sipServerOverride = login.server
                    model.store.sipUserOverride = login.user
                    model.store.sipPassOverride = login.pass
                    model.sip = null
                    model.pendingSip = null
                    model.notice = "Phone login saved"
                }) {
                    Text("Use it")
                }
            },
            dismissButton = { TextButton({ model.pendingSip = null }) { Text("Cancel") } },
        )
    }

    // A call in progress covers everything; then the dial pad.
    org.ownpbx.myphone.phone.Phone.ui?.let { c ->
        InCallScreen(c)
        return
    }
    model.dialPad?.let { initial ->
        BackHandler { model.dialPad = null }
        DialPadScreen(
            initial,
            onCall = {
                model.dialPad = null
                model.callRequest = it
            },
            onClose = { model.dialPad = null },
        )
        return
    }

    val peer = chatWith
    if (peer != null) {
        BackHandler { chatWith = null }
        ChatScreen(model, peer, snack) { chatWith = null }
        return
    }
    BackHandler(enabled = tab != Tab.Messages) { tab = Tab.Messages }

    Scaffold(
        snackbarHost = { SnackbarHost(snack) },
        bottomBar = {
            // Faxes only for users a fax number rings (or who can send faxes).
            val tabs = Tab.entries.filter { it != Tab.Faxes || overview?.fax_enabled == true || tab == Tab.Faxes }
            NavigationBar {
                tabs.forEach { t ->
                    val badge =
                        when (t) {
                            Tab.Calls -> overview?.let { it.missed_new ?: it.missed_24h } ?: 0
                            Tab.Voicemail -> overview?.new_voicemail ?: 0
                            Tab.Faxes -> overview?.new_faxes ?: 0
                            else -> 0
                        }
                    NavigationBarItem(
                        selected = tab == t,
                        onClick = { tab = t },
                        label = { Text(t.label, maxLines = 1, softWrap = false) },
                        // Six tabs: labels only under the selected one, so they fit.
                        alwaysShowLabel = tabs.size <= 5,
                        icon = {
                            BadgedBox(badge = { if (badge > 0) Badge { Text("$badge") } }) {
                                Icon(t.icon, t.label)
                            }
                        },
                    )
                }
            }
        },
    ) { pad ->
        val m = Modifier.padding(pad)
        when (tab) {
            Tab.Messages -> MessagesScreen(model, snack, m) { chatWith = it }
            Tab.Calls -> CallsScreen(model, snack, m) { chatWith = it }
            Tab.Contacts -> ContactsScreen(model, snack, m)
            Tab.Voicemail -> VoicemailScreen(model, snack, m) { chatWith = it }
            Tab.Faxes -> FaxScreen(model, snack, m)
            Tab.Settings -> SettingsScreen(model, overview, m)
        }
    }
}
