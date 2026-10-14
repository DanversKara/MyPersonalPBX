// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.PlaybackParams
import androidx.compose.animation.animateContentSize
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.ownpbx.myphone.Notifier
import org.ownpbx.myphone.data.ApiException
import org.ownpbx.myphone.data.Vm
import java.io.File

private val SPEEDS = listOf(1f, 1.5f, 2f)

/** What the player is doing for the open message. */
private enum class PlayState { Idle, Loading, Playing, Paused, Error }

@Composable
fun VoicemailScreen(model: AppModel, snack: SnackbarHostState, modifier: Modifier, text: (String) -> Unit) {
    var folder by rememberSaveable { mutableStateOf("INBOX") }
    var items by remember { mutableStateOf<List<Vm>?>(null) }
    var selected by remember { mutableStateOf(setOf<Long>()) }
    var openId by remember { mutableStateOf<Long?>(null) }
    var state by remember { mutableStateOf(PlayState.Idle) }
    var errorText by remember { mutableStateOf("") }
    var posMs by remember { mutableIntStateOf(0) }
    var lenMs by remember { mutableIntStateOf(0) }
    var seeking by remember { mutableStateOf<Float?>(null) }
    var speed by rememberSaveable { mutableFloatStateOf(1f) }
    val player = remember { mutableStateOf<MediaPlayer?>(null) }
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val cacheDir = remember { File(ctx.cacheDir, "voicemail").apply { mkdirs() } }

    suspend fun refresh() {
        model.guard { it.voicemail(folder) }?.let { r ->
            items = r.voicemail
            if (folder == "INBOX") Notifier.seen(model.store, vmId = r.voicemail.maxOfOrNull { it.id } ?: 0)
        }
    }
    LaunchedEffect(folder) { items = null; selected = emptySet(); refresh() }

    fun release() {
        player.value?.release(); player.value = null
        state = PlayState.Idle; posMs = 0; lenMs = 0
    }
    DisposableEffect(Unit) { onDispose { player.value?.release(); player.value = null } }

    LaunchedEffect(state) {
        while (state == PlayState.Playing) {
            player.value?.let { if (seeking == null) posMs = it.currentPosition }
            delay(200)
        }
    }

    fun applySpeed(mp: MediaPlayer) {
        // Setting params on a paused player would start it, so only while playing.
        try { if (mp.isPlaying) mp.playbackParams = PlaybackParams().setSpeed(speed) } catch (_: Exception) {}
    }

    /** Download (once, cached) with the app's own connection, then play the local file. */
    fun open(v: Vm) {
        if (openId == v.id) {
            val mp = player.value
            when {
                mp == null -> {}
                state == PlayState.Playing -> { mp.pause(); state = PlayState.Paused; return }
                state == PlayState.Paused -> { mp.start(); applySpeed(mp); state = PlayState.Playing; return }
            }
        }
        release()
        openId = v.id
        state = PlayState.Loading
        scope.launch {
            val f = File(cacheDir, "${v.id}.wav")
            try {
                if (!f.exists() || f.length() < 44) model.api().downloadVoicemail(v.id, f)
            } catch (e: ApiException) {
                if (e.status == 401) { model.signedOut("You were signed out. Please sign in again."); return@launch }
                errorText = if (e.status == 404) "This message's audio isn't on the PBX any more."
                else "Couldn't download the message (error ${e.status})."
                state = PlayState.Error; return@launch
            } catch (e: Exception) {
                errorText = "Couldn't download the message. Check your connection."
                state = PlayState.Error; return@launch
            }
            if (openId != v.id) return@launch
            val mp = MediaPlayer()
            try {
                mp.setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                mp.setDataSource(f.path)
                mp.setOnCompletionListener { posMs = lenMs; state = PlayState.Paused; it.seekTo(0); posMs = 0 }
                mp.setOnErrorListener { _, what, extra ->
                    errorText = "This phone can't play the message (code $what/$extra)."
                    f.delete(); state = PlayState.Error; true
                }
                mp.prepare()
                lenMs = mp.duration.coerceAtLeast(0)
                mp.start(); applySpeed(mp)
                player.value = mp
                state = PlayState.Playing
                if (v.is_read == 0) {
                    model.guardQuiet { it.heard(v.id) }
                    items = items?.map { if (it.id == v.id) it.copy(is_read = 1) else it }
                }
            } catch (e: Exception) {
                mp.release()
                f.delete()
                errorText = "This phone can't play the message."
                state = PlayState.Error
            }
        }
    }

    fun close() { release(); openId = null }

    fun delete(ids: Set<Long>) {
        if (openId in ids) close()
        selected = emptySet()
        val inFolder = folder
        model.undoable(snack, if (ids.size == 1) "Voicemail deleted" else "${ids.size} voicemails deleted",
            ids.map { "vm:$it" }.toSet(),
            commit = { api ->
                api.deleteVoicemail(ids.toList(), inFolder).also { r ->
                    if (r.ok) ids.forEach { File(cacheDir, "$it.wav").delete() }
                }
            },
            after = { refresh() })
    }

    fun moveTo(v: Vm, to: String) {
        if (openId == v.id) close()
        model.doThenUndo(snack, if (to == "Old") "Moved to Old" else "Moved to New", setOf("vm:${v.id}"),
            commit = { it.moveVoicemail(v.id, to) },
            revert = { it.moveVoicemail(v.id, if (to == "Old") "INBOX" else "Old") },
            after = { refresh() })
    }

    val shown = items?.filter { "vm:${it.id}" !in model.gone }

    Scaffold(modifier = modifier, topBar = {
        if (selected.isNotEmpty()) TopAppBar(
            title = { Text("${selected.size} selected") },
            navigationIcon = { IconButton({ selected = emptySet() }) { Icon(Icons.Filled.Close, "Cancel") } },
            actions = {
                IconButton({ selected = shown.orEmpty().map { it.id }.toSet() }) { Icon(Icons.Filled.SelectAll, "Select all") }
                IconButton({ delete(selected) }) { Icon(Icons.Filled.Delete, "Delete") }
            },
            colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.secondaryContainer),
        ) else Column(Modifier.statusBarsPadding().padding(16.dp, 8.dp)) {
            Text("Voicemail", style = MaterialTheme.typography.headlineSmall)
            Row(Modifier.padding(top = 4.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(folder == "INBOX", { close(); folder = "INBOX" }, { Text("New") })
                FilterChip(folder == "Old", { close(); folder = "Old" }, { Text("Old") })
            }
        }
    }) { pad ->
        PullToRefreshBox(false, { scope.launch { refresh() } }, Modifier.padding(pad).fillMaxSize()) {
            when {
                shown == null -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                shown.isEmpty() -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(Icons.Filled.Voicemail, null, Modifier.size(48.dp), tint = MaterialTheme.colorScheme.outline)
                        Spacer(Modifier.height(8.dp))
                        Text(if (folder == "INBOX") "No new voicemail" else "No old voicemail",
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
                else -> LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(12.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    items(shown, key = { it.id }) { v ->
                        val isSel = v.id in selected
                        val isOpen = openId == v.id
                        val dismiss = rememberSwipeToDismissBoxState(confirmValueChange = {
                            if (it == SwipeToDismissBoxValue.EndToStart) { delete(setOf(v.id)); true } else false
                        })
                        SwipeToDismissBox(dismiss, backgroundContent = { SwipeDeleteBackground() },
                            enableDismissFromStartToEnd = false, gesturesEnabled = !isOpen) {
                            ElevatedCard(
                                Modifier.fillMaxWidth().animateContentSize().combinedClickable(
                                    onClick = {
                                        when {
                                            selected.isNotEmpty() -> selected = if (isSel) selected - v.id else selected + v.id
                                            isOpen -> close()
                                            else -> open(v)
                                        }
                                    },
                                    onLongClick = { selected = selected + v.id }),
                                colors = if (isSel) CardDefaults.elevatedCardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)
                                else CardDefaults.elevatedCardColors(),
                            ) {
                                Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                                    Avatar(v.caller, isSel)
                                    Spacer(Modifier.width(12.dp))
                                    Column(Modifier.weight(1f)) {
                                        Row(verticalAlignment = Alignment.CenterVertically) {
                                            Text(prettyNumber(v.caller.ifEmpty { "Unknown caller" }),
                                                fontWeight = if (v.is_read == 0) FontWeight.Bold else FontWeight.Normal)
                                            if (v.is_read == 0) {
                                                Spacer(Modifier.width(8.dp))
                                                Badge(containerColor = MaterialTheme.colorScheme.primary) { Text("New") }
                                            }
                                        }
                                        Text("${shortTime(v.received_at)} · ${clock(v.duration_sec * 1000)}",
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                                    }
                                    FilledTonalIconButton({ open(v) }) {
                                        when {
                                            isOpen && state == PlayState.Loading ->
                                                CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                                            isOpen && state == PlayState.Playing -> Icon(Icons.Filled.Pause, "Pause")
                                            else -> Icon(Icons.Filled.PlayArrow, "Play")
                                        }
                                    }
                                }
                                if (isOpen) Player(
                                    state = state, errorText = errorText,
                                    posMs = seeking?.let { (it * lenMs).toInt() } ?: posMs,
                                    lenMs = if (lenMs > 0) lenMs else v.duration_sec * 1000,
                                    speed = speed,
                                    onSeek = { seeking = it },
                                    onSeekDone = {
                                        seeking?.let { f -> player.value?.seekTo((f * lenMs).toInt()); posMs = (f * lenMs).toInt() }
                                        seeking = null
                                    },
                                    onSpeed = {
                                        speed = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.size]
                                        player.value?.let { applySpeed(it) }
                                    },
                                    onRetry = { File(cacheDir, "${v.id}.wav").delete(); open(v) },
                                    onText = { close(); text(v.caller) },
                                    onCall = { close(); model.callRequest = v.caller },
                                    textable = v.caller.any { it.isDigit() },
                                    folder = folder,
                                    onMove = { moveTo(v, if (folder == "INBOX") "Old" else "INBOX") },
                                    onBlock = { scope.launch { if (model.change { it.block(v.caller) }) model.notice = "Blocked" } },
                                    onDelete = { delete(setOf(v.id)) },
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun Player(
    state: PlayState, errorText: String, posMs: Int, lenMs: Int, speed: Float,
    onSeek: (Float) -> Unit, onSeekDone: () -> Unit, onSpeed: () -> Unit, onRetry: () -> Unit,
    onText: () -> Unit, textable: Boolean, folder: String, onCall: () -> Unit,
    onMove: () -> Unit, onBlock: () -> Unit, onDelete: () -> Unit,
) {
    Column(Modifier.fillMaxWidth().padding(start = 12.dp, end = 12.dp, bottom = 8.dp)) {
        if (state == PlayState.Error) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Filled.ErrorOutline, null, tint = MaterialTheme.colorScheme.error)
                Spacer(Modifier.width(8.dp))
                Text(errorText, Modifier.weight(1f), color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall)
                TextButton(onRetry) { Text("Retry") }
            }
        } else {
            Slider(
                value = if (lenMs > 0) (posMs.toFloat() / lenMs).coerceIn(0f, 1f) else 0f,
                onValueChange = onSeek, onValueChangeFinished = onSeekDone,
                enabled = state == PlayState.Playing || state == PlayState.Paused,
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(clock(posMs), style = MaterialTheme.typography.labelMedium)
                Spacer(Modifier.weight(1f))
                AssistChip(onSpeed, { Text(if (speed == 1f) "1x" else "${speed}x") },
                    leadingIcon = { Icon(Icons.Filled.Speed, null, Modifier.size(16.dp)) })
                Spacer(Modifier.weight(1f))
                Text(clock(lenMs), style = MaterialTheme.typography.labelMedium)
            }
        }
        HorizontalDivider(Modifier.padding(vertical = 4.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceEvenly) {
            if (textable) VmAction(Icons.Filled.Call, "Call", onCall)
            if (textable) VmAction(Icons.Filled.Chat, "Text", onText)
            VmAction(if (folder == "INBOX") Icons.Filled.Archive else Icons.Filled.Unarchive,
                if (folder == "INBOX") "Move to Old" else "Move to New", onMove)
            if (textable) VmAction(Icons.Filled.Block, "Block", onBlock)
            VmAction(Icons.Filled.Delete, "Delete", onDelete)
        }
    }
}

@Composable
private fun VmAction(icon: androidx.compose.ui.graphics.vector.ImageVector, label: String, onClick: () -> Unit) {
    TextButton(onClick) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Icon(icon, null)
            Text(label, style = MaterialTheme.typography.labelSmall)
        }
    }
}

/** 75000 -> "1:15". */
private fun clock(ms: Int): String {
    val s = (ms.coerceAtLeast(0) + 500) / 1000
    return "${s / 60}:${(s % 60).toString().padStart(2, '0')}"
}
