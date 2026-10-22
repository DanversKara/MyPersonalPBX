// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.data

import android.content.Context
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

/**
 * Offline copy of the user's PBX contacts, for when the phone has no
 * internet or the PBX is unreachable (degoogled phone, emergency).
 * Refreshed on every successful API sync; read when the network fails.
 */
class ContactCache(context: Context) {
    private val file = File(context.applicationContext.filesDir, "contacts-cache.json")
    private val json = Json { ignoreUnknownKeys = true }

    fun save(contacts: List<Contact>) {
        try {
            file.writeText(json.encodeToString(Contacts(contacts)))
        } catch (_: Exception) {
        }
    }

    fun load(): List<Contact> {
        return try {
            if (!file.exists()) emptyList()
            else json.decodeFromString<Contacts>(file.readText()).contacts
        } catch (_: Exception) {
            emptyList()
        }
    }

    fun lastUpdated(): Long = if (file.exists()) file.lastModified() else 0L
}
