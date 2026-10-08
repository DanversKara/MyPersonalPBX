# voip.ms SMS/MMS Integration (best-route)

This implements the recommended route for your 4 DIDs:

- **Inbound:** voip.ms URL Callback -> `/hooks/voipms-sms` -> `did_sms_routes` -> `messages` table -> Zoiper (SIP MESSAGE via pbx-brain) + My Phone + ESP (`/messages/inbox`)
- **Outbound:** Zoiper / My Phone / ESP -> external PSTN number -> voip.ms `sendSMS` API using the DID mapped to your extension

PortSIP does the same thing with a built-in webhook; here we built that webhook for own-pbx.

## 1. voip.ms portal

1. Main Menu > SOAP and REST/JSON API > Enable API. Set an **API Password** (shown once, save it). This is different from your login password.
2. For each of your 4 DIDs: DID Numbers > Manage DIDs > Edit (pencil)
   - Message Service (SMS/MMS): Enable
   - SMS URL Callback: paste the webhook URL from the PBX (see below)
   - Number format: **E.164** (e.g. +15551234567)
   - Apply changes
3. Optional but recommended: enable **URL Callback Retry** - the PBX returns plain `ok` so voip.ms won't retry forever.

## 2. PBX setup

1. Deploy this code, restart `pbx-api`, `pbx-brain`, `pbx-agi`.
   - DB migrates automatically (`did_sms_routes`, `voipms_sms_dedupe`, kv_settings).
2. Open admin panel -> **SMS routes** (or `/sms-routes`).
   - Set API username (your voip.ms login email), API password, webhook token (auto-generated if blank), default DID.
   - Add 4 routes: DID (digits, e.g. `15551234567`) -> destination extension (e.g. `8801`), label e.g. "Main".
   - The page shows the exact webhook URL to paste into voip.ms:
     ```
     https://<your-pbx>/hooks/voipms-sms?token=<token>&to={TO}&from={FROM}&message={MESSAGE}&id={ID}&date={TIMESTAMP}
     ```
3. Make the webhook reachable from the internet:
   - Via Cloudflare Tunnel / Nginx Proxy Manager to `http://<pbx-ip>:8001`
   - `/hooks/voipms-sms` is in `REMOTE_ALLOWED_PREFIXES`, so it works through the tunnel even when admin pages are LAN-only.
   - Test: `curl "https://<your-pbx>/hooks/voipms-sms?token=<token>"` should return `ok`.

## 3. How routing works

### Inbound

voip.ms POSTs (or GETs) `to, from, message, id, date` to the webhook.

- `to` is your DID, `from` is the external mobile, `message` is the body.
- PBX normalizes DID to digits (`15551234567`), looks up `did_sms_routes.did` -> `dest_exten`.
- Inserts into `messages (from_ext=+1555..., to_ext=8801, body)`.
- Calls pbx-brain `/messages/send` which does `PJSIP/<sip_username>/sendMessage` - Zoiper pops the message.
- My Phone shows it under Messages grouped by `+1555...`. ESP polls `GET /messages/inbox?ext=8801&since=...` on pbx-brain:8099 with `X-Msg-Token`.

Dedupe: `voipms_sms_dedupe` prevents double-store on voip.ms retries.

MMS: if voip.ms includes `media_url`, it's appended as `[MMS: <url>]`. For full MMS media download, extend `_handle_voipms_inbound` to fetch via `getMMS`.

### Outbound

- **Zoiper:** type `+15551234567` or `15551234567` as the destination in a SIP MESSAGE. `[pbx-msg]` -> AGI `msg-route` detects external via `is_external_number()` and calls voip.ms `sendSMS` with the DID mapped to your exten.
- **My Phone:** `/ucp/messages` -> Send now accepts external numbers; same voip.ms path.
- **ESP / HA:** `POST http://127.0.0.1:8099/messages/send` with `{"to": "+1555...", "from": "8801", "body": "...", "store": true}` -> `send_message_api` detects external and uses voip.ms.

Sender DID selection: DID whose `dest_exten` == your exten, else `voipms_default_did`, else first route.

## 4. Testing

1. Send SMS from your cell to DID 1 -> should appear in Zoiper for ext 8801 within seconds, plus My Phone.
2. Reply from Zoiper to `+1xxx` -> cell should receive it from your DID.
3. Check logs:
   - `journalctl -u pbx-api -f` for webhook hits
   - `journalctl -u pbx-brain -f` for `msg external:` lines
   - `journalctl -u pbx-agi -f` for `msg-route external:`
4. If inbound doesn't arrive: verify webhook URL in voip.ms, token, and that `did_sms_routes` has the normalized DID.

## 5. Files changed

- `pbx-api/voipms_sms.py` + `pbx-brain/voipms_sms.py` (shared helper)
- `db/schema.sql` (new tables)
- `pbx-api/app.py`: `/hooks/voipms-sms` GET+POST, `/sms-routes` admin UI, migration, `REMOTE_ALLOWED_PREFIXES`
- `pbx-api/ucp.py`: `msg_send` allows external numbers
- `pbx-brain/agi_server.py`: `handle_msg_route` routes external via voip.ms
- `pbx-brain/app.py`: `send_message_api` routes external via voip.ms (ESP/HA)

## 6. Security notes

- Webhook token in URL query (`?token=...`) prevents random POSTs from storing messages.
- voip.ms API password stored in `kv_settings` (not in code).
- Outbound still respects per-extension `msg_out`, monthly quotas, and `max_messages`.
