// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material.icons.filled.OpenInBrowser
import androidx.compose.ui.platform.LocalContext
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import org.ownpbx.myphone.data.PairLink
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
        if (busy) return
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
                    404, 405 -> "$base answered ${e.status} (not found). Check the address is just your PBX, like https://myaccount.example.com. If it is, update the PBX (pbx-update)."
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

    val ctx = LocalContext.current

    /** Open My Phone in an in-app browser tab: sign in there (password or
     *  single sign-on) and the PBX sends this app a pairing code. */
    fun browserSignIn() {
        val base = Api.normalizeServer(server)
        if (base.isEmpty()) { error = "Enter your PBX address first."; return }
        val bytes = ByteArray(18).also { java.security.SecureRandom().nextBytes(it) }
        val state = android.util.Base64.encodeToString(bytes,
            android.util.Base64.URL_SAFE or android.util.Base64.NO_WRAP or android.util.Base64.NO_PADDING)
        model.store.browserState = state
        model.store.browserServer = base
        model.store.browserAt = System.currentTimeMillis()
        error = null
        val url = android.net.Uri.parse("$base/ucp/app/connect?st=$state")
        try {
            androidx.browser.customtabs.CustomTabsIntent.Builder().setShowTitle(true).build().launchUrl(ctx, url)
        } catch (e: Exception) {
            try { ctx.startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, url)) }
            catch (e2: Exception) { error = "No web browser found on this phone." }
        }
    }

    // A pairing link arrived. If it answers our own "Sign in with my browser"
    // (same state, same server, recent), connect straight away; otherwise
    // (camera, another app) fill it in and let the person check and tap Connect.
    LaunchedEffect(model.pendingPair) {
        model.pendingPair?.let { p ->
            val st = model.store
            val ours = p.state != null && p.state == st.browserState &&
                System.currentTimeMillis() - st.browserAt < 15 * 60_000L &&
                (p.server == null || Api.normalizeServer(p.server) == st.browserServer)
            p.server?.let { server = it }
            code = p.code
            error = null
            model.pendingPair = null
            if (p.state != null) { st.browserState = ""; st.browserAt = 0 }
            if (ours) go()
            else if (p.state != null) error = "Check the server address, then tap Connect."
        }
    }

    // In-app scanner: the user meant to scan, so connect straight away.
    val scanner = rememberLauncherForActivityResult(ScanContract()) { result ->
        val text = result.contents ?: return@rememberLauncherForActivityResult   // cancelled
        val p = PairLink.parse(text)
        if (p == null) { error = "That isn't a My Phone pairing code. Use the QR code on My Phone > App."; return@rememberLauncherForActivityResult }
        p.server?.let { server = it }
        code = p.code
        go()
    }
    fun scan() {
        try {
            scanner.launch(ScanOptions()
                .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt("Scan the QR code from My Phone > App")
                .setBeepEnabled(false)
                .setOrientationLocked(false))
        } catch (e: Exception) {
            error = "The scanner couldn't start (${e.javaClass.simpleName}). " +
                "Scan the QR code with your phone's camera app instead, or type the code."
        }
    }

    Scaffold { pad ->
        Column(
            Modifier.padding(pad).fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Spacer(Modifier.height(24.dp))
            Text("My Phone", style = MaterialTheme.typography.displaySmall)
            Text("Connect this phone to your PBX.",
                style = MaterialTheme.typography.bodyLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
            OutlinedTextField(server, { server = it }, Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("Server") }, placeholder = { Text("https://pbx.example.com") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next))
            if (server.trim().startsWith("http://")) {
                Text("This address isn't encrypted (http). Only use it on your home network or VPN.",
                    color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
            Button(::browserSignIn, Modifier.fillMaxWidth().height(52.dp), enabled = !busy) {
                Icon(Icons.Filled.OpenInBrowser, null)
                Spacer(Modifier.width(8.dp))
                Text("Sign in with my browser")
            }
            Text("Opens My Phone here in the app. Sign in, and this phone connects by itself.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Row(verticalAlignment = Alignment.CenterVertically) {
                HorizontalDivider(Modifier.weight(1f))
                Text("  or with a pairing code  ", style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                HorizontalDivider(Modifier.weight(1f))
            }
            ElevatedCard(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("1. Sign in to My Phone in a browser.", style = MaterialTheme.typography.bodyMedium)
                    Text("2. Open the App tab and tap \"Get a pairing code\".", style = MaterialTheme.typography.bodyMedium)
                    Text("3. Scan the QR code, or type the code below. It works once, for 5 minutes.",
                        style = MaterialTheme.typography.bodyMedium)
                }
            }
            OutlinedButton(::scan, Modifier.fillMaxWidth().height(52.dp), enabled = !busy) {
                Icon(Icons.Filled.QrCodeScanner, null)
                Spacer(Modifier.width(8.dp))
                Text("Scan QR code")
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                HorizontalDivider(Modifier.weight(1f))
                Text("  or type it  ", style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                HorizontalDivider(Modifier.weight(1f))
            }
            OutlinedTextField(code, { code = it.uppercase().filter { ch -> ch.isLetterOrDigit() || ch == '-' || ch == ' ' }.take(12) },
                Modifier.fillMaxWidth(), singleLine = true,
                label = { Text("Pairing code") },
                textStyle = MaterialTheme.typography.titleLarge,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Characters,
                    keyboardType = KeyboardType.Ascii, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { if (!busy) go() }))
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            FilledTonalButton(::go, Modifier.fillMaxWidth().height(52.dp), enabled = !busy) {
                if (busy) CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp)
                else Text("Connect")
            }
            Text("Free and open-source. No tracking, no Google services.",
                Modifier.align(Alignment.CenterHorizontally),
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
