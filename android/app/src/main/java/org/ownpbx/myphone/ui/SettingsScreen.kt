// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Device
import org.ownpbx.myphone.data.Overview
import org.ownpbx.myphone.data.Settings

@Composable
private fun Section(title: String) {
    Text(title, Modifier.padding(start = 16.dp, top = 24.dp, bottom = 4.dp),
        style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.primary)
}

@Composable
private fun SwitchRow(icon: ImageVector, title: String, sub: String?, checked: Boolean, enabled: Boolean = true,
                      onChange: (Boolean) -> Unit) {
    ListItem(
        modifier = Modifier.clickable(enabled = enabled) { onChange(!checked) },
        leadingContent = { Icon(icon, null) },
        headlineContent = { Text(title) },
        supportingContent = sub?.let { { Text(it) } },
        trailingContent = { Switch(checked, onChange, enabled = enabled) },
    )
}

@Composable
private fun ClickRow(icon: ImageVector, title: String, sub: String? = null, onClick: () -> Unit) {
    ListItem(
        modifier = Modifier.clickable(onClick = onClick),
        leadingContent = { Icon(icon, null) },
        headlineContent = { Text(title) },
        supportingContent = sub?.let { { Text(it) } },
    )
}

/** Small "edit one value" dialog used by several rows. */
@Composable
private fun EditDialog(title: String, initial: String, hint: String, phone: Boolean = true,
                       onDismiss: () -> Unit, onSave: (String) -> Unit) {
    var v by remember { mutableStateOf(initial) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text(title) },
        text = {
            OutlinedTextField(v, { v = it }, singleLine = true, label = { Text(hint) },
                keyboardOptions = KeyboardOptions(keyboardType = if (phone) KeyboardType.Phone else KeyboardType.Email))
        },
        confirmButton = { TextButton({ onSave(v.trim()) }) { Text("Save") } },
        dismissButton = { TextButton(onDismiss) { Text("Cancel") } })
}

@Composable
fun SettingsScreen(model: AppModel, overview: Overview?, modifier: Modifier) {
    val store = model.store
    var s by remember { mutableStateOf<Settings?>(null) }
    var devices by remember { mutableStateOf<List<Device>>(emptyList()) }
    var edit by remember { mutableStateOf<String?>(null) }
    var changingPin by remember { mutableStateOf(false) }
    var recordAsk by remember { mutableStateOf(false) }
    var bio by remember { mutableStateOf(store.biometric) }
    var secure by remember { mutableStateOf(store.secureScreen) }
    var lockAfter by remember { mutableIntStateOf(store.lockAfterSec) }
    val scope = rememberCoroutineScope()

    suspend fun load() {
        model.guard { it.settings() }?.let { s = it }
        model.guard { it.devices() }?.let { devices = it.devices }
    }
    LaunchedEffect(Unit) { load() }

    /** Save a change optimistically; reload from the server if it was refused. */
    fun saveCalls(n: Settings) { s = n; scope.launch { if (!model.change { it.saveCalls(n) }) load() } }
    fun savePrefs(n: Settings, ack: Boolean = false) { s = n; scope.launch { if (!model.change { it.savePrefs(n, ack) }) load() } }
    fun saveMsg(n: Settings) { s = n; scope.launch { if (!model.change { it.saveMessaging(n.msg_in, n.msg_out) }) load() } }

    if (changingPin) {
        SetPinScreen(model, onCancel = { changingPin = false }, onSaved = { changingPin = false; model.notice = "PIN changed" })
        return
    }

    Column(modifier.fillMaxSize().verticalScroll(rememberScrollState())) {
        Text("Settings", Modifier.statusBarsPadding().padding(16.dp, 8.dp), style = MaterialTheme.typography.headlineSmall)
        ListItem(
            leadingContent = { Avatar(store.exten.ifEmpty { store.username }) },
            headlineContent = { Text(overview?.me?.display_name?.ifEmpty { null } ?: store.username) },
            supportingContent = {
                Text("Ext ${store.exten.ifEmpty { "-" }} · " + when (overview?.registered) {
                    true -> "phone online"; false -> "no phone online"; else -> "status unknown" } +
                    (overview?.texts_left?.let { " · $it texts left" } ?: ""))
            },
        )
        if (overview?.locked == true) Text("The administrator has turned on the safety lock; changes are paused.",
            Modifier.padding(16.dp), color = MaterialTheme.colorScheme.error)

        val st = s
        if (st == null) {
            LinearProgressIndicator(Modifier.fillMaxWidth().padding(16.dp))
        } else {
            Section("Calls")
            SwitchRow(Icons.Filled.DoNotDisturbOn, "Do not disturb", "Calls go straight to voicemail", st.dnd) {
                saveCalls(st.copy(dnd = it))
            }
            ClickRow(Icons.Filled.PhoneForwarded, "Forward all calls",
                st.forward_always.ifEmpty { "Off" }) { edit = "fa" }
            ClickRow(Icons.Filled.PhoneMissed, "Forward when I don't answer",
                st.forward_noanswer.ifEmpty { "Off" }) { edit = "fn" }
            var ringMenu by remember { mutableStateOf(false) }
            Box {
                ClickRow(Icons.Filled.Timer, "Ring time", "${st.ring_seconds} seconds") { ringMenu = true }
                DropdownMenu(ringMenu, { ringMenu = false }) {
                    st.ring_choices.forEach { r ->
                        DropdownMenuItem({ Text("$r seconds") }, { ringMenu = false; saveCalls(st.copy(ring_seconds = r)) })
                    }
                }
            }
            SwitchRow(Icons.Filled.FiberManualRecord, "Record my calls",
                if (st.record_allowed) "You must follow call-recording laws" else "Not included in your plan",
                st.user_record, enabled = st.record_allowed) {
                if (it && st.record_ack_needed) recordAsk = true else savePrefs(st.copy(user_record = it))
            }

            Section("Texts")
            SwitchRow(Icons.Filled.MoveToInbox, "Receive texts", null, st.msg_in) { saveMsg(st.copy(msg_in = it)) }
            SwitchRow(Icons.Filled.Outbox, "Send texts", null, st.msg_out) { saveMsg(st.copy(msg_out = it)) }
            SwitchRow(Icons.Filled.Email, "Email me my texts", null, st.text_email) { savePrefs(st.copy(text_email = it)) }
            ClickRow(Icons.Filled.AlternateEmail, "Notification email", st.vm_email.ifEmpty { "Not set" }) { edit = "email" }

            Section("Blocked numbers")
            if (st.blocked.isEmpty()) Text("Nobody is blocked.", Modifier.padding(horizontal = 16.dp),
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            st.blocked.forEach { b ->
                ListItem(leadingContent = { Icon(Icons.Filled.Block, null) },
                    headlineContent = { Text(prettyNumber(b.number)) },
                    trailingContent = {
                        TextButton({ scope.launch { model.change { it.unblock(b.number) }; load() } }) { Text("Unblock") }
                    })
            }
            ClickRow(Icons.Filled.PersonAddDisabled, "Block a number") { edit = "block" }
        }

        Section("App security")
        ClickRow(Icons.Filled.Pin, "Change PIN", "${store.pinLength} digits") { changingPin = true }
        SwitchRow(Icons.Filled.Fingerprint, "Unlock with fingerprint or face", "Your PIN always works too", bio) {
            bio = it; store.biometric = it
        }
        var lockMenu by remember { mutableStateOf(false) }
        Box {
            ClickRow(Icons.Filled.LockClock, "Lock the app",
                when (lockAfter) { 0 -> "Immediately when I leave"; else -> "After ${lockAfter / 60} min away" }) { lockMenu = true }
            DropdownMenu(lockMenu, { lockMenu = false }) {
                listOf(0 to "Immediately", 60 to "After 1 minute", 300 to "After 5 minutes", 900 to "After 15 minutes").forEach { (v, l) ->
                    DropdownMenuItem({ Text(l) }, { lockMenu = false; lockAfter = v; store.lockAfterSec = v })
                }
            }
        }
        SwitchRow(Icons.Filled.VisibilityOff, "Hide screen in recent apps", "Also blocks screenshots", secure) {
            secure = it; store.secureScreen = it; model.onSecureChanged()
        }
        ClickRow(Icons.Filled.Lock, "Lock now") { model.lock() }

        Section("Account")
        ClickRow(Icons.Filled.Dns, "Server", store.server) {}
        ClickRow(Icons.Filled.Password, "Change panel password", "Signs out your other phones and browsers") { edit = "pw" }
        devices.forEach { d ->
            ListItem(leadingContent = { Icon(Icons.Filled.Smartphone, null) },
                headlineContent = { Text(d.name + if (d.this_device) " (this phone)" else "") },
                supportingContent = { Text("Last used ${shortTime(d.last_used_at ?: d.created_at)}") },
                trailingContent = {
                    if (!d.this_device) TextButton({
                        scope.launch { model.guard { it.deleteDevice(d.id) }; load() }
                    }) { Text("Sign out") }
                })
        }
        ClickRow(Icons.Filled.Logout, "Sign out of this phone") {
            scope.launch { model.guard { it.logout() }; model.signedOut() }
        }

        Section("About")
        ListItem(headlineContent = { Text("My Phone ${org.ownpbx.myphone.BuildConfig.VERSION_NAME}") },
            supportingContent = { Text("Free software (GPL-2.0-or-later). No ads, no tracking, no Google services.") })
        Spacer(Modifier.height(32.dp))
    }

    val st = s
    when (edit) {
        "fa" -> st?.let { EditDialog("Forward all calls", it.forward_always, "Number (empty = off)", onDismiss = { edit = null }) { v ->
            edit = null; saveCalls(it.copy(forward_always = v)) } }
        "fn" -> st?.let { EditDialog("Forward when unanswered", it.forward_noanswer, "Number (empty = off)", onDismiss = { edit = null }) { v ->
            edit = null; saveCalls(it.copy(forward_noanswer = v)) } }
        "email" -> st?.let { EditDialog("Notification email", it.vm_email, "Email address", phone = false, onDismiss = { edit = null }) { v ->
            edit = null; savePrefs(it.copy(vm_email = v)) } }
        "block" -> EditDialog("Block a number", "", "Extension or phone number", onDismiss = { edit = null }) { v ->
            edit = null; scope.launch { model.change { it.block(v) }; load() } }
        "pw" -> PasswordDialog({ edit = null }) { cur, new ->
            edit = null; scope.launch { if (model.change { it.changePassword(cur, new) }) { model.notice = "Password changed"; load() } } }
    }
    if (recordAsk) AlertDialog(
        onDismissRequest = { recordAsk = false },
        title = { Text("Recording calls") },
        text = { Text("In many places everyone on a call must agree before it's recorded. You're responsible for " +
            "following the law where you and the other person are. Turn on recording?") },
        confirmButton = { TextButton({ recordAsk = false; st?.let { savePrefs(it.copy(user_record = true), ack = true) } }) {
            Text("I understand, turn on") } },
        dismissButton = { TextButton({ recordAsk = false }) { Text("Cancel") } })
}

@Composable
private fun PasswordDialog(onDismiss: () -> Unit, onSave: (String, String) -> Unit) {
    var cur by remember { mutableStateOf("") }
    var new by remember { mutableStateOf("") }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("Change panel password") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(cur, { cur = it }, singleLine = true, label = { Text("Current password") },
                    visualTransformation = PasswordVisualTransformation())
                OutlinedTextField(new, { new = it }, singleLine = true, label = { Text("New password (8+ characters)") },
                    visualTransformation = PasswordVisualTransformation())
            }
        },
        confirmButton = { TextButton({ onSave(cur, new) }, enabled = new.length >= 8 && cur.isNotEmpty()) { Text("Change") } },
        dismissButton = { TextButton(onDismiss) { Text("Cancel") } })
}
