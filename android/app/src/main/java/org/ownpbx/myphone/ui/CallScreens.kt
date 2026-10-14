// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package org.ownpbx.myphone.ui

import androidx.compose.foundation.clickable
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import org.ownpbx.myphone.phone.CallUi
import org.ownpbx.myphone.phone.Phone

private val KEYS = listOf(
    "1" to "", "2" to "ABC", "3" to "DEF",
    "4" to "GHI", "5" to "JKL", "6" to "MNO",
    "7" to "PQRS", "8" to "TUV", "9" to "WXYZ",
    "*" to "", "0" to "+", "#" to "",
)

@Composable
private fun Keypad(onKey: (Char) -> Unit, onLongZero: (() -> Unit)? = null) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        KEYS.chunked(3).forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(18.dp)) {
                row.forEach { (k, letters) ->
                    Surface(
                        shape = CircleShape, tonalElevation = 2.dp,
                        modifier = Modifier.size(72.dp).clip(CircleShape).clickable { onKey(k[0]) },
                    ) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.Center) {
                            Text(k, fontSize = 28.sp)
                            if (letters.isNotEmpty()) Text(letters, style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
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
    Scaffold(topBar = {
        TopAppBar(title = { Text("Dial") },
            navigationIcon = { IconButton(onClose) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") } })
    }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.weight(1f))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(prettyNumber(number).ifEmpty { " " }, Modifier.weight(1f), fontSize = 32.sp,
                    textAlign = TextAlign.Center, maxLines = 1)
                if (number.isNotEmpty()) IconButton({ number = number.dropLast(1) }) {
                    Icon(Icons.AutoMirrored.Filled.Backspace, "Delete")
                }
            }
            Spacer(Modifier.height(24.dp))
            Keypad({ if (number.length < 32) number += it })
            Spacer(Modifier.height(24.dp))
            LargeFloatingActionButton(
                onClick = { if (number.isNotBlank()) onCall(number) },
                containerColor = Color(0xFF188038), contentColor = Color.White, shape = CircleShape,
            ) { Icon(Icons.Filled.Call, "Call", Modifier.size(36.dp)) }
            Spacer(Modifier.height(24.dp))
        }
    }
}

/** The call in progress (or just ended). */
@Composable
fun InCallScreen(c: CallUi) {
    var keypad by remember { mutableStateOf(false) }
    var digits by remember { mutableStateOf("") }
    var elapsed by remember { mutableLongStateOf(0L) }
    LaunchedEffect(c.connectedAt, c.ended) {
        while (c.connectedAt > 0 && !c.ended) {
            elapsed = (android.os.SystemClock.elapsedRealtime() - c.connectedAt) / 1000
            delay(500)
        }
    }
    // Leave the "Call ended" screen up briefly, then close it.
    LaunchedEffect(c.ended) {
        if (c.ended && c.problem.isEmpty()) { delay(1500); Phone.dismiss() }
    }

    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surface) {
        Column(Modifier.fillMaxSize().systemBarsPadding().padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally) {
            Spacer(Modifier.height(32.dp))
            Avatar(c.number)
            Spacer(Modifier.height(16.dp))
            Text(prettyNumber(c.number), style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            Text(if (c.connectedAt > 0 && !c.ended) "%d:%02d".format(elapsed / 60, elapsed % 60) else c.status,
                style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (digits.isNotEmpty()) Text(digits, style = MaterialTheme.typography.titleLarge)
            if (c.problem.isNotEmpty()) {
                Spacer(Modifier.height(16.dp))
                Text(c.problem, color = MaterialTheme.colorScheme.error, textAlign = TextAlign.Center)
            }
            Spacer(Modifier.weight(1f))
            if (c.ended) {
                Button({ Phone.dismiss() }) { Text("Close") }
            } else {
                if (keypad) {
                    Keypad({ Phone.dtmf(it); digits = (digits + it).takeLast(20) })
                    Spacer(Modifier.height(24.dp))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(28.dp)) {
                    CallToggle(if (c.muted) Icons.Filled.MicOff else Icons.Filled.Mic, "Mute", c.muted) { Phone.toggleMute() }
                    CallToggle(Icons.Filled.Dialpad, "Keypad", keypad) { keypad = !keypad }
                    CallToggle(Icons.AutoMirrored.Filled.VolumeUp, "Speaker", c.speaker) { Phone.toggleSpeaker() }
                }
                Spacer(Modifier.height(32.dp))
                LargeFloatingActionButton({ Phone.hangUp() }, containerColor = Color(0xFFD93025),
                    contentColor = Color.White, shape = CircleShape) {
                    Icon(Icons.Filled.CallEnd, "Hang up", Modifier.size(36.dp))
                }
            }
            Spacer(Modifier.height(24.dp))
        }
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
