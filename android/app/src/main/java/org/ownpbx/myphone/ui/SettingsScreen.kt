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
import androidx.compose.ui.platform.LocalContext
import org.ownpbx.myphone.Notifier
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
    var edit by model::settingsDialog   // survives the PIN lock
    var changingPin by remember { mutableStateOf(false) }
    var recordAsk by remember { mutableStateOf(false) }
    var bio by remember { mutableStateOf(store.biometric) }
    var secure by remember { mutableStateOf(store.secureScreen) }
    var lockAfter by remember { mutableIntStateOf(store.lockAfterSec) }
    var notifyOn by remember { mutableStateOf(store.notifyOn) }
    var notifyText by remember { mutableStateOf(store.notifyShowText) }
    val ctx = LocalContext.current
    var lastCheck by remember { mutableStateOf(store.lastCheckAt to store.lastCheckInfo) }
    var checking by remember { mutableStateOf(false) }
    var checkEvery by remember { mutableIntStateOf(store.checkEvery) }
    var incoming by remember { mutableStateOf(store.incomingCalls) }
    val power = ctx.getSystemService(android.os.PowerManager::class.java)
    var unrestricted by remember { mutableStateOf(power?.isIgnoringBatteryOptimizations(ctx.packageName) == true) }
    // Re-read after coming back from Android's battery screen.
    androidx.lifecycle.compose.LifecycleResumeEffect(Unit) {
        unrestricted = power?.isIgnoringBatteryOptimizations(ctx.packageName) == true
        onPauseOrDispose {}
    }
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

        Section("Notifications")
        SwitchRow(Icons.Filled.Notifications, "Notifications", "New texts, missed calls and voicemail", notifyOn) {
            notifyOn = it; store.notifyOn = it
            if (it) { Notifier.schedule(ctx); org.ownpbx.myphone.LiveService.sync(ctx) } else Notifier.stop(ctx)
        }
        var everyMenu by remember { mutableStateOf(false) }
        Box {
            ClickRow(Icons.Filled.Speed, "Check for new things",
                org.ownpbx.myphone.LiveService.describe(checkEvery) +
                    if (checkEvery < 900) " · shows a small \"Connected\" notification" else "") { if (notifyOn) everyMenu = true }
            DropdownMenu(everyMenu, { everyMenu = false }) {
                listOf(0 to "Instant (stays connected)", 30 to "Every 30 seconds", 60 to "Every 1 minute",
                    300 to "Every 5 minutes", 900 to "Every 15 minutes (saves battery)").forEach { (v, label) ->
                    DropdownMenuItem({ Text(label) }, {
                        everyMenu = false; checkEvery = v; store.checkEvery = v
                        // Restart so the new pace applies now.
                        ctx.stopService(android.content.Intent(ctx, org.ownpbx.myphone.LiveService::class.java))
                        org.ownpbx.myphone.LiveService.sync(ctx)
                    })
                }
            }
        }
        // Shown whenever the connection service can run — for fast
        // notifications OR for incoming calls — and it stays visible after
        // "Allow" too. It used to vanish for good the moment Android
        // reported the exemption (and was hidden entirely for calls-only
        // use), so when Motorola quietly re-enabled battery killing there
        // was no way back here from inside the app. Motorola also has a
        // second switch, hence the App settings row under it.
        if ((notifyOn && checkEvery < 900) || incoming) {
            ClickRow(if (unrestricted) Icons.Filled.BatteryStd else Icons.Filled.BatteryAlert,
                "Let it run in the background",
                if (unrestricted) "Allowed in Android — tap to re-check. On Motorola the row below matters too."
                else "Android's battery saver can pause the connection. Tap and choose Allow.") {
                try {
                    ctx.startActivity(android.content.Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        android.net.Uri.parse("package:" + ctx.packageName)))
                } catch (_: Exception) {
                    try { ctx.startActivity(android.content.Intent(android.provider.Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
                    catch (_: Exception) {}
                }
            }
            ClickRow(Icons.Filled.SettingsApplications, "App settings (Motorola battery killer)",
                "Battery > Unrestricted, and turn off 'Pause app activity if unused'") {
                try {
                    ctx.startActivity(android.content.Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        android.net.Uri.parse("package:" + ctx.packageName)))
                } catch (_: Exception) {}
            }
        }
        SwitchRow(Icons.Filled.Sms, "Show text in notifications",
            "Off: just \"New message\" and who it's from", notifyText, enabled = notifyOn) {
            notifyText = it; store.notifyShowText = it
        }
        ClickRow(Icons.Filled.NotificationsActive, "Send a test notification", "Shows one now, to check sound and pop-ups") {
            if (!Notifier.test(ctx)) model.notice = "Android is blocking notifications for this app. Turn them on under Sounds and pop-ups."
        }
        ClickRow(Icons.Filled.Sync, if (checking) "Checking..." else "Check now",
            lastCheck.let { (at, info) ->
                if (at == 0L) "No background check has run yet"
                else "Last check ${java.text.DateFormat.getTimeInstance(java.text.DateFormat.SHORT).format(java.util.Date(at))}: $info"
            }) {
            if (!checking) {
                checking = true
                model.bg.launch {
                    try { Notifier.run(ctx.applicationContext, seenOnly = false, force = true) }
                    catch (e: Exception) { Notifier.record(store, "Couldn't reach the PBX (${e.javaClass.simpleName})") }
                    lastCheck = store.lastCheckAt to store.lastCheckInfo
                    checking = false
                }
            }
        }
        ClickRow(Icons.Filled.Tune, "Sounds and pop-ups", "Android's notification settings for this app") {
            try {
                ctx.startActivity(android.content.Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, ctx.packageName))
            } catch (_: Exception) {}
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

        Section("Calling")
        SwitchRow(Icons.Filled.PhoneCallback, "Incoming calls",
            if (incoming) org.ownpbx.myphone.phone.Phone.registration else "Calls ring your other phones only", incoming) {
            incoming = it; store.incomingCalls = it
            org.ownpbx.myphone.LiveService.sync(ctx)
        }
        val customSip = store.sipUserOverride.isNotBlank() || store.sipServerOverride.isNotBlank() || store.sipPassOverride.isNotBlank()
        ClickRow(Icons.Filled.Call, "Phone login",
            if (customSip) "Custom: ${store.sipUserOverride.ifBlank { "(PBX's username)" }} @ ${store.sipServerOverride.ifBlank { "(PBX's server)" }}"
            else "Automatic (your extension's login from the PBX)") { edit = "sip" }

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
        "sip" -> SipLoginDialog(model) { edit = null }
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

/** Settings > Calling > Phone login: scan the phone-login QR (My Phone >
 *  App > Show phone login QR), or type server / username / password like in
 *  Zoiper; blank = use what the PBX provides. What's typed is kept in the
 *  model, so going to a password manager (and back through the PIN) keeps it. */
@Composable
private fun SipLoginDialog(model: AppModel, onDone: () -> Unit) {
    val st = model.store
    val draft = model.sipDraft ?: org.ownpbx.myphone.data.SipLogin(st.sipServerOverride, st.sipUserOverride, st.sipPassOverride)
        .also { model.sipDraft = it }
    var show by remember { mutableStateOf(false) }
    val pbx = model.sip
    val appCtx = androidx.compose.ui.platform.LocalContext.current.applicationContext
    fun close() { model.sipDraft = null; onDone() }
    val scanner = androidx.activity.compose.rememberLauncherForActivityResult(com.journeyapps.barcodescanner.ScanContract()) { r ->
        val text = r.contents ?: return@rememberLauncherForActivityResult
        val login = org.ownpbx.myphone.data.PairLink.parseSip(text)
        if (login == null) model.notice = "That isn't a phone login QR. Use My Phone > App > Show phone login QR."
        else { model.sipDraft = login; model.notice = "Scanned. Check it and tap Save." }
    }
    AlertDialog(
        onDismissRequest = ::close,
        title = { Text("Phone login") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Get it from your PBX, scan the phone login QR (My Phone > App on the web), or type it like in Zoiper.",
                    style = MaterialTheme.typography.bodySmall)
                // One tap: the extension's phone login straight from the PBX (no QR needed).
                FilledTonalButton({
                    model.bg.launch {
                        val sip = model.guard { it.sip() }
                        when {
                            sip == null -> {}
                            !sip.enabled -> model.notice = sip.reason.ifEmpty { "Your PBX has no phone login for you yet." }
                            else -> {
                                model.sipDraft = org.ownpbx.myphone.data.SipLogin("${sip.domain}:${sip.port}", sip.username, sip.password)
                                show = true
                                model.notice = "Filled in from your PBX. Check it and tap Save."
                            }
                        }
                    }
                }, Modifier.fillMaxWidth()) {
                    Icon(Icons.Filled.CloudDownload, null); Spacer(Modifier.width(8.dp)); Text("Get from my PBX")
                }
                OutlinedButton({
                    try {
                        model.openingOwnScreen()
                        scanner.launch(com.journeyapps.barcodescanner.ScanOptions()
                            .setDesiredBarcodeFormats(com.journeyapps.barcodescanner.ScanOptions.QR_CODE)
                            .setPrompt("Scan the phone login QR from My Phone > App")
                            .setBeepEnabled(false).setOrientationLocked(false))
                    } catch (e: Exception) { model.notice = "The scanner couldn't start." }
                }, Modifier.fillMaxWidth()) {
                    Icon(Icons.Filled.QrCodeScanner, null); Spacer(Modifier.width(8.dp)); Text("Scan QR code")
                }
                OutlinedTextField(draft.server, { model.sipDraft = draft.copy(server = it.trim()) }, singleLine = true,
                    label = { Text("Server (host or host:port)") },
                    placeholder = { Text(pbx?.domain?.takeIf { it.isNotBlank() }?.let { "$it:${pbx.port}" } ?: "phone.example.com:5061") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
                OutlinedTextField(draft.user, { model.sipDraft = draft.copy(user = it.trim()) }, singleLine = true,
                    label = { Text("Username") }, placeholder = { Text(pbx?.username?.ifBlank { null } ?: "") })
                OutlinedTextField(draft.pass, { model.sipDraft = draft.copy(pass = it) }, singleLine = true,
                    label = { Text("Password") },
                    visualTransformation = if (show) androidx.compose.ui.text.input.VisualTransformation.None else PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    trailingIcon = {
                        IconButton({ show = !show }) { Icon(if (show) Icons.Filled.VisibilityOff else Icons.Filled.Visibility, "Show") }
                    })
            }
        },
        confirmButton = {
            TextButton({
                st.sipServerOverride = draft.server; st.sipUserOverride = draft.user; st.sipPassOverride = draft.pass
                model.sip = null; model.notice = "Phone login saved"; close()
                org.ownpbx.myphone.LiveService.sync(appCtx)         // sign in again with it
            }) { Text("Save") }
        },
        dismissButton = {
            Row {
                TextButton({
                    st.sipServerOverride = ""; st.sipUserOverride = ""; st.sipPassOverride = ""
                    model.sip = null; model.notice = "Using your PBX's phone login"; close()
                    org.ownpbx.myphone.LiveService.sync(appCtx)
                }) { Text("Use automatic") }
                TextButton(::close) { Text("Cancel") }
            }
        })
}
