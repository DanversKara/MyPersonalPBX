// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import org.ownpbx.myphone.CrashLog

@Composable
fun AppRoot(model: AppModel, activity: FragmentActivity) {
    CrashReportDialog()
    // A call (ringing or in progress) shows over everything, including the
    // PIN lock: answering shouldn't need the PIN. Only call controls show.
    org.ownpbx.myphone.phone.Phone.ui?.let { c ->
        if (model.stage != Stage.SignIn) { InCallScreen(c); return }
    }
    when (model.stage) {
        Stage.SignIn -> SignInScreen(model)
        Stage.SetPin -> SetPinScreen(model)
        Stage.Locked -> LockScreen(model, activity)
        Stage.Ready -> MainScreen(model)
    }
}

/** "The app closed unexpectedly last time" with the error and a Copy button. */
@Composable
private fun CrashReportDialog() {
    val ctx = LocalContext.current
    var report by remember { mutableStateOf(CrashLog.read(ctx)) }
    val text = report ?: return
    fun dismiss() { CrashLog.clear(ctx); report = null }
    AlertDialog(
        onDismissRequest = ::dismiss,
        title = { Text("The app closed unexpectedly") },
        text = {
            SelectionContainer {
                Text("Copy this and send it to whoever looks after your PBX:\n\n$text",
                    Modifier.heightIn(max = 360.dp).verticalScroll(rememberScrollState()),
                    style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace))
            }
        },
        confirmButton = {
            TextButton({
                (ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
                    .setPrimaryClip(ClipData.newPlainText("My Phone crash", text))
                dismiss()
            }) { Text("Copy and close") }
        },
        dismissButton = { TextButton(::dismiss) { Text("Close") } },
    )
}
