// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.phone

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.linphone.core.Account
import org.linphone.core.AudioDevice
import org.linphone.core.Call
import org.linphone.core.Core
import org.linphone.core.CoreListenerStub
import org.linphone.core.Factory
import org.linphone.core.MediaEncryption
import org.linphone.core.Reason
import org.linphone.core.RegistrationState
import org.linphone.core.TransportType
import org.ownpbx.myphone.data.SipInfo
import java.io.File

/** What the call screen shows. null = no call. */
data class CallUi(
    val number: String,
    val status: String,          // "Calling...", "Ringing...", "Incoming call", "Connected", "Call ended", ...
    val connectedAt: Long = 0,   // SystemClock.elapsedRealtime() when answered, 0 = not yet
    val muted: Boolean = false,
    val speaker: Boolean = false,
    val ended: Boolean = false,
    val problem: String = "",    // why it failed, in words, plus the SIP code
    val incoming: Boolean = false,
    val name: String = "",       // caller's name, if the PBX sent one
)

/**
 * The built-in phone (Linphone).
 *
 * Calls go through the edge exactly like a remote Zoiper (TLS 5061, SRTP).
 * To receive calls the app signs in (registers) on the extension's own app
 * line, `<sip_username>-app` (see config-gen pjsip.conf.j2), never on the
 * main line, so the desk phone / Zoiper stay signed in. pbx-brain rings the
 * app line together with the main one. A login that isn't an app line is
 * used for outgoing calls only.
 *
 * Linphone must be used from the main thread.
 */
object Phone {
    private var core: Core? = null
    private var configuredFor: String = ""
    var ui by mutableStateOf<CallUi?>(null)
        private set
    /** Sign-in state for incoming calls, in words (Settings shows it). */
    var registration by mutableStateOf("Not signed in")
        private set
    var registered by mutableStateOf(false)
        private set

    private var appCtx: Context? = null

    private val listener = object : CoreListenerStub() {
        override fun onCallStateChanged(core: Core, call: Call, state: Call.State?, message: String) {
            if (state == Call.State.IncomingReceived) { incoming(call); return }
            val cur = ui ?: return
            ui = when (state) {
                Call.State.OutgoingInit, Call.State.OutgoingProgress -> cur.copy(status = "Calling...")
                Call.State.OutgoingRinging, Call.State.OutgoingEarlyMedia -> cur.copy(status = "Ringing...")
                Call.State.Connected, Call.State.StreamsRunning ->
                    cur.copy(status = "Connected", connectedAt = if (cur.connectedAt == 0L)
                        android.os.SystemClock.elapsedRealtime() else cur.connectedAt)
                Call.State.Paused, Call.State.PausedByRemote -> cur.copy(status = "On hold")
                Call.State.Error -> cur.copy(status = "Call failed", ended = true, problem = describe(call, message))
                Call.State.End, Call.State.Released -> if (cur.ended) cur else when {
                    cur.connectedAt > 0 -> cur.copy(status = "Call ended", ended = true)
                    // Caller hung up / someone else answered: just a missed call, no error.
                    cur.incoming -> cur.copy(status = "Missed call", ended = true)
                    else -> cur.copy(status = endedBeforeAnswer(call), ended = true, problem = describe(call, message))
                }
                else -> cur
            }
            if (state == Call.State.Connected || state == Call.State.StreamsRunning) appCtx?.let { IncomingNotice.cancel(it) }
            if (ui?.ended == true) appCtx?.let { CallService.stop(it); IncomingNotice.cancel(it) }
        }

        override fun onAccountRegistrationStateChanged(core: Core, account: Account, state: RegistrationState?, message: String) {
            registered = state == RegistrationState.Ok
            registration = when (state) {
                RegistrationState.Ok -> "Signed in: this phone rings for incoming calls"
                RegistrationState.Progress, RegistrationState.Refreshing -> "Signing in..."
                RegistrationState.Failed -> "Couldn't sign in" + (account.errorInfo?.let { ei ->
                    val code = ei.protocolCode
                    when (code) {
                        401, 403, 407 -> ": the PBX refused the phone login. Tap Get from my PBX."
                        0 -> ": can't reach ${account.params.serverAddress?.domain ?: "the PBX"}."
                        else -> " (SIP $code ${ei.phrase ?: ""})."
                    }
                } ?: ".")
                RegistrationState.Cleared, RegistrationState.None, null -> "Not signed in"
                else -> state.name
            }
        }
    }

    private fun incoming(call: Call) {
        val ctx = appCtx
        // Already on a call: tell the PBX we're busy (the other phones still ring).
        if (ui != null && ui?.ended == false) { call.decline(Reason.Busy); return }
        val from = call.remoteAddress
        val number = from.username.orEmpty()
        ui = CallUi(number = number, name = from.displayName.orEmpty().takeIf { it != number }.orEmpty(),
            status = "Incoming call", incoming = true)
        if (ctx != null) IncomingNotice.show(ctx, number, ui?.name.orEmpty())
    }

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
        val raw = listOfNotNull(ei?.phrase?.takeIf { it.isNotBlank() }, message.takeIf { it.isNotBlank() },
            call.reason?.name?.takeIf { it != "None" }?.let { "reason $it" })
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

    /** Only the app's own line may register; registering the main line would
     *  replace the desk phone / Zoiper (max_contacts=1 on the PBX). */
    fun canRegister(sip: SipInfo) = sip.username.endsWith("-app")

    /** Create / reconfigure the engine for this login. Main thread. */
    private fun ensure(ctx: Context, sip: SipInfo, register: Boolean): Core {
        appCtx = ctx.applicationContext
        val reg = register && canRegister(sip)
        val key = "${sip.username}@${sip.domain}:${sip.port}#${sip.password.hashCode()}#$reg"
        val f = Factory.instance()
        val c = core ?: f.createCore(null, null, ctx.applicationContext).also { c ->
            c.addListener(listener)
            c.setUserAgent("MyPhone", org.ownpbx.myphone.BuildConfig.VERSION_NAME)
            systemCaBundle(ctx)?.let { c.rootCa = it }
            // Local listening transports: TLS only (random port).
            val t = c.transports
            t.udpPort = 0; t.tcpPort = 0; t.tlsPort = -1
            c.transports = t
            c.mediaEncryption = MediaEncryption.SRTP
            c.isMediaEncryptionMandatory = true
            c.isKeepAliveEnabled = true             // keep the TLS connection to the edge open
            c.isNativeRingingEnabled = true         // ring with the phone's own ringtone
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
            p.isRegisterEnabled = reg
            p.expires = 600
            val acc = c.createAccount(p)
            c.addAccount(acc)
            c.defaultAccount = acc
            // Only the codecs the PBX accepts (ulaw, alaw, G.722).
            // telephone-event = keypad tones (RFC 4733) for IVR menus and voicemail.
            val want = sip.codecs.map { it.uppercase() }.toSet() + "TELEPHONE-EVENT"
            c.audioPayloadTypes.forEach { pt -> pt.enable(pt.mimeType.uppercase() in want) }
            configuredFor = key
            if (!reg) {
                registered = false
                registration = if (register) "Outgoing calls only: this login is your main line. Tap Get from my PBX to also receive calls."
                    else "Incoming calls are off"
            }
        }
        return c
    }

    /** Sign in for incoming calls (or keep the current sign-in). Main thread. */
    fun startListening(ctx: Context, sip: SipInfo): Boolean = try {
        ensure(ctx, sip, register = true)
        canRegister(sip)
    } catch (e: Exception) { registration = "The phone engine couldn't start (${e.javaClass.simpleName})"; false }

    /** Sign out of incoming calls (outgoing still works). Main thread. */
    fun stopListening() {
        val c = core ?: return
        c.accountList.forEach { acc ->
            if (acc.params.isRegisterEnabled) {
                val p = acc.params.clone(); p.isRegisterEnabled = false; acc.params = p
            }
        }
        configuredFor = ""
        registered = false
        registration = "Incoming calls are off"
    }

    /** Place a call. Returns an error message, or null when it started. */
    fun call(ctx: Context, sip: SipInfo, number: String, keepListening: Boolean = false): String? {
        if (ui != null && ui?.ended == false) return "You're already on a call."
        val dial = number.filter { it.isDigit() || it == '*' || it == '#' || it == '+' }
        if (dial.isEmpty()) return "Enter a number."
        val c = try { ensure(ctx, sip, register = keepListening) }
            catch (e: Exception) { return "The phone engine couldn't start (${e.javaClass.simpleName})." }
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

    /** Answer the ringing call (CallService calls this after going foreground). */
    fun answer(): Boolean {
        val call = core?.calls?.firstOrNull { it.state == Call.State.IncomingReceived || it.state == Call.State.IncomingEarlyMedia }
            ?: return false
        val params = core?.createCallParams(call)
        params?.mediaEncryption = MediaEncryption.SRTP
        val ok = (if (params != null) call.acceptWithParams(params) else call.accept()) == 0
        if (ok) ui = ui?.copy(status = "Connecting...")
        appCtx?.let { IncomingNotice.cancel(it) }
        return ok
    }

    fun decline() {
        val call = core?.calls?.firstOrNull { it.state == Call.State.IncomingReceived || it.state == Call.State.IncomingEarlyMedia }
        call?.decline(Reason.Declined)
        ui = ui?.copy(status = "Declined", ended = true)
        appCtx?.let { IncomingNotice.cancel(it); CallService.stop(it) }
    }

    fun hangUp() {
        val call = current()
        if (call != null) call.terminate()
        else ui = ui?.copy(status = "Call ended", ended = true)
        appCtx?.let { CallService.stop(it); IncomingNotice.cancel(it) }
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
