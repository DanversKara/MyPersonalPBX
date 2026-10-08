# Own PBX — Design (Asterisk core)

## 1. Decision log

- 2026-10-05: Engine = **Asterisk 22 LTS** (current LTS). Plain Asterisk with our own panel and call logic; no third-party GUI or module framework.
- **All call intelligence lives in our Python code via ARI** (Stasis app `pbx-brain`). Asterisk is the media/signaling engine; the brain is ours. This is the literal "APIs for extensions": extensions are rows in our DB, routing is code.
- **Kamailio stays** as the edge proxy (already working: public door, NAT traversal, fail2ban). Asterisk binds to the LAN only and is never directly exposed.
- Considered and rejected: building directly on pjproject with no Asterisk (library gives SIP+media only; voicemail, recording, spy-mixing, conferences, CDR would all be hand-built — ~10x the work for no risk reduction, since Asterisk is GPLv2 and can't be taken away).

## 2. Components

```
[Zoiper phones] --SIP--> [Kamailio edge] --SIP--> [Asterisk core] <--ARI ws--> [pbx-brain]
[VoIP.ms etc.]  --SIP trunk----------------------> [Asterisk core]
[Admin browser] --HTTPS--> [pbx-api (FastAPI)] --> [SQLite DB] --> [config-gen] --> Asterisk configs
```

| Component | Tech | Job |
|---|---|---|
| asterisk-core | Vanilla Asterisk 22 LTS | SIP (chan_pjsip), RTP, codecs (G.711u/a, G.722, Opus), ARI. Only our generated configs. |
| pbx-brain | Python + ari-py, Stasis app `pbx-brain` | Owns every call: internal routing, ring groups, voicemail record/playback, call recording (admin/user split), ChanSpy *555 via ARI snoop, conferences via ARI bridges, DISA with PIN, CDR writing, SIP MESSAGE routing via FastAGI. |
| pbx-api | FastAPI | Management REST + panel backend. Owns the DB. Triggers config-gen on changes. |
| config-gen | Python + Jinja2 | Renders Asterisk configs from DB, reloads Asterisk safely. |
| db | SQLite (single node; schema is Postgres-compatible) | Extensions, logins, trunks, routes, voicemail, recordings, CDR, messages, settings. |
| panel | Existing panel UI, new REST client | Dashboard, /me self-service, recordings/voicemail tabs, billing, security. UI templates carried over; backend rewritten against pbx-api. |

## 3. Call flows

**Internal (exten -> exten).** Phone -> Kamailio -> Asterisk `[extensions]` -> `Stasis(pbx-brain,internal,${EXTEN})` -> brain looks up callee contacts -> ARI originate to `PJSIP/<endpoint>` -> bridge. Recording started per flags (admin dir vs user dir). CDR written on StasisEnd.

**Inbound (DID -> login ring group).** Provider trunk -> Asterisk `[trunk-in]` -> `Stasis(pbx-brain,inbound,${DID})` -> `inbound_routes` lookup -> ring the login's `inbound_ring` phones (30s, MixMonitor per their flags) -> no answer -> ARI record into the login's voicemail box -> MWI notify the registered endpoints.

**Outbound (exten -> PSTN).** `Stasis(pbx-brain,internal,${EXTEN})` matches `outbound_routes` patterns -> ARI originate `PJSIP/<trunk>/<number>` -> bridge. DISA is a separate path: PIN verified first, channel never recorded.

**Voicemail retrieval.** `*97` -> Stasis -> ARI playback of the login's messages with a DTMF menu (next / delete / save). Panel `/me` voicemail tab streams the same files.

**ChanSpy *555.** `*555` -> Stasis -> menu of active channels -> ARI `POST /channels/{id}/snoop` (spy mode). Whisper/barge are one parameter away.

**Messages (ext-to-ext texting).** Out-of-dialog SIP MESSAGE -> `[pbx-msg]` -> FastAGI into pbx-brain -> quota check (`max_messages`/month) -> deliver via `MessageSend` dialplan app on a Local channel.

## 4. Data model

See `db/schema.sql`. Tables: `logins`, `extensions`, `voicemail_boxes`, `voicemail_messages`, `trunks`, `inbound_routes`, `outbound_routes`, `recordings`, `cdr`, `messages`, `kv_settings`, `signins`, `audit`.

Key rules carried over: per-login voicemail boxes (fallback mailbox in settings); admin recording flag is PBX-side only and never exposed to users; users toggle only their own `user_record`; CDR rows are soft-hidden per login with a 90-day retention lock (system CDR never touched).

## 5. pbx-api routes (v1)

- `POST /auth/login`, `/auth/logout`, `GET /auth/me` — session cookie, bcrypt, PBX-verified (same model as today).
- `/extensions` CRUD, `POST /extensions/{id}/rotate-secret` (SIP secret never leaves the server in responses).
- `/trunks` CRUD.
- `/inbound-routes`, `/outbound-routes` CRUD.
- `/logins` CRUD (admin only).
- `/voicemail/boxes`, `/voicemail/messages`, `/voicemail/audio/{id}` (stream), delete.
- `/recordings`, `/recordings/audio/{id}` (stream), delete, email.
- `/live/endpoints`, `/live/channels`.
- `/cdr`, `POST /cdr/hide` (90-day lock enforced server-side).
- `/messages`, delete.
- `POST /system/reload` (config-gen + `pjsip reload`).
- `GET /healthz`.
- Later phases: `/billing/*` (Stripe), `/security/cve` (OSV.dev checker).

## 6. Config generation

Rendered from DB: `pjsip.conf` (transport, trunk registrations/auths/endpoints, extension aors/auths/endpoints). Static files shipped: `extensions.conf` (Stasis handoff only), `ari.conf`, `http.conf` (bind 127.0.0.1), `musiconhold.conf`.

Reload strategy: endpoint/trunk changes -> `asterisk -rx "pjsip reload"` (non-disruptive to active calls). Transport/ARI changes -> full restart (rare, announced). Writes are atomic (temp file + rename).

## 7. Security model

- Asterisk binds LAN only; Kamailio remains the internet-facing edge with fail2ban.
- ARI on 127.0.0.1 only, strong random password, never exposed.
- pbx-api behind TLS (existing NPM), session auth, CSRF, rate-limited login, audit log.
- SIP secrets live in the DB only; `.env.example` pattern; no secrets in git.
- No third-party admin GUI or module framework: the only admin surface is our own panel.

## 8. Migration plan (from the previous PBX)

1. New VM/LXC `pbx-new`: `scripts/install.sh` (Asterisk 22 from source + our stack).
2. Export extensions/users/logins from the live PBX -> import script into the new DB.
3. Test: second Zoiper account against the new core — register, ext-to-ext call, voicemail, recording.
4. Trunk: register the new core to a separate VoIP.ms sub-account for inbound/outbound tests.
5. Cutover: point Kamailio upstream at the new core. Keep the old PBX powered off but intact for 2 weeks as rollback.

## 9. Phases

- **Phase 1 (now):** Asterisk + brain skeleton + pbx-api + config-gen + schema. Milestone: two SIP phones register, ext-to-ext call with audio, CDR written. Proven in sandbox with sipp/pjsua before touching real infra.
- **Phase 2:** Voicemail (record/play/MWI), recording admin/user split, *555 spy, conferences, DISA, feature codes.
- **Phase 3:** Trunks + inbound DID routing + outbound routes.
- **Phase 4:** Messages, live view, panel integration, /me self-service.
- **Phase 5:** Billing, CVE checker, alerts, hardening, ISO/installer.

## 10. Open questions (later)

- SQLite vs Postgres on day 1: SQLite (single node, zero ops).
- Bootable ISO installer: phase 5+.
- WebRTC clients: later.
