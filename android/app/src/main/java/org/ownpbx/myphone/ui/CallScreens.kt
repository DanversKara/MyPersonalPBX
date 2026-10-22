// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package org.ownpbx.myphone.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.Backspace
import androidx.compose.material.icons.automirrored.filled.VolumeUp
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import org.ownpbx.myphone.phone.CallUi
import org.ownpbx.myphone.phone.Phone

private val KEYS =
    listOf(
        "1" to "",
        "2" to "ABC",
        "3" to "DEF",
        "4" to "GHI",
        "5" to "JKL",
        "6" to "MNO",
        "7" to "PQRS",
        "8" to "TUV",
        "9" to "WXYZ",
        "*" to "",
        "0" to "+",
        "#" to "",
    )

@Composable
private fun Keypad(onKey: (Char) -> Unit, onLongZero: (() -> Unit)? = null) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        KEYS.chunked(3).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(18.dp)) {
                row.forEach { (k, letters) ->
                    Surface(
                        shape = CircleShape,
                        tonalElevation = 2.dp,
                        modifier = Modifier.size(72.dp).clip(CircleShape).clickable { onKey(k[0]) },
                    ) {
                        Column(
                            horizontalAlignment = Alignment.CenterHorizontally,
                            verticalArrangement = Arrangement.Center,
                        ) {
                            Text(k, fontSize = 28.sp)
                            if (letters.isNotEmpty())
                                Text(
                                    letters,
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                        }
                    }
                }
            }
        }
    }
}

/** Full-screen dial pad. */
@Composable
fun DialPadScreen(initial: String = "", onCall: (String) -> Unit, onClose: () -> Unit) {
    var number by remember { mutableStateOf(initial) }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Dial") },
                navigationIcon = {
                    IconButton(onClose) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                },
            )
        }
    ) { pad ->
        Column(
            Modifier.padding(pad).fillMaxSize().padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Spacer(Modifier.weight(1f))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    prettyNumber(number).ifEmpty { " " },
                    Modifier.weight(1f),
                    fontSize = 32.sp,
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                )
                if (number.isNotEmpty())
                    IconButton({ number = number.dropLast(1) }) {
                        Icon(Icons.AutoMirrored.Filled.Backspace, "Delete")
                    }
            }
            Spacer(Modifier.height(24.dp))
            Keypad({ if (number.length < 32) number += it })
            Spacer(Modifier.height(24.dp))
            LargeFloatingActionButton(
                onClick = { if (number.isNotBlank()) onCall(number) },
                containerColor = Color(0xFF188038),
                contentColor = Color.White,
                shape = CircleShape,
            ) {
                Icon(Icons.Filled.Call, "Call", Modifier.size(36.dp))
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

/** The call in progress (or just ended). */
@Composable
fun InCallScreen(c: CallUi) {
    var keypad by remember { mutableStateOf(false) }
    var digits by remember { mutableStateOf("") }
    /** PTT: true while the user holds the talk button (mic live). */
    var pttTalking by remember { mutableStateOf(false) }
    DisposableEffect(Unit) { onDispose { Phone.setPttTalking(false) } }
    var elapsed by remember { mutableLongStateOf(0L) }
    LaunchedEffect(c.connectedAt, c.ended) {
        while (c.connectedAt > 0 && !c.ended) {
            elapsed = (android.os.SystemClock.elapsedRealtime() - c.connectedAt) / 1000
            delay(500)
        }
    }
    // Leave the "Call ended" screen up briefly, then close it.
    LaunchedEffect(c.ended) {
        if (c.ended && c.problem.isEmpty()) {
            delay(1500)
            Phone.dismiss()
        }
    }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surface) {
        Column(
            Modifier.fillMaxSize().systemBarsPadding().padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Spacer(Modifier.height(32.dp))
            Avatar(c.number)
            Spacer(Modifier.height(16.dp))
            if (c.name.isNotBlank()) Text(c.name, style = MaterialTheme.typography.titleLarge)
            Text(
                prettyNumber(c.number),
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.SemiBold,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                when {
                    c.ptt && c.connectedAt > 0 && !c.ended && pttTalking -> "You're talking"
                    c.ptt && c.connectedAt > 0 && !c.ended -> "Listening — hold to talk"
                    c.connectedAt > 0 && !c.ended -> "%d:%02d".format(elapsed / 60, elapsed % 60)
                    else -> c.status
                },
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (digits.isNotEmpty()) Text(digits, style = MaterialTheme.typography.titleLarge)
            if (c.problem.isNotEmpty()) {
                Spacer(Modifier.height(16.dp))
                Text(
                    c.problem,
                    color = MaterialTheme.colorScheme.error,
                    textAlign = TextAlign.Center,
                )
                val clip = androidx.compose.ui.platform.LocalClipboardManager.current
                TextButton({
                    clip.setText(
                        androidx.compose.ui.text.AnnotatedString(
                            "My Phone ${org.ownpbx.myphone.BuildConfig.VERSION_NAME} call to ${c.number}: ${c.status}\n${c.problem}"
                        )
                    )
                }) {
                    Icon(Icons.Filled.ContentCopy, null)
                    Spacer(Modifier.width(6.dp))
                    Text("Copy details")
                }
            }
            Spacer(Modifier.weight(1f))
            val ctx = androidx.compose.ui.platform.LocalContext.current
            val askMic =
                androidx.activity.compose.rememberLauncherForActivityResult(
                    androidx.activity.result.contract.ActivityResultContracts.RequestPermission()
                ) { ok ->
                    if (ok) org.ownpbx.myphone.phone.CallService.answer(ctx) else Phone.decline()
                }
            if (c.ended) {
                Button({ Phone.dismiss() }) { Text("Close") }
            } else if (c.incoming && c.connectedAt == 0L && c.status == "Incoming call") {
                Row(horizontalArrangement = Arrangement.spacedBy(64.dp)) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        LargeFloatingActionButton(
                            { Phone.decline() },
                            containerColor = Color(0xFFD93025),
                            contentColor = Color.White,
                            shape = CircleShape,
                        ) {
                            Icon(Icons.Filled.CallEnd, "Decline", Modifier.size(36.dp))
                        }
                        Spacer(Modifier.height(6.dp))
                        Text("Decline")
                    }
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        LargeFloatingActionButton(
                            {
                                if (
                                    androidx.core.content.ContextCompat.checkSelfPermission(
                                        ctx,
                                        android.Manifest.permission.RECORD_AUDIO,
                                    ) == android.content.pm.PackageManager.PERMISSION_GRANTED
                                )
                                    org.ownpbx.myphone.phone.CallService.answer(ctx)
                                else
                                    try {
                                        askMic.launch(android.Manifest.permission.RECORD_AUDIO)
                                    } catch (_: Exception) {}
                            },
                            containerColor = Color(0xFF188038),
                            contentColor = Color.White,
                            shape = CircleShape,
                        ) {
                            Icon(Icons.Filled.Call, "Answer", Modifier.size(36.dp))
                        }
                        Spacer(Modifier.height(6.dp))
                        Text("Answer")
                    }
                }
            } else if (c.ptt) {
                PttControls(c, pttTalking) { on ->
                    pttTalking = on
                    Phone.setPttTalking(on)
                }
            } else {
                if (keypad) {
                    Keypad({
                        Phone.dtmf(it)
                        digits = (digits + it).takeLast(20)
                    })
                    Spacer(Modifier.height(24.dp))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(28.dp)) {
                    CallToggle(
                        if (c.muted) Icons.Filled.MicOff else Icons.Filled.Mic,
                        "Mute",
                        c.muted,
                    ) {
                        Phone.toggleMute()
                    }
                    CallToggle(Icons.Filled.Dialpad, "Keypad", keypad) { keypad = !keypad }
                    CallToggle(Icons.AutoMirrored.Filled.VolumeUp, "Speaker", c.speaker) {
                        Phone.toggleSpeaker()
                    }
                }
                Spacer(Modifier.height(32.dp))
                LargeFloatingActionButton(
                    { Phone.hangUp() },
                    containerColor = Color(0xFFD93025),
                    contentColor = Color.White,
                    shape = CircleShape,
                ) {
                    Icon(Icons.Filled.CallEnd, "Hang up", Modifier.size(36.dp))
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

/**
 * Push-to-talk call controls: a speaker toggle and the big hold-to-talk button. The mic is muted
 * except while the button is held down.
 */
@Composable
private fun PttControls(c: CallUi, talking: Boolean, onTalk: (Boolean) -> Unit) {
    Row(horizontalArrangement = Arrangement.spacedBy(28.dp)) {
        CallToggle(Icons.AutoMirrored.Filled.VolumeUp, "Speaker", c.speaker) {
            Phone.toggleSpeaker()
        }
    }
    Spacer(Modifier.height(24.dp))
    Surface(
        shape = CircleShape,
        color = if (talking) Color(0xFF188038) else MaterialTheme.colorScheme.primaryContainer,
        modifier =
            Modifier.size(180.dp).clip(CircleShape).pointerInput(Unit) {
                detectTapGestures(
                    onPress = {
                        onTalk(true)
                        try {
                            awaitRelease()
                        } finally {
                            onTalk(false)
                        }
                    }
                )
            },
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Icon(
                if (talking) Icons.Filled.Mic else Icons.Filled.MicOff,
                null,
                Modifier.size(48.dp),
                tint = if (talking) Color.White else MaterialTheme.colorScheme.onPrimaryContainer,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                if (talking) "Talking…" else "Hold to talk",
                color = if (talking) Color.White else MaterialTheme.colorScheme.onPrimaryContainer,
                fontWeight = FontWeight.SemiBold,
            )
        }
    }
    Spacer(Modifier.height(32.dp))
    LargeFloatingActionButton(
        { Phone.hangUp() },
        containerColor = Color(0xFFD93025),
        contentColor = Color.White,
        shape = CircleShape,
    ) {
        Icon(Icons.Filled.CallEnd, "Hang up", Modifier.size(36.dp))
    }
}

@Composable
private fun CallToggle(icon: ImageVector, label: String, on: Boolean, onClick: () -> Unit) {
    Column(horizontalAlignment = Alignment.CenterHorizontally) {
        FilledIconToggleButton(on, { onClick() }, Modifier.size(64.dp)) { Icon(icon, label) }
        Spacer(Modifier.height(4.dp))
        Text(label, style = MaterialTheme.typography.labelMedium)
    }
}
