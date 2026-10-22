// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Backspace
import androidx.compose.material.icons.filled.Fingerprint
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Pin

/** Dots + big number pad, like the Android lock screen. */
@Composable
fun PinPad(
    title: String,
    subtitle: String?,
    length: Int,
    error: String?,
    enabled: Boolean = true,
    onBiometric: (() -> Unit)? = null,
    onDone: (String) -> Unit,
) {
    var pin by remember { mutableStateOf("") }
    val haptic = LocalHapticFeedback.current
    LaunchedEffect(error) { if (error != null) pin = "" }

    fun press(d: String) {
        if (!enabled || pin.length >= length) return
        haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove)
        pin += d
        if (pin.length == length) { val p = pin; pin = ""; onDone(p) }
    }

    Column(
        Modifier.fillMaxSize().systemBarsPadding().padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Icon(Icons.Filled.Lock, null, Modifier.size(40.dp), tint = MaterialTheme.colorScheme.primary)
        Spacer(Modifier.height(16.dp))
        Text(title, style = MaterialTheme.typography.headlineSmall, textAlign = TextAlign.Center)
        subtitle?.let {
            Spacer(Modifier.height(8.dp))
            Text(it, style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(24.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(if (length > 6) 8.dp else 14.dp)) {
            repeat(length) { i ->
                Box(Modifier.size(if (length > 6) 12.dp else 16.dp).clip(CircleShape).background(
                    if (i < pin.length) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surfaceVariant))
            }
        }
        Spacer(Modifier.height(12.dp))
        Text(error ?: " ", color = MaterialTheme.colorScheme.error, textAlign = TextAlign.Center)
        Spacer(Modifier.height(16.dp))
        val rows = listOf(listOf("1", "2", "3"), listOf("4", "5", "6"), listOf("7", "8", "9"), listOf("bio", "0", "del"))
        rows.forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(20.dp)) {
                row.forEach { k ->
                    Box(Modifier.size(76.dp), contentAlignment = Alignment.Center) {
                        when (k) {
                            "bio" -> if (onBiometric != null) IconButton(onBiometric, Modifier.size(64.dp)) {
                                Icon(Icons.Filled.Fingerprint, "Use fingerprint", Modifier.size(32.dp))
                            }
                            "del" -> IconButton({ pin = pin.dropLast(1) }, Modifier.size(64.dp)) {
                                Icon(Icons.Filled.Backspace, "Delete digit")
                            }
                            else -> FilledTonalButton({ press(k) }, Modifier.size(72.dp), shape = CircleShape,
                                contentPadding = PaddingValues(0.dp), enabled = enabled) {
                                Text(k, style = MaterialTheme.typography.headlineMedium)
                            }
                        }
                    }
                }
            }
            Spacer(Modifier.height(12.dp))
        }
    }
}

/** First run (or "Change PIN"): pick 6 or 12 digits, enter twice. */
@Composable
fun SetPinScreen(model: AppModel, onCancel: (() -> Unit)? = null, onSaved: (() -> Unit)? = null) {
    var length by remember { mutableIntStateOf(model.store.pinLength) }
    var first by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    Scaffold { pad ->
        Column(Modifier.padding(pad)) {
            Row(Modifier.fillMaxWidth().padding(16.dp), horizontalArrangement = Arrangement.Center) {
                FilterChip(length == 6, { length = 6; first = null; error = null }, { Text("6 digits") })
                Spacer(Modifier.width(12.dp))
                FilterChip(length == 12, { length = 12; first = null; error = null }, { Text("12 digits (extra safe)") })
            }
            key(length, first == null) {
                PinPad(
                    title = if (first == null) "Choose an app PIN" else "Enter it again",
                    subtitle = if (first == null) "You'll use it to open My Phone. It stays on this phone." else null,
                    length = length, error = error,
                ) { p ->
                    when {
                        first == null && Pin.tooSimple(p) -> error = "Too easy to guess. Try another."
                        first == null -> { first = p; error = null }
                        p != first -> { first = null; error = "PINs didn't match. Start again." }
                        else -> scope.launch {
                            model.store.pinHash = Pin.hash(p)
                            model.store.pinLength = length
                            model.store.pinFails = 0
                            if (onSaved != null) onSaved() else model.stage = Stage.Ready
                        }
                    }
                }
            }
            onCancel?.let { TextButton(it, Modifier.align(Alignment.CenterHorizontally)) { Text("Cancel") } }
        }
    }
}

@Composable
fun LockScreen(model: AppModel, activity: FragmentActivity) {
    val store = model.store
    var error by remember { mutableStateOf<String?>(null) }
    var waitLeft by remember { mutableLongStateOf(0L) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        while (true) {
            waitLeft = ((store.pinLockedUntil - System.currentTimeMillis()) / 1000).coerceAtLeast(0)
            delay(500)
        }
    }

    val canBio = store.biometric && BiometricManager.from(activity)
        .canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG) == BiometricManager.BIOMETRIC_SUCCESS
    val bio: (() -> Unit)? = if (!canBio) null else {
        {
            val prompt = BiometricPrompt(activity, ContextCompat.getMainExecutor(activity),
                object : BiometricPrompt.AuthenticationCallback() {
                    override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                        store.pinFails = 0
                        model.stage = Stage.Ready
                    }
                })
            prompt.authenticate(BiometricPrompt.PromptInfo.Builder()
                .setTitle("Unlock My Phone").setNegativeButtonText("Use PIN")
                .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG).build())
        }
    }
    LaunchedEffect(canBio) { if (canBio && store.pinFails == 0) bio?.invoke() }

    PinPad(
        title = "Enter your PIN",
        subtitle = if (waitLeft > 0) "Too many wrong PINs. Wait $waitLeft s." else store.username.ifEmpty { null },
        length = store.pinLength, error = error, enabled = waitLeft == 0L, onBiometric = bio,
    ) { p ->
        scope.launch {
            if (Pin.check(p, store.pinHash ?: "")) {
                store.pinFails = 0
                model.stage = Stage.Ready
            } else {
                val fails = store.pinFails + 1
                store.pinFails = fails
                if (fails >= Pin.MAX_FAILS) {
                    model.guard { it.logout() }
                    model.signedOut("Too many wrong PINs, so the app signed out. Sign in again.")
                    return@launch
                }
                store.pinLockedUntil = System.currentTimeMillis() + Pin.delayFor(fails) * 1000
                error = "Wrong PIN. ${Pin.MAX_FAILS - fails} tries left before sign-out."
            }
        }
    }
}
