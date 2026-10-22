// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.data

import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.FormBody
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

@Serializable
data class Who(
    val username: String,
    val exten: String = "",
    val display_name: String = "",
    val role: String = "user",
)

@Serializable data class LoginResp(val token: String, val ttl_days: Int = 60, val me: Who)

@Serializable
data class Msg(
    val id: Long,
    val peer: String,
    val mine: Boolean,
    val body: String,
    val sent_at: String = "",
    val via_did: String = "",
)

@Serializable
data class ChatThread(
    val peer: String,
    val last: Msg,
    val count: Int = 1,
    val blocked: Boolean = false,
    val peer_name: String = "",
)

@Serializable data class Threads(val threads: List<ChatThread> = emptyList())

@Serializable data class ThreadResp(val peer: String, val messages: List<Msg> = emptyList())

@Serializable data class Since(val messages: List<Msg> = emptyList())

@Serializable
data class Overview(
    val me: Who,
    val dnd: Boolean = false,
    val new_voicemail: Int = 0,
    val missed_24h: Int = 0,
    /** Missed calls this phone hasn't looked at yet (cleared by callsSeen). */
    val missed_new: Int? = null,
    val texts_left: Int? = null,
    val registered: Boolean? = null,
    val latest_message_id: Long = 0,
    val locked: Boolean = false,
)

@Serializable
data class Call(
    val id: Long,
    val kind: String = "",
    val other: String = "",
    val started_at: String = "",
    val duration_sec: Int? = null,
    val bill_sec: Int? = null,
    val disposition: String? = null,
    val recording_id: Long? = null,
    val hidden: Boolean = false,
    val peer_name: String = "",
)

@Serializable data class Calls(val calls: List<Call> = emptyList())

@Serializable
data class Vm(
    val id: Long,
    val caller: String = "",
    val received_at: String = "",
    val duration_sec: Int = 0,
    val folder: String = "INBOX",
    val is_read: Int = 0,
    val caller_name: String = "",
)

@Serializable data class Vms(val folder: String = "INBOX", val voicemail: List<Vm> = emptyList())

@Serializable
data class BlockRule(val number: String, val action: String = "", val target: String = "")

@Serializable data class IvrMenu(val id: Long, val name: String)

@Serializable
data class Settings(
    val dnd: Boolean = false,
    val forward_always: String = "",
    val forward_noanswer: String = "",
    val ring_seconds: Int = 30,
    val ring_choices: List<Int> = listOf(10, 15, 20, 25, 30, 45, 60, 90, 120),
    val answer_ivr_id: Long? = null,
    val ivr_menus: List<IvrMenu> = emptyList(),
    val user_record: Boolean = false,
    val record_allowed: Boolean = false,
    val record_ack_needed: Boolean = true,
    val user_ptt: Boolean = false,
    val ptt_allowed: Boolean = false,
    val vm_email: String = "",
    val text_email: Boolean = true,
    val msg_in: Boolean = true,
    val msg_out: Boolean = true,
    val blocked: List<BlockRule> = emptyList(),
)

@Serializable
data class Device(
    val id: Long,
    val name: String = "",
    val created_at: String = "",
    val last_used_at: String? = null,
    val this_device: Boolean = false,
)

@Serializable data class Devices(val devices: List<Device> = emptyList())

@Serializable
data class SipInfo(
    val enabled: Boolean = false,
    val reason: String = "",
    val exten: String = "",
    val display_name: String = "",
    val username: String = "",
    val password: String = "",
    val domain: String = "",
    val port: Int = 5061,
    val main_username: String = "",
    val register: Boolean = false,
    val codecs: List<String> = listOf("PCMU", "PCMA", "G722"),
)

@Serializable
data class WaitResp(
    val msg: Long = 0,
    val call: Long = 0,
    val vm: Long = 0,
    val changed: Boolean = false,
)

@Serializable
data class Result(val ok: Boolean = false, val code: String = "", val message: String = "")

@Serializable
data class Contact(
    val id: Long,
    val name: String = "",
    val number: String = "",
    val number_norm: String = "",
    val org: String = "",
    val email: String = "",
    val notes_count: Int = 0,
    val last_at: String = "",
    /**
     * True when this contact can receive push-to-talk calls (their admin flag and their own PTT
     * wish are both on).
     */
    val ptt: Boolean = false,
)

@Serializable data class Contacts(val contacts: List<Contact> = emptyList())

@Serializable
data class NoteItem(
    val id: Long,
    val contact_id: Long,
    val body: String = "",
    val item_type: String = "",
    val item_id: Long? = null,
    val item_at: String = "",
    val created_at: String = "",
)

@Serializable
data class TimelineItem(
    val type: String = "",
    val id: Long = 0,
    val at: String = "",
    val name: String = "",
    val number: String = "",
    val direction: String = "",
    val duration_sec: Int = 0,
    val body: String = "",
    val note_count: Int = 0,
)

@Serializable
data class ContactDetail(
    val contact: Contact,
    val timeline: List<TimelineItem> = emptyList(),
    val notes: List<NoteItem> = emptyList(),
)

@Serializable data class TimelineResp(val items: List<TimelineItem> = emptyList())

/** HTTP 401 means the token is gone (signed out elsewhere / expired). */
class ApiException(val status: Int, message: String) : Exception(message)

/** Thin client for pbx-api's /api/v1/app and /api/v1/me endpoints. */
class Api(base: String, private val token: String?) {
    private val base = base.trim().trimEnd('/')

    companion object {
        val json = Json {
            ignoreUnknownKeys = true
            coerceInputValues = true
            explicitNulls = false
        }
        val http: OkHttpClient =
            OkHttpClient.Builder()
                .connectTimeout(15, TimeUnit.SECONDS)
                .readTimeout(30, TimeUnit.SECONDS)
                .build()
        /** For /me/wait, which holds the request open up to ~55 s. */
        val httpLong: OkHttpClient = http.newBuilder().readTimeout(75, TimeUnit.SECONDS).build()

        fun normalizeServer(s: String): String {
            var u = s.trim().trimEnd('/')
            if (u.isNotEmpty() && !u.startsWith("http://") && !u.startsWith("https://"))
                u = "https://$u"
            return u
        }
    }

    fun url(path: String) = "$base/api/v1$path"

    fun authHeaders(): Map<String, String> =
        token?.let { mapOf("Authorization" to "Bearer $it") } ?: emptyMap()

    private fun errorText(body: String, code: Int): String =
        try {
            val o = json.parseToJsonElement(body) as JsonObject
            val m = o["message"] ?: o["detail"]
            if (m is JsonPrimitive) m.content else "Error $code"
        } catch (e: Exception) {
            "Error $code"
        }

    private suspend fun exec(b: Request.Builder): String =
        withContext(Dispatchers.IO) {
            authHeaders().forEach { (k, v) -> b.header(k, v) }
            http.newCall(b.build()).execute().use { r ->
                val text = r.body?.string() ?: ""
                if (!r.isSuccessful) throw ApiException(r.code, errorText(text, r.code))
                text
            }
        }

    private suspend inline fun <reified T> get(path: String, q: Map<String, Any?> = emptyMap()): T {
        val u = url(path).toHttpUrl().newBuilder()
        q.forEach { (k, v) -> if (v != null) u.addQueryParameter(k, v.toString()) }
        return json.decodeFromString(exec(Request.Builder().url(u.build())))
    }

    private suspend inline fun <reified T> postJson(path: String, body: JsonObject): T =
        json.decodeFromString(
            exec(
                Request.Builder()
                    .url(url(path))
                    .post(body.toString().toRequestBody("application/json".toMediaType()))
            )
        )

    /** Form POST like the web panel's forms. List values repeat the key. */
    suspend fun form(path: String, fields: List<Pair<String, String>>): Result {
        val fb = FormBody.Builder()
        fields.forEach { (k, v) -> fb.add(k, v) }
        return try {
            json.decodeFromString(exec(Request.Builder().url(url(path)).post(fb.build())))
        } catch (e: ApiException) {
            if (e.status == 401) throw e
            Result(false, "error", e.message ?: "Failed")
        }
    }

    private fun obj(vararg p: Pair<String, String>) =
        JsonObject(p.associate { it.first to JsonPrimitive(it.second) })

    // ---- sign in (pairing code only; the app never handles the panel password)
    suspend fun pair(code: String, device: String): LoginResp =
        postJson("/app/pair", obj("code" to code, "device_name" to device))

    suspend fun logout() {
        exec(Request.Builder().url(url("/app/logout")).post(ByteArray(0).toRequestBody()))
    }

    // ---- reads
    /** Long poll: returns when something newer than these ids exists, or after [timeout] s. */
    suspend fun wait(msg: Long, call: Long, vm: Long, timeout: Int = 50): WaitResp =
        withContext(Dispatchers.IO) {
            val u =
                url("/me/wait")
                    .toHttpUrl()
                    .newBuilder()
                    .addQueryParameter("msg", msg.toString())
                    .addQueryParameter("call", call.toString())
                    .addQueryParameter("vm", vm.toString())
                    .addQueryParameter("timeout", timeout.toString())
                    .build()
            val b = Request.Builder().url(u)
            authHeaders().forEach { (k, v) -> b.header(k, v) }
            httpLong.newCall(b.build()).execute().use { r ->
                val text = r.body?.string() ?: ""
                if (!r.isSuccessful) throw ApiException(r.code, errorText(text, r.code))
                json.decodeFromString<WaitResp>(text)
            }
        }

    suspend fun overview(): Overview = get("/me/overview")

    suspend fun threads(): Threads = get("/me/threads")

    suspend fun thread(peer: String): ThreadResp =
        get("/me/threads/${java.net.URLEncoder.encode(peer, "UTF-8")}", mapOf("limit" to 300))

    suspend fun since(after: Long): Since = get("/me/messages/since", mapOf("after" to after))

    /** view: "all", "missed" or "hidden". */
    suspend fun calls(view: String): Calls =
        get("/me/calls", mapOf("view" to view, "missed" to if (view == "missed") 1 else 0))

    suspend fun voicemail(folder: String): Vms = get("/me/voicemail", mapOf("folder" to folder))

    suspend fun settings(): Settings = get("/me/settings")

    /** Phone settings for the built-in phone (edge TLS + SRTP). */
    suspend fun sip(): SipInfo = get("/me/sip")

    suspend fun devices(): Devices = get("/me/devices")

    fun voicemailAudioUrl(id: Long) = url("/me/voicemail/$id/audio")

    /**
     * Download a voicemail to [dest] with this client (token + same TLS/proxy path as every other
     * call), so playback never depends on MediaPlayer's own HTTP stack.
     */
    suspend fun downloadVoicemail(id: Long, dest: java.io.File) =
        withContext(Dispatchers.IO) {
            val b = Request.Builder().url(voicemailAudioUrl(id))
            authHeaders().forEach { (k, v) -> b.header(k, v) }
            http.newCall(b.build()).execute().use { r ->
                if (!r.isSuccessful)
                    throw ApiException(r.code, errorText(r.body?.string() ?: "", r.code))
                val tmp = java.io.File(dest.path + ".part")
                r.body!!.byteStream().use { inp -> tmp.outputStream().use { inp.copyTo(it) } }
                if (!tmp.renameTo(dest)) {
                    tmp.copyTo(dest, overwrite = true)
                    tmp.delete()
                }
            }
            dest
        }

    fun recordingAudioUrl(id: Long) = url("/me/recordings/$id/audio")

    suspend fun deleteDevice(id: Long) {
        exec(Request.Builder().url(url("/me/devices/$id")).delete())
    }

    // ---- contacts + notes
    suspend fun contacts(): Contacts = get("/me/contacts")

    suspend fun contactDetail(id: Long): ContactDetail = get("/me/contacts/$id")

    suspend fun timeline(limit: Int = 200): TimelineResp =
        get("/me/timeline", mapOf("limit" to limit))

    // ---- changes
    suspend fun send(to: String, body: String) =
        form("/me/messages/send", listOf("to" to to, "body" to body))

    suspend fun deleteMessages(ids: List<Long> = emptyList(), peers: List<String> = emptyList()) =
        form(
            "/me/messages/delete",
            ids.map { "ids" to it.toString() } + peers.map { "convs" to it },
        )

    suspend fun hideCalls(ids: Collection<Long>, unhide: Boolean = false) =
        form(
            "/me/calls/hide",
            ids.map { "ids" to it.toString() } +
                if (unhide) listOf("unhide" to "1") else emptyList(),
        )

    suspend fun callsSeen() {
        exec(Request.Builder().url(url("/me/calls/seen")).post(ByteArray(0).toRequestBody()))
    }

    suspend fun deleteVoicemail(ids: List<Long>, folder: String) =
        form("/me/voicemail/delete", ids.map { "ids" to it.toString() } + ("folder" to folder))

    suspend fun heard(id: Long) = form("/me/voicemail/$id/heard", emptyList())

    suspend fun moveVoicemail(id: Long, to: String) =
        form("/me/voicemail/$id/move", listOf("to" to to))

    suspend fun setDnd(on: Boolean) = form("/me/dnd", listOf("on" to if (on) "1" else "0"))

    suspend fun saveCalls(s: Settings) =
        form(
            "/me/settings/calls",
            buildList {
                add("dnd" to if (s.dnd) "1" else "0")
                add("forward_always" to s.forward_always)
                add("forward_noanswer" to s.forward_noanswer)
                add("ring_seconds" to s.ring_seconds.toString())
                add("answer_ivr_id" to (s.answer_ivr_id?.toString() ?: ""))
                // The PTT wish lives on THIS POST on the server: it is only
                // applied when the field is carried, and absence preserves
                // the stored wish — so always send it explicitly ("1"/"0"),
                // never checkbox-style, or turning PTT off wouldn't stick.
                add("user_ptt" to if (s.user_ptt) "1" else "0")
            },
        )

    suspend fun savePrefs(s: Settings, recordAck: Boolean) =
        form(
            "/me/settings/prefs",
            buildList {
                if (s.user_record) add("user_record" to "1")
                // Sent explicitly here too (see saveCalls): harmless if the
                // prefs handler ignores it, correct if it ever reads it.
                add("user_ptt" to if (s.user_ptt) "1" else "0")
                if (recordAck) add("record_ack" to "1")
                if (s.text_email) add("text_email" to "1")
                add("vm_email" to s.vm_email)
            },
        )

    suspend fun saveMessaging(msgIn: Boolean, msgOut: Boolean) =
        form(
            "/me/settings/messaging",
            buildList {
                if (msgIn) add("msg_in" to "1")
                if (msgOut) add("msg_out" to "1")
            },
        )

    suspend fun block(number: String) =
        form("/me/block", listOf("number" to number, "back" to "settings"))

    suspend fun unblock(number: String) =
        form("/me/block", listOf("number" to number, "action" to "remove", "back" to "settings"))

    suspend fun changePassword(cur: String, new: String) =
        form("/me/settings/password", listOf("current" to cur, "new" to new, "confirm" to new))

    suspend fun saveContact(id: Long?, name: String, number: String, org: String, email: String) =
        form(
            "/me/contacts/save",
            buildList {
                if (id != null) add("id" to id.toString())
                add("name" to name)
                add("number" to number)
                add("org" to org)
                add("email" to email)
            },
        )

    suspend fun deleteContacts(ids: List<Long>) =
        form("/me/contacts/delete", ids.map { "ids" to it.toString() })

    suspend fun saveNote(
        contactId: Long,
        body: String,
        itemType: String = "",
        itemId: Long? = null,
    ) =
        form(
            "/me/notes/save",
            buildList {
                add("contact_id" to contactId.toString())
                add("body" to body)
                if (itemType.isNotBlank()) add("item_type" to itemType)
                if (itemId != null) add("item_id" to itemId.toString())
            },
        )

    suspend fun deleteNotes(ids: List<Long>) =
        form("/me/notes/delete", ids.map { "ids" to it.toString() })
}
