// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(
    androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.ExperimentalFoundationApi::class,
)

package org.ownpbx.myphone.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Contact
import org.ownpbx.myphone.data.ContactDetail
import org.ownpbx.myphone.data.TimelineItem

/**
 * Contacts, per-contact timeline, and notes. Names feed the call/message/ voicemail lists through
 * the peer_name / caller_name the PBX resolves.
 */
@Composable
fun ContactsScreen(model: AppModel, snack: SnackbarHostState, modifier: Modifier) {
    var openId by rememberSaveable { mutableStateOf<Long?>(null) }
    val id = openId
    if (id != null) {
        BackHandler { openId = null }
        ContactDetailScreen(model, id) { openId = null }
        return
    }

    var contacts by remember { mutableStateOf<List<Contact>?>(null) }
    var offline by remember { mutableStateOf(false) }
    var query by rememberSaveable { mutableStateOf("") }
    var dialog by remember { mutableStateOf<Contact?>(null) } // id == 0 -> new contact
    val scope = rememberCoroutineScope()

    suspend fun refresh() {
        val r = model.guard { it.contacts() }
        if (r != null) {
            contacts = r.contacts
            offline = false
            model.contactCache.save(r.contacts)
        } else {
            val cached = model.contactCache.load()
            if (cached.isNotEmpty()) {
                contacts = cached
                offline = true
            } else {
                model.notice = "Couldn't load contacts"
            }
        }
    }
    LaunchedEffect(Unit) {
        // Show the offline copy immediately; refresh from the server in back.
        val cached = model.contactCache.load()
        if (cached.isNotEmpty()) {
            contacts = cached
            offline = true
        }
        refresh()
    }

    val q = query.trim()
    val dq = q.filter { it.isDigit() }
    val shown =
        contacts.orEmpty().filter { c ->
            q.isBlank() ||
                c.name.contains(q, ignoreCase = true) ||
                c.org.contains(q, ignoreCase = true) ||
                c.number.contains(dq.ifEmpty { q })
        }

    Scaffold(
        modifier = modifier,
        topBar = {
            Column(Modifier.statusBarsPadding().padding(16.dp, 8.dp)) {
                Text("Contacts", style = MaterialTheme.typography.headlineSmall)
                if (offline) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Offline — showing saved contacts",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.tertiary,
                    )
                }
                Spacer(Modifier.height(8.dp))
                OutlinedTextField(
                    query,
                    { query = it },
                    Modifier.fillMaxWidth(),
                    singleLine = true,
                    placeholder = { Text("Search name or number") },
                    leadingIcon = { Icon(Icons.Filled.Search, null) },
                )
            }
        },
        floatingActionButton = {
            FloatingActionButton({ dialog = Contact(id = 0) }) {
                Icon(Icons.Filled.Add, "Add contact")
            }
        },
    ) { pad ->
        PullToRefreshBox(
            false,
            { scope.launch { refresh() } },
            Modifier.padding(pad).fillMaxSize(),
        ) {
            when {
                contacts == null ->
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator()
                    }
                shown.isEmpty() ->
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Icon(
                                Icons.Filled.Person,
                                null,
                                Modifier.size(48.dp),
                                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            Spacer(Modifier.height(8.dp))
                            Text(
                                if (q.isBlank()) "No contacts yet" else "No matches",
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            if (q.isBlank())
                                Text(
                                    "Tap + to add your first one.",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                        }
                    }
                else ->
                    LazyColumn(Modifier.fillMaxSize()) {
                        items(shown, key = { it.id }) { c ->
                            ListItem(
                                // Tap opens the contact; for a PTT contact the
                                // button (or a long press) places a PTT call.
                                modifier =
                                    Modifier.combinedClickable(
                                        onClick = { openId = c.id },
                                        onLongClick =
                                            if (c.ptt) ({ model.pttCallRequest = c.number })
                                            else null,
                                    ),
                                leadingContent = { Avatar(c.name.ifBlank { c.number }) },
                                headlineContent = {
                                    Text(c.name.ifBlank { prettyNumber(c.number) })
                                },
                                supportingContent = {
                                    if (c.name.isNotBlank()) Text(prettyNumber(c.number))
                                    else if (c.org.isNotBlank()) Text(c.org)
                                },
                                trailingContent = {
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        if (c.ptt)
                                            IconButton({ model.pttCallRequest = c.number }) {
                                                Icon(Icons.Filled.RecordVoiceOver, "Push-to-talk")
                                            }
                                        if (c.notes_count > 0) Badge { Text("${c.notes_count}") }
                                    }
                                },
                            )
                        }
                    }
            }
        }
    }

    dialog?.let { c ->
        ContactDialog(
            c,
            onDismiss = { dialog = null },
            onSaved = {
                dialog = null
                scope.launch { refresh() }
            },
            model = model,
        )
    }
}

/** Add or edit a contact. Name and number are required. */
@Composable
private fun ContactDialog(c: Contact, model: AppModel, onDismiss: () -> Unit, onSaved: () -> Unit) {
    var name by remember { mutableStateOf(c.name) }
    var number by remember { mutableStateOf(c.number) }
    var org by remember { mutableStateOf(c.org) }
    var email by remember { mutableStateOf(c.email) }
    var bad by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    fun save() {
        val nm = name.trim()
        val nb = number.trim()
        if (nm.isEmpty() || nb.isEmpty()) {
            bad = true
            return
        }
        scope.launch {
            val ok = model.change {
                it.saveContact(if (c.id == 0L) null else c.id, nm, nb, org.trim(), email.trim())
            }
            if (ok) {
                model.notice = "Contact saved"
                onSaved()
            }
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (c.id == 0L) "Add contact" else "Edit contact") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    name,
                    {
                        name = it
                        bad = false
                    },
                    Modifier.fillMaxWidth(),
                    singleLine = true,
                    label = { Text("Name *") },
                    isError = bad && name.trim().isEmpty(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Text),
                )
                OutlinedTextField(
                    number,
                    {
                        number = it
                        bad = false
                    },
                    Modifier.fillMaxWidth(),
                    singleLine = true,
                    label = { Text("Number *") },
                    isError = bad && number.trim().isEmpty(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone),
                )
                OutlinedTextField(
                    org,
                    { org = it },
                    Modifier.fillMaxWidth(),
                    singleLine = true,
                    label = { Text("Organization") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Text),
                )
                OutlinedTextField(
                    email,
                    { email = it },
                    Modifier.fillMaxWidth(),
                    singleLine = true,
                    label = { Text("Email") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
                )
                if (bad)
                    Text(
                        "Name and number are required.",
                        color = MaterialTheme.colorScheme.error,
                        style = MaterialTheme.typography.bodySmall,
                    )
            }
        },
        confirmButton = { TextButton({ save() }) { Text("Save") } },
        dismissButton = { TextButton(onDismiss) { Text("Cancel") } },
    )
}

/** One contact: header, call/text/voicemail/recording timeline, and notes. */
@Composable
private fun ContactDetailScreen(model: AppModel, id: Long, onClose: () -> Unit) {
    var detail by remember { mutableStateOf<ContactDetail?>(null) }
    var edit by remember { mutableStateOf<Contact?>(null) }
    var noteBody by remember { mutableStateOf("") }
    val scope = rememberCoroutineScope()

    suspend fun refresh() {
        val d = model.guard { it.contactDetail(id) }
        if (d == null) model.notice = "Couldn't load contact" else detail = d
    }
    LaunchedEffect(id) { refresh() }

    val c = detail?.contact
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(c?.name?.ifBlank { prettyNumber(c.number) } ?: "Contact") },
                navigationIcon = {
                    IconButton(onClose) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                },
                actions = {
                    if (c != null) {
                        IconButton({ edit = c }) { Icon(Icons.Filled.Edit, "Edit") }
                        IconButton({
                            scope.launch {
                                if (model.change { it.deleteContacts(listOf(id)) }) {
                                    model.notice = "Contact deleted"
                                    onClose()
                                }
                            }
                        }) {
                            Icon(Icons.Filled.Delete, "Delete")
                        }
                    }
                },
            )
        }
    ) { pad ->
        val d = detail
        when {
            d == null ->
                Box(Modifier.padding(pad).fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            else ->
                LazyColumn(Modifier.padding(pad).fillMaxSize()) {
                    item {
                        val cc = d.contact
                        Column(Modifier.padding(16.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Avatar(cc.name.ifBlank { cc.number })
                                Spacer(Modifier.width(12.dp))
                                Column {
                                    Text(
                                        cc.name.ifBlank { prettyNumber(cc.number) },
                                        style = MaterialTheme.typography.titleLarge,
                                    )
                                    if (cc.name.isNotBlank())
                                        Text(
                                            prettyNumber(cc.number),
                                            style = MaterialTheme.typography.bodyMedium,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        )
                                }
                            }
                            if (cc.org.isNotBlank()) Text(cc.org, Modifier.padding(top = 8.dp))
                            if (cc.email.isNotBlank())
                                Text(
                                    cc.email,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            if (cc.ptt) {
                                Spacer(Modifier.height(12.dp))
                                FilledTonalButton({ model.pttCallRequest = cc.number }) {
                                    Icon(Icons.Filled.RecordVoiceOver, null)
                                    Spacer(Modifier.width(8.dp))
                                    Text("Push-to-talk")
                                }
                                Text(
                                    "Answers on their speakerphone — hold to talk once you're through.",
                                    Modifier.padding(top = 4.dp),
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                        }
                        HorizontalDivider()
                    }
                    item {
                        Text(
                            "Timeline",
                            Modifier.padding(16.dp, 12.dp, 16.dp, 4.dp),
                            style = MaterialTheme.typography.titleSmall,
                            color = MaterialTheme.colorScheme.primary,
                        )
                    }
                    if (d.timeline.isEmpty())
                        item {
                            Text(
                                "No history yet.",
                                Modifier.padding(horizontal = 16.dp),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    items(d.timeline, key = { "${it.type}:${it.id}" }) { t -> TimelineRow(t) }
                    item {
                        Text(
                            "Notes (${d.notes.size})",
                            Modifier.padding(16.dp, 16.dp, 16.dp, 4.dp),
                            style = MaterialTheme.typography.titleSmall,
                            color = MaterialTheme.colorScheme.primary,
                        )
                    }
                    items(d.notes, key = { it.id }) { n ->
                        ListItem(
                            headlineContent = { Text(n.body) },
                            supportingContent = {
                                if (n.created_at.isNotBlank())
                                    Text(
                                        shortTime(n.created_at),
                                        style = MaterialTheme.typography.labelMedium,
                                    )
                            },
                            trailingContent = {
                                IconButton({
                                    scope.launch {
                                        if (model.change { it.deleteNotes(listOf(n.id)) }) refresh()
                                    }
                                }) {
                                    Icon(Icons.Filled.Delete, "Delete note")
                                }
                            },
                        )
                    }
                    item {
                        Row(
                            Modifier.padding(16.dp, 8.dp, 16.dp, 16.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            OutlinedTextField(
                                noteBody,
                                { noteBody = it },
                                Modifier.weight(1f),
                                singleLine = true,
                                placeholder = { Text("Add a note") },
                            )
                            Spacer(Modifier.width(8.dp))
                            IconButton({
                                val b = noteBody.trim()
                                if (b.isNotEmpty())
                                    scope.launch {
                                        if (model.change { it.saveNote(id, b) }) {
                                            noteBody = ""
                                            refresh()
                                        }
                                    }
                            }) {
                                Icon(Icons.Filled.Add, "Add note")
                            }
                        }
                    }
                }
        }
    }

    edit?.let { ec ->
        ContactDialog(
            ec,
            onDismiss = { edit = null },
            onSaved = {
                edit = null
                scope.launch { refresh() }
            },
            model = model,
        )
    }
}

@Composable
private fun TimelineRow(t: TimelineItem) {
    val (icon, label) =
        when (t.type) {
            "message" -> Icons.Filled.Chat to "Text"
            "voicemail" -> Icons.Filled.Voicemail to "Voicemail"
            "recording" -> Icons.Filled.Mic to "Recording"
            "missed" -> Icons.AutoMirrored.Filled.CallMissed to "Missed call"
            else ->
                if (t.direction == "out") Icons.AutoMirrored.Filled.CallMade to "Outgoing call"
                else Icons.AutoMirrored.Filled.CallReceived to "Incoming call"
        }
    ListItem(
        leadingContent = { Icon(icon, label) },
        headlineContent = { Text(t.name.ifBlank { prettyNumber(t.number) }) },
        supportingContent = {
            val bits = buildList {
                if (t.at.isNotBlank()) add(shortTime(t.at))
                add(label)
                if (t.duration_sec > 0) add(duration(t.duration_sec))
                if (t.body.isNotBlank()) add(t.body)
            }
            Text(bits.joinToString(" · "), maxLines = 2, overflow = TextOverflow.Ellipsis)
        },
    )
}
