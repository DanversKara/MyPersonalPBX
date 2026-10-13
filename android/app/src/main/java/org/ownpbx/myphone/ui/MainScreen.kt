// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.compose.ui.platform.LocalContext
import org.ownpbx.myphone.Notifier
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Voicemail
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import kotlinx.coroutines.delay
import org.ownpbx.myphone.data.Overview

enum class Tab(val label: String, val icon: ImageVector) {
    Messages("Messages", Icons.Filled.Chat),
    Calls("Calls", Icons.Filled.Call),
    Voicemail("Voicemail", Icons.Filled.Voicemail),
    Settings("Settings", Icons.Filled.Settings),
}

@Composable
fun MainScreen(model: AppModel) {
    var tab by rememberSaveable { mutableStateOf(Tab.Messages) }
    var chatWith by rememberSaveable { mutableStateOf<String?>(null) }
    var overview by remember { mutableStateOf<Overview?>(null) }
    val snack = remember { SnackbarHostState() }

    val ctx = LocalContext.current

    // Notifications: start the background check, and ask once (Android 13+).
    val askNotify = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
        if (!ok) model.notice = "Notifications are off. You can turn them on in Android settings."
    }
    LaunchedEffect(Unit) {
        Notifier.schedule(ctx)
        org.ownpbx.myphone.LiveService.sync(ctx)
        if (Build.VERSION.SDK_INT >= 33 && model.store.notifyOn && !model.store.notifyAsked &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            model.store.notifyAsked = true
            try { askNotify.launch(Manifest.permission.POST_NOTIFICATIONS) } catch (_: Exception) {}
        }
    }
    // A tapped notification: jump to that tab / chat.
    LaunchedEffect(model.openTarget) {
        val (what, who) = model.openTarget ?: return@LaunchedEffect
        model.openTarget = null
        when (what) {
            "calls" -> { chatWith = null; tab = Tab.Calls }
            "voicemail" -> { chatWith = null; tab = Tab.Voicemail }
            "messages" -> { tab = Tab.Messages; chatWith = who }
        }
    }
    // What's on screen no longer needs a notification.
    LaunchedEffect(tab, chatWith) {
        when {
            chatWith != null -> Notifier.clear(ctx, "messages", chatWith)
            tab == Tab.Messages -> Notifier.clear(ctx, "messages")
            tab == Tab.Calls -> Notifier.clear(ctx, "calls")
            tab == Tab.Voicemail -> Notifier.clear(ctx, "voicemail")
            else -> {}
        }
    }

    LaunchedEffect(model.notice) {
        model.notice?.let { snack.showSnackbar(it); model.notice = null }
    }
    // Looking at Calls clears the missed-call badge (on the PBX, so it stays
    // cleared). Older PBXes without /me/calls/seen just keep the 24h count.
    suspend fun markCallsSeen() {
        if (overview?.missed_new == null) return
        model.guardQuiet { it.callsSeen() }?.let { overview = overview?.copy(missed_new = 0, missed_24h = 0) }
    }
    LaunchedEffect(Unit) {
        while (true) {
            // Don't poll from the background: the notification service does that.
            if (!Notifier.appVisible) { delay(2_000); continue }
            model.guard { it.overview() }?.let {
                overview = it
                // Texts are listed in the app while it's open: no notification for them later.
                Notifier.seen(model.store, msgId = it.latest_message_id)
            }
            if (tab == Tab.Calls && (overview?.missed_new ?: 0) > 0) markCallsSeen()
            delay(30_000)
        }
    }
    LaunchedEffect(tab) { if (tab == Tab.Calls) markCallsSeen() }

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
            NavigationBar {
                Tab.entries.forEach { t ->
                    val badge = when (t) {
                        Tab.Calls -> overview?.let { it.missed_new ?: it.missed_24h } ?: 0
                        Tab.Voicemail -> overview?.new_voicemail ?: 0
                        else -> 0
                    }
                    NavigationBarItem(
                        selected = tab == t, onClick = { tab = t }, label = { Text(t.label) },
                        icon = {
                            BadgedBox(badge = { if (badge > 0) Badge { Text("$badge") } }) { Icon(t.icon, t.label) }
                        })
                }
            }
        },
    ) { pad ->
        val m = Modifier.padding(pad)
        when (tab) {
            Tab.Messages -> MessagesScreen(model, snack, m) { chatWith = it }
            Tab.Calls -> CallsScreen(model, snack, m) { chatWith = it }
            Tab.Voicemail -> VoicemailScreen(model, snack, m) { chatWith = it }
            Tab.Settings -> SettingsScreen(model, overview, m)
        }
    }
}
