// SPDX-License-Identifier: GPL-2.0-or-later
@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class, androidx.compose.foundation.ExperimentalFoundationApi::class)

package org.ownpbx.myphone.ui

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.ownpbx.myphone.data.Api
import org.ownpbx.myphone.data.ApiException
import org.ownpbx.myphone.data.Store

enum class Stage { SignIn, SetPin, Locked, Ready }

/** App-wide state: where we are (sign in / PIN / main) and error messages. */
class AppModel(val store: Store) {
    var stage by mutableStateOf(initialStage())
    /** One-shot message for the snackbar. */
    var notice by mutableStateOf<String?>(null)
    var onSecureChanged: () -> Unit = {}

    private fun initialStage() = when {
        store.token == null -> Stage.SignIn
        store.pinHash == null -> Stage.SetPin
        else -> Stage.Locked
    }

    fun api(): Api = store.api()

    fun lock() {
        if (stage == Stage.Ready && store.pinHash != null) stage = Stage.Locked
    }

    fun signedOut(message: String? = null) {
        store.wipe()
        stage = Stage.SignIn
        notice = message
    }

    /** Run an API call; 401 signs out, other errors become a snackbar. */
    suspend fun <T> guard(block: suspend (Api) -> T): T? = try {
        block(api())
    } catch (e: ApiException) {
        if (e.status == 401) signedOut("You were signed out. Please sign in again.")
        else notice = e.message
        null
    } catch (e: Exception) {
        notice = "Can't reach your PBX. Check your connection."
        null
    }

    /** Like guard, but other errors stay silent (background housekeeping). */
    suspend fun <T> guardQuiet(block: suspend (Api) -> T): T? = try {
        block(api())
    } catch (e: ApiException) {
        if (e.status == 401) signedOut("You were signed out. Please sign in again.")
        null
    } catch (e: Exception) {
        null
    }

    /** For change calls: show the server's answer, return whether it worked. */
    suspend fun change(block: suspend (Api) -> org.ownpbx.myphone.data.Result): Boolean {
        val r = guard(block) ?: return false
        if (!r.ok || r.code !in setOf("saved", "deleted", "msgsent", "ok", "vmdel", "recdel")) notice = r.message
        return r.ok
    }
}
