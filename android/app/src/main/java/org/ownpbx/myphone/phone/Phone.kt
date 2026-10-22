// SPDX-License-Identifier: GPL-2.0-or-later
package org.ownpbx.myphone.phone

import android.content.Context
import android.media.AudioManager
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import java.io.File
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
import org.ownpbx.myphone.data.Store

/** What the call screen shows. null = no call. */
data class CallUi(
    val number: String,
    val status:
        String, // "Calling...", "Ringing...", "Incoming call", "Connected", "Call ended", ...
    val connectedAt: Long = 0, // SystemClock.elapsedRealtime() when answered, 0 = not yet
    val muted: Boolean = false,
    val speaker: Boolean = false,
    val ended: Boolean = false,
    val problem: String = "", // why it failed, in words, plus the SIP code
    val incoming: Boolean = false,
    val name: String = "", // caller's name, if the PBX sent one
    /**
     * Push-to-talk call (placed with *88, or an incoming PTT page the user accepted): the mic
     * starts muted and the screen shows hold-to-talk. Incoming pages never auto-answer — they chirp
     * and wait for an explicit Accept.
     */
    val ptt: Boolean = false,
    /** PTT extra-loud boost is on for this call (max stream volume + playback gain). */
    val boost: Boolean = false,
    /** PTT VOX (voice-activated, hands-free) is on for this call. */
    val vox: Boolean = false,
)

/**
 * The built-in phone (Linphone).
 *
 * Calls go through the edge exactly like a remote Zoiper (TLS 5061, SRTP). To receive calls the app
 * signs in (registers) on the extension's own app line, `<sip_username>-app` (see config-gen
 * pjsip.conf.j2), never on the main line, so the desk phone / Zoiper stay signed in. pbx-brain
 * rings the app line together with the main one. A login that isn't an app line is used for
 * outgoing calls only.
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
    /** True while the user holds the PTT talk button (mic live). */
    private var pttTalking = false

    // ---- PTT extra-loud boost: what to put back when it ends.
    private var boostApplied = false
    private var savedStreamVolume: Int? = null
    private var savedPlaybackGainDb: Float? = null

    // ---- PTT VOX: main-thread metering loop over the call's record level.
    private val voxHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var voxRunnable: Runnable? = null
    private var voxOpen = false
    private var voxLastAboveAt = 0L

    /** Extra playback gain while the boost is on, on top of max stream volume. */
    private const val BOOST_GAIN_DB = 8f
    /** VOX opens the mic above this local level (Call.getRecordVolume, dBm0). */
    private const val VOX_THRESHOLD_DBM0 = -30f
    /** VOX keeps the mic open this long after the level drops (word gaps). */
    private const val VOX_HANG_MS = 800L

    private val listener =
        object : CoreListenerStub() {
            override fun onCallStateChanged(
                core: Core,
                call: Call,
                state: Call.State?,
                message: String,
            ) {
                if (state == Call.State.IncomingReceived) {
                    incoming(call)
                    return
                }
                val cur = ui ?: return
                ui =
                    when (state) {
                        Call.State.OutgoingInit,
                        Call.State.OutgoingProgress -> cur.copy(status = "Calling...")
                        Call.State.OutgoingRinging,
                        Call.State.OutgoingEarlyMedia -> cur.copy(status = "Ringing...")
                        Call.State.Connected,
                        Call.State.StreamsRunning ->
                            cur.copy(
                                status = "Connected",
                                connectedAt =
                                    if (cur.connectedAt == 0L)
                                        android.os.SystemClock.elapsedRealtime()
                                    else cur.connectedAt,
                            )
                        Call.State.Paused,
                        Call.State.PausedByRemote -> cur.copy(status = "On hold")
                        Call.State.Error ->
                            cur.copy(
                                status = "Call failed",
                                ended = true,
                                problem = describe(call, message),
                            )
                        Call.State.End,
                        Call.State.Released ->
                            if (cur.ended) cur
                            else
                                when {
                                    cur.connectedAt > 0 ->
                                        cur.copy(status = "Call ended", ended = true)
                                    // Another phone on the extension picked up (the PBX says so in
                                    // the
                                    // CANCEL).
                                    cur.incoming && elsewhere(call) ->
                                        cur.copy(status = "Answered on another phone", ended = true)
                                    // Caller hung up, or it rang out to voicemail: a missed call,
                                    // no error.
                                    cur.incoming -> {
                                        val at =
                                            (call.callLog?.startDate ?: 0L).let {
                                                if (it > 0) it * 1000
                                                else System.currentTimeMillis()
                                            }
                                        appCtx?.let {
                                            org.ownpbx.myphone.Notifier.missedNow(
                                                it,
                                                cur.number,
                                                cur.name,
                                                at,
                                            )
                                        }
                                        cur.copy(status = "Missed call", ended = true)
                                    }
                                    else ->
                                        cur.copy(
                                            status = endedBeforeAnswer(call),
                                            ended = true,
                                            problem = describe(call, message),
                                        )
                                }
                        else -> cur
                    }
                if (state == Call.State.Connected || state == Call.State.StreamsRunning) {
                    stopChirp()
                    appCtx?.let { IncomingNotice.cancel(it) }
                }
                if (state == Call.State.Connected || state == Call.State.StreamsRunning) {
                    val u = ui
                    if (u != null && u.ptt) {
                        // A PTT call starts muted; only holding the talk button
                        // unmutes (setPttTalking). Re-assert that here, because
                        // the mic can only be muted once the call exists.
                        if (!pttTalking) {
                            try {
                                call.microphoneMuted = true
                            } catch (_: Exception) {}
                            ui = u.copy(muted = true)
                        }
                        // PTT is a speakerphone feature (nobody holds the
                        // phone to their ear on a work site): speaker on by
                        // default for the caller too, not just the side that
                        // auto-answered. ui.speaker carries the choice, so a
                        // manual toggle-off mid-call is respected here.
                        if (u.speaker) applySpeaker(call, true)
                        if (u.boost) applyBoost()
                        if (u.vox) startVox()
                    }
                }
                if (state == Call.State.End || state == Call.State.Released) {
                    pttTalking = false
                    stopChirp()
                    stopVox()
                    restoreBoost()
                }
                if (ui?.ended == true) {
                    stopChirp()
                    appCtx?.let {
                        CallService.stop(it)
                        IncomingNotice.cancel(it)
                    }
                }
            }

            override fun onAccountRegistrationStateChanged(
                core: Core,
                account: Account,
                state: RegistrationState?,
                message: String,
            ) {
                registered = state == RegistrationState.Ok
                registration =
                    when (state) {
                        RegistrationState.Ok -> "Signed in: this phone rings for incoming calls"
                        RegistrationState.Progress,
                        RegistrationState.Refreshing -> "Signing in..."
                        RegistrationState.Failed ->
                            "Couldn't sign in" +
                                (account.errorInfo?.let { ei ->
                                    val code = ei.protocolCode
                                    when (code) {
                                        401,
                                        403,
                                        407 ->
                                            ": the PBX refused the phone login. Tap Get from my PBX."
                                        0 ->
                                            ": can't reach ${account.params.serverAddress?.domain ?: "the PBX"}."
                                        else -> " (SIP $code ${ei.phrase ?: ""})."
                                    }
                                } ?: ".")
                        RegistrationState.Cleared,
                        RegistrationState.None,
                        null -> "Not signed in"
                        else -> state.name
                    }
            }
        }

    private fun incoming(call: Call) {
        val ctx = appCtx
        // Already on a call: tell the PBX we're busy (the other phones still ring).
        if (ui != null && ui?.ended == false) {
            call.decline(Reason.Busy)
            return
        }
        val from = call.remoteAddress
        val number = from.username.orEmpty()
        val rawName = from.displayName.orEmpty().takeIf { it != number }.orEmpty()
        // PTT marker: the PBX sets X-PTT: 1 on the INVITE and prefixes the
        // caller name with "PTT ". The headers live on the call's remote
        // params (Linphone SDK 5.4: Call.getRemoteParams() ->
        // CallParams.getCustomHeader(name)); the name prefix (and the old
        // Call-Info answer-after marker from pre-accept-flow servers) is
        // the fallback when no header can be read.
        val xPtt =
            try {
                call.remoteParams?.getCustomHeader("X-PTT")?.trim()
            } catch (_: Exception) {
                null
            }
        val callInfo =
            try {
                call.remoteParams?.getCustomHeader("Call-Info")
            } catch (_: Exception) {
                null
            }
        val marked =
            xPtt == "1" ||
                xPtt.equals("true", ignoreCase = true) ||
                callInfo?.contains("answer-after=0", ignoreCase = true) == true ||
                rawName.startsWith("PTT ")
        // My own effective PTT state, cached from /me/settings by the
        // Settings screen and LiveService (no network I/O on this thread).
        val myPtt =
            ctx != null &&
                try {
                    Store(ctx).pttOn
                } catch (_: Exception) {
                    false
                }
        val isPtt = marked && myPtt
        if (!isPtt) {
            ui = CallUi(number = number, name = rawName, status = "Incoming call", incoming = true)
            if (ctx != null) IncomingNotice.show(ctx, number, ui?.name.orEmpty())
            return
        }
        // Incoming PTT with my PTT on: NEVER answer on its own — a page
        // must not open the line (mic/speaker) until I accept it. Chirp
        // over the ringtone and show the Accept / No-accept screen; if I
        // don't accept, the PBX applies my missed-PTT setting (voicemail
        // or chirp again). The IncomingNotice full-screen intent brings
        // the accept screen up, and its actions work from the lock screen.
        val prefs = if (ctx != null) Store(ctx) else null
        ui =
            CallUi(
                number = number,
                name = rawName.removePrefix("PTT "),
                status = "PTT page — Accept to connect",
                incoming = true,
                ptt = true,
                muted = true,
                speaker = true,
                boost = prefs?.pttExtraLoud == true,
                vox = prefs?.pttVox == true,
            )
        if (ctx != null) {
            IncomingNotice.show(ctx, number, ui?.name.orEmpty(), ptt = true)
            startChirp(ctx)
        }
    }

    // ---- PTT chirp ------------------------------------------------------
    // An original alert tone, synthesized at runtime from sine waves: three
    // short rising tones (1320 -> 1760 -> 2200 Hz) and a pause, looped. It
    // is NOT the Nextel chirp and not any sampled or trademarked sound, and
    // the app ships no audio file for it. It plays on the alarm audio
    // stream so a PTT page cuts over the ringtone and notification sounds
    // instead of mixing with them, with a matching vibration pattern.
    private var chirpTrack: android.media.AudioTrack? = null
    private var chirpVibrator: android.os.Vibrator? = null

    private fun chirpPcm(): ShortArray {
        val sr = 22050
        val tones = intArrayOf(1320, 1760, 2200)
        val burst = (sr * 0.07).toInt() // 70 ms per tone
        val gap = (sr * 0.04).toInt() // 40 ms between tones
        val pause = (sr * 1.5).toInt() // 1.5 s of quiet, then repeat
        val out = ShortArray(tones.size * (burst + gap) + pause)
        var pos = 0
        for (f in tones) {
            for (i in 0 until burst) {
                // Raised-cosine envelope, so the tones chirp instead of clicking.
                val env = 0.5 - 0.5 * kotlin.math.cos(2.0 * Math.PI * i / burst)
                val s = kotlin.math.sin(2.0 * Math.PI * f * i / sr)
                val h = 0.25 * kotlin.math.sin(4.0 * Math.PI * f * i / sr)
                out[pos + i] = ((s + h) * env * 0.55 * Short.MAX_VALUE).toInt().toShort()
            }
            pos += burst + gap
        }
        return out
    }

    private fun startChirp(ctx: Context) {
        stopChirp()
        try {
            val pcm = chirpPcm()
            val track =
                android.media.AudioTrack.Builder()
                    .setAudioAttributes(
                        android.media.AudioAttributes.Builder()
                            .setUsage(android.media.AudioAttributes.USAGE_ALARM)
                            .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SONIFICATION)
                            .build()
                    )
                    .setAudioFormat(
                        android.media.AudioFormat.Builder()
                            .setEncoding(android.media.AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(22050)
                            .setChannelMask(android.media.AudioFormat.CHANNEL_OUT_MONO)
                            .build()
                    )
                    .setBufferSizeInBytes(pcm.size * 2)
                    .setTransferMode(android.media.AudioTrack.MODE_STATIC)
                    .build()
            track.write(pcm, 0, pcm.size)
            track.setLoopPoints(0, pcm.size, -1)
            track.play()
            chirpTrack = track
        } catch (_: Exception) {}
        try {
            val v = ctx.getSystemService(android.os.Vibrator::class.java)
            if (v != null && v.hasVibrator()) {
                v.vibrate(
                    android.os.VibrationEffect.createWaveform(
                        longArrayOf(0, 160, 120, 160, 1400),
                        0,
                    )
                )
                chirpVibrator = v
            }
        } catch (_: Exception) {}
    }

    private fun stopChirp() {
        try {
            chirpTrack?.stop()
        } catch (_: Exception) {}
        try {
            chirpTrack?.release()
        } catch (_: Exception) {}
        chirpTrack = null
        try {
            chirpVibrator?.cancel()
        } catch (_: Exception) {}
        chirpVibrator = null
    }

    private fun elsewhere(call: Call): Boolean =
        try {
            val s = call.callLog?.status
            s == Call.Status.AcceptedElsewhere || s == Call.Status.DeclinedElsewhere
        } catch (_: Exception) {
            false
        }

    private fun describe(call: Call, message: String): String {
        val ei = call.errorInfo
        val code = ei?.protocolCode ?: 0
        val words =
            when (code) {
                0 ->
                    if (message.contains("TLS", true) || message.contains("certificate", true))
                        "Couldn't make a secure connection to the PBX."
                    else ""
                401,
                403,
                407 -> "The PBX refused the phone login."
                404,
                484 -> "That number doesn't exist."
                480 -> "Nobody is available on that number."
                486,
                600 -> "Busy."
                488,
                606 -> "The call settings didn't match the PBX (audio/encryption)."
                503 -> "The PBX couldn't place the call right now."
                else -> ""
            }
        val raw =
            listOfNotNull(
                    ei?.phrase?.takeIf { it.isNotBlank() },
                    message.takeIf { it.isNotBlank() },
                    call.reason?.name?.takeIf { it != "None" }?.let { "reason $it" },
                )
                .distinct()
                .joinToString(" - ")
        return listOf(words, if (code > 0) "SIP $code" else "", raw)
            .filter { it.isNotBlank() }
            .joinToString(" · ")
    }

    private fun endedBeforeAnswer(call: Call) =
        when (call.errorInfo?.protocolCode ?: 0) {
            486,
            600 -> "Busy"
            480,
            408 -> "No answer"
            0 -> "Call ended"
            else -> "Call failed"
        }

    /**
     * Android's trusted CAs as one PEM file, so TLS to the edge is verified against the same roots
     * the phone trusts (the edge has a real cert).
     */
    private fun systemCaBundle(ctx: Context): String? =
        try {
            val out = File(ctx.filesDir, "rootca.pem")
            if (!out.exists() || out.length() < 1000) {
                val sb = StringBuilder()
                val dirs =
                    listOf(
                        File("/apex/com.android.conscrypt/cacerts"),
                        File("/system/etc/security/cacerts"),
                    )
                val dir = dirs.firstOrNull { it.isDirectory && (it.list()?.isNotEmpty() == true) }
                dir?.listFiles()?.forEach { f ->
                    val t = f.readText()
                    val s = t.indexOf("-----BEGIN CERTIFICATE-----")
                    val e = t.indexOf("-----END CERTIFICATE-----")
                    if (s >= 0 && e > s)
                        sb.append(t, s, e + "-----END CERTIFICATE-----".length).append('\n')
                }
                if (sb.isNotEmpty()) out.writeText(sb.toString())
            }
            out.takeIf { it.exists() && it.length() > 1000 }?.path
        } catch (e: Exception) {
            null
        }

    /**
     * Only the app's own line may register; registering the main line would replace the desk phone
     * / Zoiper (max_contacts=1 on the PBX).
     */
    fun canRegister(sip: SipInfo) = sip.username.endsWith("-app")

    /** Create / reconfigure the engine for this login. Main thread. */
    private fun ensure(ctx: Context, sip: SipInfo, register: Boolean): Core {
        appCtx = ctx.applicationContext
        val reg = register && canRegister(sip)
        val key = "${sip.username}@${sip.domain}:${sip.port}#${sip.password.hashCode()}#$reg"
        val f = Factory.instance()
        val c =
            core
                ?: f.createCore(null, null, ctx.applicationContext).also { c ->
                    c.addListener(listener)
                    c.setUserAgent("MyPhone", org.ownpbx.myphone.BuildConfig.VERSION_NAME)
                    systemCaBundle(ctx)?.let { c.rootCa = it }
                    // Local listening transports: TLS only (random port).
                    val t = c.transports
                    t.udpPort = 0
                    t.tcpPort = 0
                    t.tlsPort = -1
                    c.transports = t
                    c.mediaEncryption = MediaEncryption.SRTP
                    c.isMediaEncryptionMandatory = true
                    c.isKeepAliveEnabled = true // keep the TLS connection to the edge open
                    c.isNativeRingingEnabled = true // ring with the phone's own ringtone
                    c.start()
                    core = c
                }
        if (configuredFor != key) {
            c.clearAccounts()
            c.clearAllAuthInfo()
            c.addAuthInfo(
                f.createAuthInfo(sip.username, null, sip.password, null, null, sip.domain, null)
            )
            val p = c.createAccountParams()
            p.identityAddress = f.createAddress("sip:${sip.username}@${sip.domain}")
            p.serverAddress =
                f.createAddress("sip:${sip.domain}:${sip.port}")?.also {
                    it.transport = TransportType.Tls
                }
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
                registration =
                    if (register)
                        "Outgoing calls only: this login is your main line. Tap Get from my PBX to also receive calls."
                    else "Incoming calls are off"
            }
        }
        return c
    }

    /** Sign in for incoming calls (or keep the current sign-in). Main thread. */
    fun startListening(ctx: Context, sip: SipInfo): Boolean =
        try {
            ensure(ctx, sip, register = true)
            // Already configured is not the same as still registered: if the
            // TLS connection dropped while Android froze the process (Doze /
            // Motorola battery killing), the account sits unregistered and
            // ensure() alone does nothing — nothing re-registers until the
            // app is reopened, and callers get a dead / busy line meanwhile.
            // Force a fresh REGISTER whenever we know we're not signed in.
            if (!registered)
                try {
                    core?.refreshRegisters()
                } catch (_: Exception) {}
            canRegister(sip)
        } catch (e: Exception) {
            registration = "The phone engine couldn't start (${e.javaClass.simpleName})"
            false
        }

    /** Sign out of incoming calls (outgoing still works). Main thread. */
    fun stopListening() {
        val c = core ?: return
        c.accountList.forEach { acc ->
            if (acc.params.isRegisterEnabled) {
                val p = acc.params.clone()
                p.isRegisterEnabled = false
                acc.params = p
            }
        }
        configuredFor = ""
        registered = false
        registration = "Incoming calls are off"
    }

    /** Place a call. Returns an error message, or null when it started. */
    fun call(
        ctx: Context,
        sip: SipInfo,
        number: String,
        keepListening: Boolean = false,
        ptt: Boolean = false,
    ): String? {
        if (ui != null && ui?.ended == false) return "You're already on a call."
        val dial = number.filter { it.isDigit() || it == '*' || it == '#' || it == '+' }
        if (dial.isEmpty()) return "Enter a number."
        val c =
            try {
                ensure(ctx, sip, register = keepListening)
            } catch (e: Exception) {
                return "The phone engine couldn't start (${e.javaClass.simpleName})."
            }
        val to =
            Factory.instance().createAddress("sip:$dial@${sip.domain}")
                ?: return "That number can't be dialled."
        to.transport = TransportType.Tls
        val params = c.createCallParams(null) ?: return "The phone engine couldn't start."
        params.mediaEncryption = MediaEncryption.SRTP
        // For a PTT call the screens show the extension, not the *88 code.
        val shown = if (ptt) dial.removePrefix("*88") else dial
        pttTalking = false
        // A PTT call starts on speakerphone (it's a hands-free feature) with
        // the user's saved boost / VOX preferences; a normal call is untouched.
        val prefs = if (ptt) Store(ctx) else null
        ui =
            CallUi(
                number = shown,
                status = "Calling...",
                ptt = ptt,
                muted = ptt,
                speaker = ptt,
                boost = prefs?.pttExtraLoud == true,
                vox = prefs?.pttVox == true,
            )
        CallService.start(ctx, shown)
        val placed = c.inviteAddressWithParams(to, params)
        if (placed == null) {
            ui =
                CallUi(
                    number = shown,
                    status = "Call failed",
                    ended = true,
                    ptt = ptt,
                    problem = "The call couldn't be started.",
                )
            CallService.stop(ctx)
        } else if (ptt) {
            // Start muted right away; Connected re-asserts it, and only
            // holding the talk button unmutes (setPttTalking).
            try {
                placed.microphoneMuted = true
            } catch (_: Exception) {}
        }
        return null
    }

    /**
     * Place a push-to-talk call to [exten]: dials *88 + the extension on the same account / path as
     * a normal call. The PBX answers the other side on speakerphone; here the mic starts muted
     * (hold to talk).
     */
    fun callPtt(
        ctx: Context,
        sip: SipInfo,
        exten: String,
        keepListening: Boolean = false,
    ): String? {
        val ext = exten.filter { it.isDigit() }
        if (ext.isEmpty()) return "Enter an extension."
        return call(ctx, sip, "*88$ext", keepListening, ptt = true)
    }

    /**
     * Hold-to-talk for a PTT call: while held the mic is live, on release it goes back to muted.
     */
    fun setPttTalking(talking: Boolean) {
        val call = current() ?: return
        if (ui?.ptt != true) return
        pttTalking = talking
        try {
            call.microphoneMuted = !talking
        } catch (_: Exception) {}
        ui = ui?.copy(muted = !talking)
    }

    /**
     * Extra-loud boost for a PTT call: voice-call stream to max plus extra playback gain in the
     * engine (Core.playbackGainDb, verified in SDK 5.4.128). The choice is remembered as the
     * default for the next PTT call. Everything is put back when the boost goes off or the call
     * ends, so a following normal call is never blasted.
     */
    fun setPttBoost(on: Boolean) {
        if (ui?.ptt != true) return
        ui = ui?.copy(boost = on)
        appCtx?.let {
            try {
                Store(it).pttExtraLoud = on
            } catch (_: Exception) {}
        }
        if (on) applyBoost() else restoreBoost()
    }

    private fun applyBoost() {
        if (boostApplied) return
        val ctx = appCtx ?: return
        try {
            val am = ctx.getSystemService(AudioManager::class.java) ?: return
            if (savedStreamVolume == null)
                savedStreamVolume = am.getStreamVolume(AudioManager.STREAM_VOICE_CALL)
            am.setStreamVolume(
                AudioManager.STREAM_VOICE_CALL,
                am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL),
                0,
            )
            core?.let { c ->
                if (savedPlaybackGainDb == null) savedPlaybackGainDb = c.playbackGainDb
                c.playbackGainDb = (savedPlaybackGainDb ?: 0f) + BOOST_GAIN_DB
            }
            boostApplied = true
        } catch (_: Exception) {}
    }

    private fun restoreBoost() {
        if (!boostApplied) return
        boostApplied = false
        try {
            savedPlaybackGainDb?.let { g -> core?.playbackGainDb = g }
            val ctx = appCtx
            val v = savedStreamVolume
            if (ctx != null && v != null) {
                ctx.getSystemService(AudioManager::class.java)
                    ?.setStreamVolume(AudioManager.STREAM_VOICE_CALL, v, 0)
            }
        } catch (_: Exception) {}
        savedPlaybackGainDb = null
        savedStreamVolume = null
    }

    /**
     * VOX for a PTT call: the mic opens when the user actually speaks and closes again after a
     * short silence, so nobody has to hold the button. The choice is remembered as the default for
     * the next PTT call. Turning it off re-mutes at once.
     */
    fun setPttVox(on: Boolean) {
        if (ui?.ptt != true) return
        ui = ui?.copy(vox = on)
        appCtx?.let {
            try {
                Store(it).pttVox = on
            } catch (_: Exception) {}
        }
        if (on) {
            startVox()
        } else {
            stopVox()
            if (voxOpen) {
                voxOpen = false
                setPttTalking(false)
            }
        }
    }

    private fun startVox() {
        stopVox()
        val r =
            object : Runnable {
                override fun run() {
                    if (!voxTick()) {
                        voxRunnable = null
                        return
                    }
                    voxHandler.postDelayed(this, 100)
                }
            }
        voxRunnable = r
        voxHandler.post(r)
    }

    private fun stopVox() {
        voxRunnable?.let { voxHandler.removeCallbacks(it) }
        voxRunnable = null
    }

    /**
     * One VOX sample: the call's measured record level (what the mic is sending, in dBm0 —
     * Call.getRecordVolume, SDK 5.4.128) against the threshold, with a hang time so word gaps don't
     * chop the mic. Returns false when VOX no longer applies (call over / VOX off).
     */
    private fun voxTick(): Boolean {
        val u = ui ?: return false
        if (!u.ptt || !u.vox || u.ended) return false
        val call = current() ?: return true // not connected yet: keep waiting
        val level =
            try {
                call.recordVolume
            } catch (_: Exception) {
                return true
            }
        val now = android.os.SystemClock.elapsedRealtime()
        if (level > VOX_THRESHOLD_DBM0) {
            voxLastAboveAt = now
            if (!voxOpen) {
                voxOpen = true
                setPttTalking(true)
            }
        } else if (voxOpen && now - voxLastAboveAt > VOX_HANG_MS) {
            voxOpen = false
            setPttTalking(false)
        }
        return true
    }

    private fun current(): Call? = core?.currentCall ?: core?.calls?.firstOrNull()

    /** Answer the ringing call (CallService calls this after going foreground). */
    fun answer(): Boolean {
        val call =
            core?.calls?.firstOrNull {
                it.state == Call.State.IncomingReceived || it.state == Call.State.IncomingEarlyMedia
            } ?: return false
        val params = core?.createCallParams(call)
        params?.mediaEncryption = MediaEncryption.SRTP
        val ok = (if (params != null) call.acceptWithParams(params) else call.accept()) == 0
        if (ok) {
            // An auto-answered PTT call: speakerphone on, mic muted until
            // the user holds the talk button.
            val ptt = ui?.ptt == true
            ui =
                ui?.copy(
                    status = if (ptt) "PTT call" else "Connecting...",
                    muted = ptt,
                    speaker = ptt,
                )
            if (ptt) {
                applySpeaker(call, true)
                try {
                    call.microphoneMuted = true
                } catch (_: Exception) {}
            }
        }
        appCtx?.let { IncomingNotice.cancel(it) }
        return ok
    }

    fun decline() {
        val call =
            core?.calls?.firstOrNull {
                it.state == Call.State.IncomingReceived || it.state == Call.State.IncomingEarlyMedia
            }
        call?.decline(Reason.Declined)
        ui = ui?.copy(status = "Declined", ended = true)
        appCtx?.let {
            IncomingNotice.cancel(it)
            CallService.stop(it)
        }
    }

    fun hangUp() {
        pttTalking = false
        stopVox()
        restoreBoost()
        val call = current()
        if (call != null) call.terminate() else ui = ui?.copy(status = "Call ended", ended = true)
        appCtx?.let {
            CallService.stop(it)
            IncomingNotice.cancel(it)
        }
    }

    fun toggleMute() {
        val call = current() ?: return
        call.microphoneMuted = !call.microphoneMuted
        ui = ui?.copy(muted = call.microphoneMuted)
    }

    /** Route this call's audio to the speaker (or back to the earpiece). */
    private fun applySpeaker(call: Call, on: Boolean) {
        val c = core ?: return
        val type = if (on) AudioDevice.Type.Speaker else AudioDevice.Type.Earpiece
        c.audioDevices.firstOrNull { it.type == type }?.let { call.outputAudioDevice = it }
    }

    fun toggleSpeaker() {
        val call = current() ?: return
        val wantSpeaker = !(ui?.speaker ?: false)
        applySpeaker(call, wantSpeaker)
        ui = ui?.copy(speaker = wantSpeaker)
    }

    fun dtmf(ch: Char) {
        current()?.sendDtmf(ch)
    }

    /** Close the "Call ended" screen. */
    fun dismiss() {
        if (ui?.ended == true) ui = null
    }
}
