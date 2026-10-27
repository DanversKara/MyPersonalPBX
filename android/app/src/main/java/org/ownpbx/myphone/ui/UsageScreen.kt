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
import org.ownpbx.myphone.data.Usage
import org.ownpbx.myphone.data.UsageFeature

/**
 * Plan & usage: the same numbers the web portal's usage panel shows (the server computes both from
 * ucp._quota/_left, so they can't drift apart). Null quota/left from the server means unlimited; a
 * quota of 0 means the feature isn't in the plan.
 */
@Composable
fun UsageScreen(model: AppModel, onBack: () -> Unit) {
    var usage by remember { mutableStateOf<Usage?>(null) }
    var failed by remember { mutableStateOf(false) }
    var reload by remember { mutableIntStateOf(0) }
    LaunchedEffect(reload) {
        failed = false
        usage =
            try {
                model.store.api().usage()
            } catch (e: Exception) {
                failed = true
                null
            }
    }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Plan & usage") },
                navigationIcon = {
                    IconButton(onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                },
            )
        }
    ) { pad ->
        Column(Modifier.padding(pad).fillMaxSize().verticalScroll(rememberScrollState())) {
            val u = usage
            when {
                failed -> {
                    Text(
                        "Couldn't load usage",
                        Modifier.padding(16.dp),
                        style = MaterialTheme.typography.bodyLarge,
                    )
                    Button({ reload++ }, Modifier.padding(horizontal = 16.dp)) { Text("Try again") }
                }
                u == null -> LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 8.dp))
                else -> {
                    ListItem(
                        headlineContent = { Text(u.plan ?: "Your plan") },
                        supportingContent = { Text("This month (resets on the 1st)") },
                    )
                    UsageRow("Minutes", u.features.call_minutes, " min")
                    UsageRow("Texts", u.features.messages, "")
                    UsageRow("Voicemail", u.features.voicemail, "", bool = true)
                    // Fax: sending allowance this month (receiving is never limited).
                    u.features.faxes?.let { f ->
                        if (u.includes.fax)
                            if (f.quota == 0L)
                                ListItem(
                                    overlineContent = { Text("Faxes") },
                                    headlineContent = { Text("Receive only") },
                                    supportingContent = { Text("Your plan doesn't include sending faxes") },
                                )
                            else UsageRow("Faxes sent", f, "")
                    }
                    UsageHeader("Your plan includes")
                    IncludeRow("Voicemail", u.includes.voicemail)
                    IncludeRow("Push-to-talk", u.includes.push_to_talk)
                    IncludeRow("Call recording", u.includes.recording)
                    IncludeRow("IVR menus", u.includes.ivr_menus)
                    IncludeRow("Texting", u.includes.messages)
                    IncludeRow("Fax (send & receive)", u.includes.fax)
                    Spacer(Modifier.height(24.dp))
                }
            }
        }
    }
}

@Composable
private fun UsageHeader(title: String) {
    Text(
        title,
        Modifier.padding(start = 16.dp, top = 24.dp, bottom = 4.dp),
        style = MaterialTheme.typography.titleSmall,
        color = MaterialTheme.colorScheme.primary,
    )
}

/**
 * One quota line. Count features read "N left" with "used of quota" underneath (unlimited: just
 * "used"); on/off features (voicemail) read Included / Not included, like the portal.
 */
@Composable
private fun UsageRow(label: String, f: UsageFeature, unit: String, bool: Boolean = false) {
    val headline: String
    val supporting: String?
    when {
        bool -> {
            headline = if (f.quota != null && f.quota != 0L) "Included" else "Not included"
            supporting = null
        }
        f.quota == 0L -> {
            headline = "Not included"
            supporting = null
        }
        f.left == null -> {
            headline = "Unlimited"
            supporting = "${f.used}$unit used"
        }
        else -> {
            headline = "${f.left}$unit left"
            supporting = "${f.used} of ${f.quota}$unit used"
        }
    }
    ListItem(
        headlineContent = { Text(headline) },
        supportingContent = supporting?.let { { Text(it) } },
        overlineContent = { Text(label) },
    )
}

@Composable
private fun IncludeRow(label: String, included: Boolean) {
    ListItem(
        headlineContent = { Text(label) },
        leadingContent = {
            if (included)
                Icon(
                    Icons.Filled.Check,
                    null,
                    tint = MaterialTheme.colorScheme.primary,
                )
            else
                Icon(
                    Icons.Filled.Remove,
                    null,
                    tint = MaterialTheme.colorScheme.onSurfaceVariant,
                )
        },
        trailingContent = { Text(if (included) "Included" else "—") },
    )
}
