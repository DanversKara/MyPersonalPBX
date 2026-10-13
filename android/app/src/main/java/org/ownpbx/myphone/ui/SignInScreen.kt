// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import android.os.Build
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Api
import org.ownpbx.myphone.data.ApiException
import org.ownpbx.myphone.data.LoginResp

@Composable
fun SignInScreen(model: AppModel) {
    var server by remember { mutableStateOf(model.store.server) }
    var usePair by remember { mutableStateOf(false) }
    var user by remember { mutableStateOf("") }
    var pass by remember { mutableStateOf("") }
    var showPass by remember { mutableStateOf(false) }
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf(model.notice) }
    val scope = rememberCoroutineScope()
    val device = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

    fun go() {
        val base = Api.normalizeServer(server)
        if (base.isEmpty()) { error = "Enter your PBX address."; return }
        busy = true; error = null
        scope.launch {
            try {
                val api = Api(base, null)
                val r: LoginResp = if (usePair) api.pair(code, device) else api.login(user.trim(), pass, device)
                model.store.server = base
                model.store.token = r.token
                model.store.username = r.me.username
                model.store.exten = r.me.exten
                model.notice = null
                model.stage = Stage.SetPin
            } catch (e: ApiException) {
                // 404, or the PBX's "office network only" 403: the server
                // doesn't have the app sign-in yet (older own-pbx).
                error = if (e.status == 404 || (e.status == 403 && e.message.orEmpty().contains("network")))
                    "Your PBX at $base doesn't support the app yet (HTTP ${e.status}). Check the address " +
                        "(http:// or https://, nothing after the name) or run pbx-update on the PBX."
                else "${e.message} (HTTP ${e.status} from $base)"
            } catch (e: Exception) {
                error = "Can't reach $base. Check the address (http:// or https://) and your connection."
            } finally {
                busy = false
            }
        }
    }

    Scaffold { pad ->
        Column(
            Modifier.padding(pad).fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Spacer(Modifier.height(24.dp))
            Text("My Phone", style = MaterialTheme.typography.displaySmall)
            Text("Sign in to your own PBX. You'll stay signed in for weeks.",
                style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
            OutlinedTextField(server, { server = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("PBX address") }, placeholder = { Text("https://pbx.example.com") },
                supportingText = { Text("Include http:// or https://. Without it, https:// is used.") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
            if (server.trim().startsWith("http://")) {
                Text("This address isn't encrypted (http). Only use it on your home network or VPN.",
                    color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                SegmentedButton(!usePair, { usePair = false }, SegmentedButtonDefaults.itemShape(0, 2)) { Text("Password") }
                SegmentedButton(usePair, { usePair = true }, SegmentedButtonDefaults.itemShape(1, 2)) { Text("Pairing code") }
            }
            if (usePair) {
                Text("In My Phone on the web, open the App tab and tap \"Get a pairing code\".",
                    style = MaterialTheme.typography.bodyMedium)
                OutlinedTextField(code, { code = it.uppercase().take(12) }, Modifier.fillMaxWidth(), singleLine = true,
                    label = { Text("Pairing code") },
                    keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters))
            } else {
                OutlinedTextField(user, { user = it }, Modifier.fillMaxWidth(), singleLine = true, label = { Text("Username") })
                OutlinedTextField(pass, { pass = it }, Modifier.fillMaxWidth(), singleLine = true, label = { Text("Password") },
                    visualTransformation = if (showPass) VisualTransformation.None else PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    trailingIcon = {
                        IconButton({ showPass = !showPass }) {
                            Icon(if (showPass) Icons.Filled.VisibilityOff else Icons.Filled.Visibility, "Show password")
                        }
                    })
            }
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            Button(::go, Modifier.fillMaxWidth().height(52.dp), enabled = !busy) {
                if (busy) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp)
                else Text("Sign in")
            }
            Text("Free and open-source. No tracking, no Google services.",
                Modifier.align(Alignment.CenterHorizontally),
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
