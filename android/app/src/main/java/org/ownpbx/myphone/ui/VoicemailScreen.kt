// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import android.media.AudioAttributes
import android.media.MediaPlayer
import android.net.Uri
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Vm

@Composable
fun VoicemailScreen(model: AppModel, snack: SnackbarHostState, modifier: Modifier) {
    var folder by rememberSaveable { mutableStateOf("INBOX") }
    var items by remember { mutableStateOf<List<Vm>?>(null) }
    var selected by remember { mutableStateOf(setOf<Long>()) }
    var hidden by remember { mutableStateOf(setOf<Long>()) }
    var playing by remember { mutableStateOf<Long?>(null) }
    var progress by remember { mutableFloatStateOf(0f) }
    val player = remember { mutableStateOf<MediaPlayer?>(null) }
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()

    suspend fun refresh() { model.guard { it.voicemail(folder) }?.let { items = it.voicemail } }
    LaunchedEffect(folder) { items = null; selected = emptySet(); refresh() }
    DisposableEffect(Unit) { onDispose { player.value?.release() } }
    LaunchedEffect(playing) {
        while (playing != null) {
            player.value?.let { if (it.duration > 0) progress = it.currentPosition.toFloat() / it.duration }
            delay(250)
        }
    }

    fun stop() { player.value?.release(); player.value = null; playing = null; progress = 0f }

    fun play(v: Vm) {
        if (playing == v.id) { stop(); return }
        stop()
        val api = model.api()
        val mp = MediaPlayer()
        mp.setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
        try {
            mp.setDataSource(ctx, Uri.parse(api.voicemailAudioUrl(v.id)), api.authHeaders())
        } catch (e: Exception) { model.notice = "Can't play this message."; return }
        mp.setOnPreparedListener { it.start() }
        mp.setOnCompletionListener { stop() }
        mp.setOnErrorListener { _, _, _ -> model.notice = "Can't play this message."; stop(); true }
        mp.prepareAsync()
        player.value = mp
        playing = v.id
        if (v.is_read == 0) scope.launch { model.guard { it.heard(v.id) } }
    }

    fun delete(ids: Set<Long>) {
        if (playing in ids) stop()
        hidden = hidden + ids; selected = emptySet()
        scope.launch {
            val r = snack.showSnackbar(if (ids.size == 1) "Voicemail deleted" else "${ids.size} voicemails deleted",
                actionLabel = "Undo", duration = SnackbarDuration.Short)
            if (r != SnackbarResult.ActionPerformed) { model.change { it.deleteVoicemail(ids.toList(), folder) }; refresh() }
            hidden = hidden - ids
        }
    }

    val shown = items?.filter { it.id !in hidden }

    Scaffold(modifier = modifier, topBar = {
        if (selected.isNotEmpty()) TopAppBar(
            title = { Text("${selected.size} selected") },
            navigationIcon = { IconButton({ selected = emptySet() }) { Icon(Icons.Filled.Close, "Cancel") } },
            actions = {
                IconButton({ selected = shown.orEmpty().map { it.id }.toSet() }) { Icon(Icons.Filled.SelectAll, "Select all") }
                IconButton({ delete(selected) }) { Icon(Icons.Filled.Delete, "Delete") }
            },
            colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.secondaryContainer),
        ) else Row(Modifier.statusBarsPadding().padding(16.dp, 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Voicemail", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.weight(1f))
            FilterChip(folder == "INBOX", { folder = "INBOX" }, { Text("New") })
            FilterChip(folder == "Old", { folder = "Old" }, { Text("Old") })
        }
    }) { pad ->
        PullToRefreshBox(false, { scope.launch { refresh() } }, Modifier.padding(pad).fillMaxSize()) {
            when {
                shown == null -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                shown.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("No voicemail", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                else -> LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(shown, key = { it.id }) { v ->
                        val isSel = v.id in selected
                        val dismiss = rememberSwipeToDismissBoxState(confirmValueChange = {
                            if (it == SwipeToDismissBoxValue.EndToStart) { delete(setOf(v.id)); true } else false
                        })
                        SwipeToDismissBox(dismiss, backgroundContent = { SwipeDeleteBackground() },
                            enableDismissFromStartToEnd = false) {
                            ElevatedCard(
                                Modifier.fillMaxWidth().combinedClickable(
                                    onClick = { if (selected.isNotEmpty()) selected = if (isSel) selected - v.id else selected + v.id },
                                    onLongClick = { selected = selected + v.id }),
                                colors = if (isSel) CardDefaults.elevatedCardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)
                                else CardDefaults.elevatedCardColors(),
                            ) {
                                Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Avatar(v.caller, isSel)
                                    Spacer(Modifier.width(12.dp))
                                    Column(Modifier.weight(1f)) {
                                        Text(prettyNumber(v.caller.ifEmpty { "Unknown" }),
                                            fontWeight = if (v.is_read == 0) FontWeight.Bold else FontWeight.Normal)
                                        Text("${shortTime(v.received_at)} · ${duration(v.duration_sec)}",
                                            style = MaterialTheme.typography.bodySmall)
                                    }
                                    if (folder == "INBOX") IconButton({
                                        scope.launch { model.change { it.moveVoicemail(v.id, "Old") }; refresh() }
                                    }) { Icon(Icons.Filled.Archive, "Move to Old") }
                                    FilledTonalIconButton({ play(v) }) {
                                        Icon(if (playing == v.id) Icons.Filled.Stop else Icons.Filled.PlayArrow,
                                            if (playing == v.id) "Stop" else "Play")
                                    }
                                }
                                if (playing == v.id) LinearProgressIndicator({ progress },
                                    Modifier.fillMaxWidth().padding(horizontal = 12.dp).padding(bottom = 12.dp))
                            }
                        }
                    }
                }
            }
        }
    }
}
