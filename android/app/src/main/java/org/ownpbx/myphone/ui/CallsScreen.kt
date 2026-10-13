// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.CallMade
import androidx.compose.material.icons.automirrored.filled.CallMissed
import androidx.compose.material.icons.automirrored.filled.CallReceived
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Call

/** Call history. Users can't delete calls (the PBX keeps them 90 days, then
 *  removes them), but they can hide them; hidden calls live under "Hidden". */
@Composable
fun CallsScreen(model: AppModel, snack: SnackbarHostState, modifier: Modifier, text: (String) -> Unit) {
    var view by rememberSaveable { mutableStateOf("all") }
    var calls by remember { mutableStateOf<List<Call>?>(null) }
    var selected by remember { mutableStateOf(setOf<Long>()) }
    var menuFor by remember { mutableStateOf<Long?>(null) }
    val scope = rememberCoroutineScope()
    val hiddenView = view == "hidden"

    suspend fun refresh() { model.guard { it.calls(view) }?.let { calls = it.calls } }
    LaunchedEffect(view) { calls = null; selected = emptySet(); refresh() }

    /** Hide (or, in the Hidden view, show again) right away; Undo reverses it. */
    fun move(ids: Set<Long>) {
        if (ids.isEmpty()) return
        selected = emptySet()
        val unhide = hiddenView
        val what = if (ids.size == 1) "Call" else "${ids.size} calls"
        model.doThenUndo(snack, if (unhide) "$what moved back" else "$what hidden", ids.map { "call:$it" }.toSet(),
            commit = { it.hideCalls(ids, unhide = unhide) },
            revert = { it.hideCalls(ids, unhide = !unhide) },
            after = { refresh() })
    }

    val shown = calls?.filter { "call:${it.id}" !in model.gone }

    Scaffold(modifier = modifier, topBar = {
        if (selected.isNotEmpty()) TopAppBar(
            title = { Text("${selected.size} selected") },
            navigationIcon = { IconButton({ selected = emptySet() }) { Icon(Icons.Filled.Close, "Cancel") } },
            actions = {
                IconButton({ selected = shown.orEmpty().map { it.id }.toSet() }) { Icon(Icons.Filled.SelectAll, "Select all") }
                IconButton({ move(selected) }) {
                    if (hiddenView) Icon(Icons.Filled.Visibility, "Show again") else Icon(Icons.Filled.VisibilityOff, "Hide")
                }
            },
            colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.secondaryContainer),
        ) else Column(Modifier.statusBarsPadding().padding(16.dp, 8.dp)) {
            Text("Calls", style = MaterialTheme.typography.headlineSmall)
            Row(Modifier.padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(view == "all", { view = "all" }, { Text("All") })
                FilterChip(view == "missed", { view = "missed" }, { Text("Missed") })
                FilterChip(view == "hidden", { view = "hidden" }, { Text("Hidden") },
                    leadingIcon = { Icon(Icons.Filled.VisibilityOff, null, Modifier.size(16.dp)) })
            }
        }
    }) { pad ->
        PullToRefreshBox(false, { scope.launch { refresh() } }, Modifier.padding(pad).fillMaxSize()) {
            val list = shown
            when {
                list == null -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                list.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(if (hiddenView) "No hidden calls" else "No calls",
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        if (hiddenView) Text("Swipe a call left to hide it.", style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
                else -> LazyColumn(Modifier.fillMaxSize()) {
                    if (hiddenView) item {
                        Text("Hidden calls stay here until the PBX removes them (90 days after the call).",
                            Modifier.padding(16.dp, 4.dp), style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    items(list, key = { it.id }) { c ->
                        val isSel = c.id in selected
                        val dismiss = rememberSwipeToDismissBoxState(confirmValueChange = {
                            if (it == SwipeToDismissBoxValue.EndToStart) { move(setOf(c.id)); true } else false
                        })
                        SwipeToDismissBox(dismiss, enableDismissFromStartToEnd = false, backgroundContent = {
                            SwipeActionBackground(
                                if (hiddenView) Icons.Filled.Visibility else Icons.Filled.VisibilityOff,
                                if (hiddenView) "Show" else "Hide")
                        }) {
                            CallRow(c, isSel,
                                onClick = {
                                    if (selected.isNotEmpty()) selected = if (isSel) selected - c.id else selected + c.id
                                    else menuFor = c.id
                                },
                                onLongClick = { selected = selected + c.id }) {
                                DropdownMenu(menuFor == c.id, { menuFor = null }) {
                                    DropdownMenuItem({ Text("Send a text") }, { menuFor = null; text(c.other) },
                                        leadingIcon = { Icon(Icons.Filled.Chat, null) })
                                    DropdownMenuItem({ Text(if (hiddenView) "Show in history" else "Hide") },
                                        { menuFor = null; move(setOf(c.id)) },
                                        leadingIcon = {
                                            Icon(if (hiddenView) Icons.Filled.Visibility else Icons.Filled.VisibilityOff, null)
                                        })
                                    DropdownMenuItem({ Text("Block this number") }, {
                                        menuFor = null; scope.launch { if (model.change { it.block(c.other) }) model.notice = "Blocked" }
                                    }, leadingIcon = { Icon(Icons.Filled.Block, null) })
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun CallRow(c: Call, isSel: Boolean, onClick: () -> Unit, onLongClick: () -> Unit, menu: @Composable () -> Unit) {
    val (icon, label, bad) = when (c.kind) {
        "out" -> Triple(Icons.AutoMirrored.Filled.CallMade, "Outgoing", false)
        "in" -> Triple(Icons.AutoMirrored.Filled.CallReceived, "Incoming", false)
        "vm" -> Triple(Icons.Filled.Voicemail, "Voicemail", true)
        else -> Triple(Icons.AutoMirrored.Filled.CallMissed, "Missed", true)
    }
    ListItem(
        modifier = Modifier.combinedClickable(onClick = onClick, onLongClick = onLongClick),
        colors = if (isSel) ListItemDefaults.colors(containerColor = MaterialTheme.colorScheme.secondaryContainer)
        else ListItemDefaults.colors(),
        leadingContent = { Avatar(c.other, isSel) },
        headlineContent = { Text(prettyNumber(c.other)) },
        supportingContent = {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(icon, label, Modifier.size(16.dp),
                    tint = if (bad) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.width(6.dp))
                Text(label + if (c.kind == "in" || c.kind == "out") " · ${duration(c.bill_sec ?: c.duration_sec)}" else "")
            }
        },
        trailingContent = {
            Box {
                Text(shortTime(c.started_at, utc = false), style = MaterialTheme.typography.labelMedium)
                menu()
            }
        },
    )
}

/** Red/grey strip behind a row being swiped, with an icon and a word. */
@Composable
fun SwipeActionBackground(icon: ImageVector, label: String) {
    Row(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.surfaceVariant).padding(horizontal = 24.dp),
        horizontalArrangement = Arrangement.End, verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(8.dp))
        Icon(icon, null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
