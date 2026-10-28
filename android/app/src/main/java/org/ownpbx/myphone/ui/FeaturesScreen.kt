// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package org.ownpbx.myphone.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Remove
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import org.ownpbx.myphone.data.Features

/**
 * Features: which services are turned on for this account, as on/off only. Shown in place of
 * "Plan & usage" in App store mode, so it never mentions minutes, limits, plans or prices.
 */
@Composable
fun FeaturesScreen(model: AppModel, onBack: () -> Unit) {
    var data by remember { mutableStateOf<Features?>(null) }
    var failed by remember { mutableStateOf(false) }
    var reload by remember { mutableIntStateOf(0) }
    LaunchedEffect(reload) {
        failed = false
        data =
            try {
                model.store.api().features()
            } catch (e: Exception) {
                failed = true
                null
            }
    }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Features") },
                navigationIcon = {
                    IconButton(onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                },
            )
        }
    ) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().verticalScroll(rememberScrollState())) {
            val d = data
            when {
                failed -> {
                    Text(
                        "Couldn't load features",
                        Modifier.padding(16.dp),
                        style = MaterialTheme.typography.bodyLarge,
                    )
                    Button({ reload++ }, Modifier.padding(horizontal = 16.dp)) { Text("Try again") }
                }
                d == null -> LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 8.dp))
                else -> {
                    d.features.forEach { f ->
                        ListItem(
                            headlineContent = { Text(f.label) },
                            leadingContent = {
                                if (f.on)
                                    Icon(Icons.Filled.Check, null, tint = MaterialTheme.colorScheme.primary)
                                else
                                    Icon(
                                        Icons.Filled.Remove,
                                        null,
                                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                            },
                            trailingContent = { Text(if (f.on) "On" else "Off") },
                        )
                    }
                    if (d.account_note.isNotBlank())
                        Text(
                            d.account_note,
                            Modifier.padding(16.dp),
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    Spacer(Modifier.height(24.dp))
                }
            }
        }
    }
}
