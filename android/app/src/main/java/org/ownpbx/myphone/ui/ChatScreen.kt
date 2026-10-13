// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Msg

@Composable
fun ChatScreen(model: AppModel, peer: String, snack: SnackbarHostState, back: () -> Unit) {
    var msgs by remember { mutableStateOf<List<Msg>>(emptyList()) }   // newest first
    var text by rememberSaveable { mutableStateOf("") }
    var sending by remember { mutableStateOf(false) }
    var selected by remember { mutableStateOf(setOf<Long>()) }
    var menu by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val list = rememberLazyListState()
    val clip = LocalClipboardManager.current
    val haptic = LocalHapticFeedback.current

    suspend fun refresh() {
        model.guard { it.thread(peer) }?.let {
            msgs = it.messages
            it.messages.firstOrNull()?.let { m -> model.store.markSeen(peer, m.id) }
        }
    }
    LaunchedEffect(peer) { while (true) { if (org.ownpbx.myphone.Notifier.appVisible) refresh(); delay(5_000) } }

    fun send() {
        val body = text.trim()
        if (body.isEmpty() || sending) return
        sending = true
        scope.launch {
            if (model.change { it.send(peer, body) }) { text = ""; refresh(); list.animateScrollToItem(0) }
            sending = false
        }
    }

    Scaffold(
        snackbarHost = { SnackbarHost(snack) },
        topBar = {
            if (selected.isNotEmpty()) TopAppBar(
                title = { Text("${selected.size} selected") },
                navigationIcon = { IconButton({ selected = emptySet() }) { Icon(Icons.Filled.Close, "Cancel") } },
                actions = {
                    IconButton({
                        clip.setText(AnnotatedString(msgs.filter { it.id in selected }.reversed().joinToString("\n") { it.body }))
                        selected = emptySet()
                    }) { Icon(Icons.Filled.ContentCopy, "Copy") }
                    IconButton({
                        val ids = selected.toList(); selected = emptySet()
                        model.bg.launch { model.change { it.deleteMessages(ids = ids) }; refresh() }
                    }) { Icon(Icons.Filled.Delete, "Delete") }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.secondaryContainer),
            ) else TopAppBar(
                navigationIcon = { IconButton(back) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") } },
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Avatar(peer); Spacer(Modifier.width(12.dp)); Text(prettyNumber(peer))
                    }
                },
                actions = {
                    IconButton({ menu = true }) { Icon(Icons.Filled.MoreVert, "More") }
                    DropdownMenu(menu, { menu = false }) {
                        DropdownMenuItem({ Text("Block") }, leadingIcon = { Icon(Icons.Filled.Block, null) }, onClick = {
                            menu = false; scope.launch { model.change { it.block(peer) }.also { if (it) model.notice = "Blocked" } }
                        })
                        DropdownMenuItem({ Text("Delete conversation") }, leadingIcon = { Icon(Icons.Filled.Delete, null) }, onClick = {
                            menu = false; model.bg.launch { if (model.change { it.deleteMessages(peers = listOf(peer)) }) back() }
                        })
                    }
                },
            )
        },
        bottomBar = {
            Row(Modifier.fillMaxWidth().navigationBarsPadding().imePadding().padding(8.dp),
                verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(text, { text = it.take(1000) }, Modifier.weight(1f), maxLines = 5,
                    placeholder = { Text("Text message") }, shape = RoundedCornerShape(24.dp))
                Spacer(Modifier.width(8.dp))
                FilledIconButton(::send, Modifier.size(52.dp), enabled = text.isNotBlank() && !sending) {
                    Icon(Icons.AutoMirrored.Filled.Send, "Send")
                }
            }
        },
    ) { pad ->
        LazyColumn(Modifier.padding(pad).fillMaxSize(), state = list, reverseLayout = true,
            contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            items(msgs, key = { it.id }) { m ->
                val isSel = m.id in selected
                Row(Modifier.fillMaxWidth(), horizontalArrangement = if (m.mine) Arrangement.End else Arrangement.Start) {
                    Surface(
                        shape = RoundedCornerShape(20.dp, 20.dp, if (m.mine) 4.dp else 20.dp, if (m.mine) 20.dp else 4.dp),
                        color = when {
                            isSel -> MaterialTheme.colorScheme.tertiaryContainer
                            m.mine -> MaterialTheme.colorScheme.primary
                            else -> MaterialTheme.colorScheme.surfaceVariant
                        },
                        modifier = Modifier.widthIn(max = 300.dp).combinedClickable(
                            onClick = { if (selected.isNotEmpty()) selected = if (isSel) selected - m.id else selected + m.id },
                            onLongClick = { haptic.performHapticFeedback(HapticFeedbackType.LongPress); selected = selected + m.id }),
                    ) {
                        Column(Modifier.padding(horizontal = 14.dp, vertical = 8.dp)) {
                            Text(m.body)
                            Text(shortTime(m.sent_at) + if (m.via_did.isNotEmpty()) " · via ${prettyNumber(m.via_did)}" else "",
                                style = MaterialTheme.typography.labelSmall)
                        }
                    }
                }
            }
        }
    }
}
