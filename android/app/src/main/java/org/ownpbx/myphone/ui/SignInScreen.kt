// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import android.os.Build
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import org.ownpbx.myphone.data.Api
import org.ownpbx.myphone.data.ApiException
import org.ownpbx.myphone.data.LoginResp

/** Sign in with a one-time pairing code from My Phone > App on the web.
 *  The app never sees the panel password. */
@Composable
fun SignInScreen(model: AppModel) {
    var server by remember { mutableStateOf(model.store.server) }
    var code by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf(model.notice) }
    val scope = rememberCoroutineScope()
    val device = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

    fun go() {
        val base = Api.normalizeServer(server)
        if (base.isEmpty()) { error = "Enter your PBX address."; return }
        if (code.count { it.isLetterOrDigit() } < 6) { error = "Enter the pairing code from My Phone > App."; return }
        busy = true; error = null
        scope.launch {
            try {
                val r: LoginResp = Api(base, null).pair(code, device)
                model.store.server = base
                model.store.token = r.token
                model.store.username = r.me.username
                model.store.exten = r.me.exten
                model.notice = null
                model.stage = Stage.SetPin
            } catch (e: ApiException) {
                error = when (e.status) {
                    // Only an older own-pbx (no app sign-in at all) answers 404/405 here.
                    404, 405 -> "Your PBX doesn't support the app yet. Update it (run pbx-update on the PBX) and try again."
                    429 -> e.message
                    401 -> e.message ?: "That code is wrong or expired. Get a new one in My Phone > App."
                    else -> "The PBX answered with an error (${e.status}). Check the address and try again."
                }
            } catch (e: Exception) {
                error = "Can't reach $base. Check the address and your connection."
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
            Text("Connect this phone to your PBX with a pairing code.",
                style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
            ElevatedCard(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("1. Sign in to My Phone in a browser.", style = MaterialTheme.typography.bodyMedium)
                    Text("2. Open the App tab and tap \"Get a pairing code\".", style = MaterialTheme.typography.bodyMedium)
                    Text("3. Enter the server and code shown there. The code works once, for 5 minutes.",
                        style = MaterialTheme.typography.bodyMedium)
                }
            }
            OutlinedTextField(server, { server = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("Server") }, placeholder = { Text("https://pbx.example.com") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next))
            if (server.trim().startsWith("http://")) {
                Text("This address isn't encrypted (http). Only use it on your home network or VPN.",
                    color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
            OutlinedTextField(code, { code = it.uppercase().filter { ch -> ch.isLetterOrDigit() || ch == '-' || ch == ' ' }.take(12) },
                Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("Pairing code") },
                textStyle = MaterialTheme.typography.titleLarge,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters,
                    keyboardType = KeyboardType.Ascii, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { if (!busy) go() }))
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            Button(::go, Modifier.fillMaxWidth().height(52.dp), enabled = !busy) {
                if (busy) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp)
                else Text("Connect")
            }
            Text("Free and open-source. No tracking, no Google services.",
                Modifier.align(Alignment.CenterHorizontally),
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
