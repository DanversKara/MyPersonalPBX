// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.ChatThread

@Composable
fun MessagesScreen(model: AppModel, snack: SnackbarHostState, modifier: Modifier, open: (String) -> Unit) {
    var threads by remember { mutableStateOf<List<ChatThread>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var selected by remember { mutableStateOf(setOf<String>()) }
    var hidden by remember { mutableStateOf(setOf<String>()) }   // swiped away, waiting for Undo
    var query by remember { mutableStateOf("") }
    var compose by remember { mutableStateOf(false) }
    var confirmBulk by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val haptic = LocalHapticFeedback.current

    suspend fun refresh() {
        model.guard { it.threads() }?.let { threads = it.threads }
        loading = false
    }
    LaunchedEffect(Unit) { while (true) { refresh(); delay(10_000) } }

    fun delete(peers: Set<String>) {
        hidden = hidden + peers
        selected = emptySet()
        scope.launch {
            val r = snack.showSnackbar(
                if (peers.size == 1) "Conversation deleted" else "${peers.size} conversations deleted",
                actionLabel = "Undo", duration = SnackbarDuration.Short)
            if (r == SnackbarResult.ActionPerformed) {
                hidden = hidden - peers
            } else {
                model.change { it.deleteMessages(peers = peers.toList()) }
                refresh()
                hidden = hidden - peers
            }
        }
    }

    val shown = threads.filter { it.peer !in hidden }
        .filter { query.isBlank() || it.peer.contains(query.filter { c -> c.isDigit() }.ifEmpty { query }) ||
            it.last.body.contains(query, ignoreCase = true) }

    Scaffold(
        modifier = modifier,
        topBar = {
            if (selected.isNotEmpty()) {
                TopAppBar(
                    title = { Text("${selected.size} selected") },
                    navigationIcon = { IconButton({ selected = emptySet() }) { Icon(Icons.Filled.Close, "Cancel") } },
                    actions = {
                        IconButton({ selected = shown.map { it.peer }.toSet() }) { Icon(Icons.Filled.SelectAll, "Select all") }
                        IconButton({ if (selected.size > 20) confirmBulk = true else delete(selected) }) {
                            Icon(Icons.Filled.Delete, "Delete")
                        }
                    },
                    colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.secondaryContainer),
                )
            } else {
                Column(Modifier.statusBarsPadding().padding(horizontal = 16.dp, vertical = 8.dp)) {
                    SearchBarField(query) { query = it }
                }
            }
        },
        floatingActionButton = {
            if (selected.isEmpty()) ExtendedFloatingActionButton(
                onClick = { compose = true }, icon = { Icon(Icons.Filled.Edit, null) }, text = { Text("Start chat") })
        },
    ) { pad ->
        PullToRefreshBox(isRefreshing = false, onRefresh = { scope.launch { refresh() } }, modifier = Modifier.padding(pad)) {
            when {
                loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                shown.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text(if (query.isBlank()) "No messages yet" else "Nothing matches",
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                else -> LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 88.dp)) {
                    items(shown, key = { it.peer }) { t ->
                        val isSel = t.peer in selected
                        val unread = !t.last.mine && t.last.id > model.store.seenFor(t.peer)
                        val dismiss = rememberSwipeToDismissBoxState(confirmValueChange = {
                            if (it == SwipeToDismissBoxValue.EndToStart) { delete(setOf(t.peer)); true } else false
                        })
                        SwipeToDismissBox(
                            state = dismiss, enableDismissFromStartToEnd = false,
                            backgroundContent = { SwipeDeleteBackground() },
                        ) {
                            ListItem(
                                modifier = Modifier.combinedClickable(
                                    onClick = {
                                        if (selected.isNotEmpty()) selected = if (isSel) selected - t.peer else selected + t.peer
                                        else open(t.peer)
                                    },
                                    onLongClick = {
                                        haptic.performHapticFeedback(HapticFeedbackType.LongPress)
                                        selected = selected + t.peer
                                    }),
                                colors = ListItemDefaults.colors(containerColor =
                                    if (isSel) MaterialTheme.colorScheme.secondaryContainer else MaterialTheme.colorScheme.surface),
                                leadingContent = { Avatar(t.peer, isSel) },
                                headlineContent = {
                                    Text(prettyNumber(t.peer), fontWeight = if (unread) FontWeight.Bold else FontWeight.Normal)
                                },
                                supportingContent = {
                                    Text((if (t.last.mine) "You: " else "") + t.last.body, maxLines = 1,
                                        overflow = TextOverflow.Ellipsis,
                                        fontWeight = if (unread) FontWeight.SemiBold else FontWeight.Normal)
                                },
                                trailingContent = {
                                    Column(horizontalAlignment = Alignment.End) {
                                        Text(shortTime(t.last.sent_at), style = MaterialTheme.typography.labelMedium)
                                        if (t.blocked) Icon(Icons.Filled.Block, "Blocked", Modifier.size(16.dp))
                                        else if (unread) Badge()
                                    }
                                },
                            )
                        }
                    }
                }
            }
        }
    }

    if (compose) NewChatDialog({ compose = false }) { compose = false; open(it) }
    if (confirmBulk) AlertDialog(
        onDismissRequest = { confirmBulk = false },
        title = { Text("Delete ${selected.size} conversations?") },
        text = { Text("They're removed from your view. The other person keeps their copy.") },
        confirmButton = { TextButton({ confirmBulk = false; delete(selected) }) { Text("Delete") } },
        dismissButton = { TextButton({ confirmBulk = false }) { Text("Cancel") } },
    )
}

@Composable
fun SearchBarField(value: String, onChange: (String) -> Unit) {
    OutlinedTextField(
        value, onChange, Modifier.fillMaxWidth(), singleLine = true,
        placeholder = { Text("Search messages") },
        leadingIcon = { Icon(Icons.Filled.Search, null) },
        trailingIcon = { if (value.isNotEmpty()) IconButton({ onChange("") }) { Icon(Icons.Filled.Close, "Clear") } },
        shape = MaterialTheme.shapes.extraLarge,
    )
}

@Composable
fun SwipeDeleteBackground() {
    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.errorContainer).padding(horizontal = 24.dp),
        contentAlignment = Alignment.CenterEnd) {
        Icon(Icons.Filled.Delete, "Delete", tint = MaterialTheme.colorScheme.onErrorContainer)
    }
}

@Composable
fun NewChatDialog(onDismiss: () -> Unit, onGo: (String) -> Unit) {
    var to by remember { mutableStateOf("") }
    val clean = to.filter { it.isDigit() || it == '+' }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("New message") },
        text = {
            OutlinedTextField(to, { to = it }, singleLine = true, label = { Text("Extension or phone number") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone))
        },
        confirmButton = { TextButton({ onGo(clean) }, enabled = clean.length >= 2) { Text("Chat") } },
        dismissButton = { TextButton(onDismiss) { Text("Cancel") } },
    )
}
