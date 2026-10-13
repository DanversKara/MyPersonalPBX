// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Call
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Voicemail
import androidx.compose.material3.*
import androidx.compose.runtime.*
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

    LaunchedEffect(model.notice) {
        model.notice?.let { snack.showSnackbar(it); model.notice = null }
    }
    LaunchedEffect(Unit) {
        while (true) {
            model.guard { it.overview() }?.let { overview = it }
            delay(30_000)
        }
    }

    chatWith?.let { peer ->
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
                        Tab.Calls -> overview?.missed_24h ?: 0
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
            Tab.Calls -> CallsScreen(model, m) { chatWith = it }
            Tab.Voicemail -> VoicemailScreen(model, snack, m)
            Tab.Settings -> SettingsScreen(model, overview, m)
        }
    }
}
