// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.compose.foundation.clickable
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Call

@Composable
fun CallsScreen(model: AppModel, modifier: Modifier, text: (String) -> Unit) {
    var missed by rememberSaveable { mutableStateOf(false) }
    var calls by remember { mutableStateOf<List<Call>?>(null) }
    var menuFor by remember { mutableStateOf<Long?>(null) }
    val scope = rememberCoroutineScope()

    suspend fun refresh() { model.guard { it.calls(missed) }?.let { calls = it.calls } }
    LaunchedEffect(missed) { calls = null; refresh() }

    Column(modifier.fillMaxSize()) {
        Row(Modifier.statusBarsPadding().padding(16.dp, 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Calls", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.weight(1f))
            FilterChip(!missed, { missed = false }, { Text("All") })
            FilterChip(missed, { missed = true }, { Text("Missed") })
        }
        PullToRefreshBox(false, { scope.launch { refresh() } }, Modifier.fillMaxSize()) {
            val list = calls
            when {
                list == null -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                list.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("No calls", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                else -> LazyColumn(Modifier.fillMaxSize()) {
                    items(list, key = { it.id }) { c ->
                        val (icon, label, bad) = when (c.kind) {
                            "out" -> Triple(Icons.AutoMirrored.Filled.CallMade, "Outgoing", false)
                            "in" -> Triple(Icons.AutoMirrored.Filled.CallReceived, "Incoming", false)
                            "vm" -> Triple(Icons.Filled.Voicemail, "Voicemail", true)
                            else -> Triple(Icons.AutoMirrored.Filled.CallMissed, "Missed", true)
                        }
                        ListItem(
                            modifier = Modifier.clickable { menuFor = c.id },
                            leadingContent = { Avatar(c.other) },
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
                                    DropdownMenu(menuFor == c.id, { menuFor = null }) {
                                        DropdownMenuItem({ Text("Send a text") }, { menuFor = null; text(c.other) },
                                            leadingIcon = { Icon(Icons.Filled.Chat, null) })
                                        DropdownMenuItem({ Text("Block this number") }, {
                                            menuFor = null; scope.launch { if (model.change { it.block(c.other) }) model.notice = "Blocked" }
                                        }, leadingIcon = { Icon(Icons.Filled.Block, null) })
                                    }
                                }
                            },
                        )
                    }
                }
            }
        }
    }
}
