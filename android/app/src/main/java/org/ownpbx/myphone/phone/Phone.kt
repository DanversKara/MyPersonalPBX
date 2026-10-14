// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.phone

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.linphone.core.AudioDevice
import org.linphone.core.Call
import org.linphone.core.Core
import org.linphone.core.CoreListenerStub
import org.linphone.core.Factory
import org.linphone.core.MediaEncryption
import org.linphone.core.TransportType
import org.ownpbx.myphone.data.SipInfo
import java.io.File

/** What the call screen shows. null = no call. */
data class CallUi(
    val number: String,
    val status: String,          // "Calling...", "Ringing...", "Connected", "Call ended", ...
    val connectedAt: Long = 0,   // SystemClock.elapsedRealtime() when answered, 0 = not yet
    val muted: Boolean = false,
    val speaker: Boolean = false,
    val ended: Boolean = false,
    val problem: String = "",    // why it failed, in words, plus the SIP code
)

/**
 * The built-in phone (Linphone). Outgoing calls only for now: the app does
 * not register, it sends each call to the PBX through the edge exactly like
 * a remote Zoiper would (TLS 5061, SRTP), using the extension's own phone
 * login. Nothing changes for the desk phone or Zoiper on the same extension.
 */
object Phone {
    private var core: Core? = null
    private var configuredFor: String = ""
    var ui by mutableStateOf<CallUi?>(null)
        private set

    private val listener = object : CoreListenerStub() {
        override fun onCallStateChanged(core: Core, call: Call, state: Call.State?, message: String) {
            val cur = ui ?: return
            ui = when (state) {
                Call.State.OutgoingInit, Call.State.OutgoingProgress -> cur.copy(status = "Calling...")
                Call.State.OutgoingRinging, Call.State.OutgoingEarlyMedia -> cur.copy(status = "Ringing...")
                Call.State.Connected, Call.State.StreamsRunning ->
                    cur.copy(status = "Connected", connectedAt = if (cur.connectedAt == 0L)
                        android.os.SystemClock.elapsedRealtime() else cur.connectedAt)
                Call.State.Paused, Call.State.PausedByRemote -> cur.copy(status = "On hold")
                Call.State.Error -> cur.copy(status = "Call failed", ended = true, problem = describe(call, message))
                Call.State.End, Call.State.Released -> if (cur.ended) cur else
                    cur.copy(status = if (cur.connectedAt > 0) "Call ended" else endedBeforeAnswer(call),
                        ended = true, problem = if (cur.connectedAt > 0) "" else describe(call, message))
                else -> cur
            }
            if (ui?.ended == true) CallService.stop(appCtx ?: return)
        }
    }
    private var appCtx: Context? = null

    private fun describe(call: Call, message: String): String {
        val ei = call.errorInfo
        val code = ei?.protocolCode ?: 0
        val words = when (code) {
            0 -> if (message.contains("TLS", true) || message.contains("certificate", true))
                "Couldn't make a secure connection to the PBX." else ""
            401, 403, 407 -> "The PBX refused the phone login."
            404, 484 -> "That number doesn't exist."
            480 -> "Nobody is available on that number."
            486, 600 -> "Busy."
            488, 606 -> "The call settings didn't match the PBX (audio/encryption)."
            503 -> "The PBX couldn't place the call right now."
            else -> ""
        }
        val raw = listOfNotNull(ei?.phrase?.takeIf { it.isNotBlank() }, message.takeIf { it.isNotBlank() })
            .distinct().joinToString(" - ")
        return listOf(words, if (code > 0) "SIP $code" else "", raw).filter { it.isNotBlank() }.joinToString(" · ")
    }

    private fun endedBeforeAnswer(call: Call) = when (call.errorInfo?.protocolCode ?: 0) {
        486, 600 -> "Busy"
        480, 408 -> "No answer"
        0 -> "Call ended"
        else -> "Call failed"
    }

    /** Android's trusted CAs as one PEM file, so TLS to the edge is verified
     *  against the same roots the phone trusts (the edge has a real cert). */
    private fun systemCaBundle(ctx: Context): String? = try {
        val out = File(ctx.filesDir, "rootca.pem")
        if (!out.exists() || out.length() < 1000) {
            val sb = StringBuilder()
            val dirs = listOf(File("/apex/com.android.conscrypt/cacerts"), File("/system/etc/security/cacerts"))
            val dir = dirs.firstOrNull { it.isDirectory && (it.list()?.isNotEmpty() == true) }
            dir?.listFiles()?.forEach { f ->
                val t = f.readText()
                val s = t.indexOf("-----BEGIN CERTIFICATE-----")
                val e = t.indexOf("-----END CERTIFICATE-----")
                if (s >= 0 && e > s) sb.append(t, s, e + "-----END CERTIFICATE-----".length).append('\n')
            }
            if (sb.isNotEmpty()) out.writeText(sb.toString())
        }
        out.takeIf { it.exists() && it.length() > 1000 }?.path
    } catch (e: Exception) { null }

    /** Create / reconfigure the engine for this extension. Main thread. */
    private fun ensure(ctx: Context, sip: SipInfo): Core {
        appCtx = ctx.applicationContext
        val key = "${sip.username}@${sip.domain}:${sip.port}"
        val f = Factory.instance()
        val c = core ?: f.createCore(null, null, ctx.applicationContext).also { c ->
            c.addListener(listener)
            c.setUserAgent("MyPhone", org.ownpbx.myphone.BuildConfig.VERSION_NAME)
            systemCaBundle(ctx)?.let { c.rootCa = it }
            // Local listening transports: only TLS (random port) is needed to call out.
            val t = c.transports
            t.udpPort = 0; t.tcpPort = 0; t.tlsPort = -1
            c.transports = t
            c.mediaEncryption = MediaEncryption.SRTP
            c.isMediaEncryptionMandatory = true
            c.start()
            core = c
        }
        if (configuredFor != key) {
            c.clearAccounts()
            c.clearAllAuthInfo()
            c.addAuthInfo(f.createAuthInfo(sip.username, null, sip.password, null, null, sip.domain, null))
            val p = c.createAccountParams()
            p.identityAddress = f.createAddress("sip:${sip.username}@${sip.domain}")
            p.serverAddress = f.createAddress("sip:${sip.domain}:${sip.port}")?.also { it.transport = TransportType.Tls }
            p.isRegisterEnabled = false         // outgoing only: no registration, other phones unaffected
            val acc = c.createAccount(p)
            c.addAccount(acc)
            c.defaultAccount = acc
            // Only the codecs the PBX accepts (ulaw, alaw, G.722).
            // telephone-event = keypad tones (RFC 4733) for IVR menus and voicemail.
            val want = sip.codecs.map { it.uppercase() }.toSet() + "TELEPHONE-EVENT"
            c.audioPayloadTypes.forEach { pt -> pt.enable(pt.mimeType.uppercase() in want) }
            configuredFor = key
        }
        return c
    }

    /** Place a call. Returns an error message, or null when it started. */
    fun call(ctx: Context, sip: SipInfo, number: String): String? {
        if (ui != null && ui?.ended == false) return "You're already on a call."
        val dial = number.filter { it.isDigit() || it == '*' || it == '#' || it == '+' }
        if (dial.isEmpty()) return "Enter a number."
        val c = try { ensure(ctx, sip) } catch (e: Exception) { return "The phone engine couldn't start (${e.javaClass.simpleName})." }
        val to = Factory.instance().createAddress("sip:$dial@${sip.domain}")
            ?: return "That number can't be dialled."
        to.transport = TransportType.Tls
        val params = c.createCallParams(null) ?: return "The phone engine couldn't start."
        params.mediaEncryption = MediaEncryption.SRTP
        ui = CallUi(number = dial, status = "Calling...")
        CallService.start(ctx, dial)
        if (c.inviteAddressWithParams(to, params) == null) {
            ui = CallUi(number = dial, status = "Call failed", ended = true, problem = "The call couldn't be started.")
            CallService.stop(ctx)
        }
        return null
    }

    private fun current(): Call? = core?.currentCall ?: core?.calls?.firstOrNull()

    fun hangUp() {
        val call = current()
        if (call != null) call.terminate()
        else ui = ui?.copy(status = "Call ended", ended = true)
        appCtx?.let { CallService.stop(it) }
    }

    fun toggleMute() {
        val call = current() ?: return
        call.microphoneMuted = !call.microphoneMuted
        ui = ui?.copy(muted = call.microphoneMuted)
    }

    fun toggleSpeaker() {
        val call = current() ?: return
        val c = core ?: return
        val wantSpeaker = !(ui?.speaker ?: false)
        val type = if (wantSpeaker) AudioDevice.Type.Speaker else AudioDevice.Type.Earpiece
        c.audioDevices.firstOrNull { it.type == type }?.let { call.outputAudioDevice = it }
        ui = ui?.copy(speaker = wantSpeaker)
    }

    fun dtmf(ch: Char) { current()?.sendDtmf(ch) }

    /** Close the "Call ended" screen. */
    fun dismiss() { if (ui?.ended == true) ui = null }
}
