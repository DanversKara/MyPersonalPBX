-- SPDX-License-Identifier: GPL-2.0-or-later
-- Own PBX schema. SQLite on day 1; types kept Postgres-compatible.
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS logins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,           -- API login; also the "extension" identity
  pwhash TEXT NOT NULL,                    -- bcrypt
  role TEXT NOT NULL DEFAULT 'user',       -- admin | user
  exten TEXT NOT NULL DEFAULT '',          -- extension number, e.g. 8800 (empty for pure admins)
  sip_username TEXT NOT NULL DEFAULT '',   -- SIP auth username (e.g. phone8800)
  sip_secret TEXT NOT NULL DEFAULT '',     -- SIP password; server-side only
  display_name TEXT NOT NULL DEFAULT '',
  record_admin INTEGER NOT NULL DEFAULT 0, -- admin-only recording flag
  user_record INTEGER NOT NULL DEFAULT 0,  -- admin allow-flag for user recording
  user_wants_record INTEGER NOT NULL DEFAULT 0, -- user's own "record my calls" wish
  vm_email TEXT NOT NULL DEFAULT '',
  max_messages INTEGER NOT NULL DEFAULT 500,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  exten_since TEXT DEFAULT NULL            -- when this login got its current extension (UTC)
);
-- NOTE: logins ARE the extensions (1:1). No separate extensions table.

CREATE TABLE IF NOT EXISTS voicemail_boxes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login_id INTEGER NOT NULL UNIQUE REFERENCES logins(id) ON DELETE CASCADE,
  mailbox TEXT NOT NULL UNIQUE,            -- sanitized per-login box
  greeting_path TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS voicemail_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mailbox TEXT NOT NULL,
  caller TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL DEFAULT (datetime('now')),
  duration_sec INTEGER NOT NULL DEFAULT 0,
  folder TEXT NOT NULL DEFAULT 'INBOX',    -- INBOX | Old
  is_read INTEGER NOT NULL DEFAULT 0,
  path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vm_mailbox ON voicemail_messages(mailbox, folder);

CREATE TABLE IF NOT EXISTS trunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  registrar TEXT NOT NULL,                 -- sip:host[:port]
  username TEXT NOT NULL,
  secret TEXT NOT NULL,
  from_user TEXT NOT NULL DEFAULT '',
  codecs TEXT NOT NULL DEFAULT 'ulaw,alaw,g722',
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS inbound_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  did TEXT NOT NULL UNIQUE,                -- digits as received from provider
  ring_extens TEXT NOT NULL DEFAULT '[]',  -- JSON array of extensions to ring, e.g. ["8800","8801"]
  timeout_sec INTEGER NOT NULL DEFAULT 30, -- ring timeout before voicemail
  enabled INTEGER NOT NULL DEFAULT 1,
  ivr_id INTEGER DEFAULT NULL              -- send callers to this IVR instead
);
-- No-answer goes to the first ring extension's login voicemail box.
-- Each login has its own box (voicemail_boxes); no shared fallback.

CREATE TABLE IF NOT EXISTS outbound_routes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  patterns TEXT NOT NULL DEFAULT '[]',     -- JSON list of dial patterns
  trunk_id INTEGER REFERENCES trunks(id) ON DELETE SET NULL,
  priority INTEGER NOT NULL DEFAULT 10,
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recordings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL,
  system TEXT NOT NULL,                    -- admin | user
  exten TEXT NOT NULL DEFAULT '',
  login_id INTEGER REFERENCES logins(id) ON DELETE SET NULL,
  direction TEXT NOT NULL DEFAULT '',      -- in | out | internal
  peer TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  duration_sec INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0,
  path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rec_login ON recordings(login_id, started_at);

CREATE TABLE IF NOT EXISTS cdr (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL UNIQUE,
  started_at TEXT NOT NULL,
  src TEXT NOT NULL DEFAULT '',
  dst TEXT NOT NULL DEFAULT '',
  direction TEXT NOT NULL DEFAULT '',
  login_id INTEGER REFERENCES logins(id) ON DELETE SET NULL,
  duration_sec INTEGER NOT NULL DEFAULT 0,
  bill_sec INTEGER NOT NULL DEFAULT 0,
  disposition TEXT NOT NULL DEFAULT '',
  recording_id INTEGER REFERENCES recordings(id) ON DELETE SET NULL,
  hidden_by TEXT NOT NULL DEFAULT '[]',    -- legacy (users can no longer hide calls)
  answered_login_id INTEGER DEFAULT NULL   -- who picked up an inbound outside call (minutes)
);
CREATE INDEX IF NOT EXISTS idx_cdr_login ON cdr(login_id, started_at);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_ext TEXT NOT NULL,
  to_ext TEXT NOT NULL,
  body TEXT NOT NULL,
  sent_at TEXT NOT NULL DEFAULT (datetime('now')),
  login_id INTEGER REFERENCES logins(id) ON DELETE SET NULL,
  via_did TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS feature_codes (
  code TEXT PRIMARY KEY,              -- e.g. '*97', '*555' (prefix match)
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  default_access TEXT NOT NULL DEFAULT 'all'  -- 'all' | 'admin' | 'none'
);
CREATE TABLE IF NOT EXISTS login_feature_access (
  login_id INTEGER NOT NULL REFERENCES logins(id) ON DELETE CASCADE,
  code TEXT NOT NULL REFERENCES feature_codes(code) ON DELETE CASCADE,
  allowed INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (login_id, code)
);

-- Messages a user deleted from their own view (the other party keeps them).
CREATE TABLE IF NOT EXISTS message_hidden (
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  login_id INTEGER NOT NULL REFERENCES logins(id) ON DELETE CASCADE,
  PRIMARY KEY (message_id, login_id)
);

-- voip.ms SMS/MMS routing: which DID goes to which extension.
-- did is stored normalized (e.g. 15551234567 for +1 555-123-4567).
-- dest_exten is the internal extension (e.g. 8801) that receives inbound
-- SMS/MMS for that DID, and whose outbound external SMS uses that DID
-- as the sender by default.
CREATE TABLE IF NOT EXISTS did_sms_routes (
  did TEXT PRIMARY KEY,
  dest_exten TEXT NOT NULL,  -- comma-separated destination extensions
  label TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Dedupe for voip.ms URL callbacks (they retry if they don't get "ok").
CREATE TABLE IF NOT EXISTS voipms_sms_dedupe (
  voipms_id TEXT PRIMARY KEY,
  received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);
-- keys: fallback_mailbox, feature_vm (*97), feature_spy (*555),
--       disa_pin_hash, stripe_secret, stripe_publishable,
--       stripe_webhook_secret, plans_json, ...

CREATE TABLE IF NOT EXISTS signins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login TEXT NOT NULL,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  ip TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  actor TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);

-- Per-user call handling, edited from the user control panel (/ucp).
-- Separate table (not columns on logins) so existing DBs pick it up via
-- CREATE TABLE IF NOT EXISTS without an ALTER migration.
CREATE TABLE IF NOT EXISTS user_prefs (
  login_id INTEGER PRIMARY KEY REFERENCES logins(id) ON DELETE CASCADE,
  dnd INTEGER NOT NULL DEFAULT 0,            -- 1 = do not disturb: straight to voicemail
  forward_always TEXT NOT NULL DEFAULT '',   -- exten or outside number; '' = off
  forward_noanswer TEXT NOT NULL DEFAULT '', -- used instead of voicemail on no answer
  ring_seconds INTEGER NOT NULL DEFAULT 0,   -- ring time before voicemail; 0 = default (30s)
  answer_ivr_id INTEGER DEFAULT NULL,        -- answer my calls with this IVR menu (own menu)
  recording_consent_at TEXT DEFAULT NULL,    -- when the user acknowledged recording laws
  recording_consent_version INTEGER NOT NULL DEFAULT 0,
  text_email INTEGER NOT NULL DEFAULT 1,     -- email me when I get a text
  msg_in INTEGER NOT NULL DEFAULT 1,         -- accept text messages
  msg_out INTEGER NOT NULL DEFAULT 1,        -- allow sending text messages
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- IVR / auto-attendant menus. An inbound route can send callers to a menu
-- (inbound_routes.ivr_id), and a menu can have an internal number (exten)
-- so phones can dial it. Destinations are JSON: {"type": T, "target": X}
--   T = ext (ring extension X) | vm (voicemail of extension X)
--     | group (ring comma-separated extensions X) | ivr (menu id X)
--     | repeat | hangup
CREATE TABLE IF NOT EXISTS ivr_menus (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_login_id INTEGER DEFAULT NULL REFERENCES logins(id) ON DELETE CASCADE,
                                             -- NULL = system menu (admin); else the user's own
  name TEXT NOT NULL,
  exten TEXT NOT NULL DEFAULT '',            -- optional internal number, e.g. 7000
  greeting_path TEXT NOT NULL DEFAULT '',    -- 8 kHz mono WAV under /var/lib/pbx/ivr
  timeout_sec INTEGER NOT NULL DEFAULT 5,    -- wait for a key after the greeting
  max_retries INTEGER NOT NULL DEFAULT 2,    -- replays after no key / wrong key
  direct_dial INTEGER NOT NULL DEFAULT 1,    -- caller may dial an extension number
  options TEXT NOT NULL DEFAULT '{}',        -- {"1": {"type":"ext","target":"8800"}, ...}
  fallback TEXT NOT NULL DEFAULT '{"type":"hangup"}',  -- after retries run out
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ivr_owner_name ON ivr_menus(COALESCE(owner_login_id, 0), name);

-- Per-user feature quotas: the hook for a future billing system. A billing
-- job (or the admin) writes rows here; features check quota() before use.
--   feature 'ivr_menus': how many IVR menus the user may own (0 = none).
-- No row -> the default in kv_settings 'default_quota_<feature>'.
CREATE TABLE IF NOT EXISTS user_entitlements (
  login_id INTEGER NOT NULL REFERENCES logins(id) ON DELETE CASCADE,
  feature TEXT NOT NULL,
  quota INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'admin',      -- admin | plan:<id> (future billing)
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (login_id, feature)
);
-- No free IVR menus: users get menus from a paid plan (or an admin override).
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('default_quota_ivr_menus', '0');
-- No plan = internal (extension-to-extension) calls only.
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('default_quota_call_minutes', '0');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('default_quota_voicemail', '0');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('default_quota_messages', '0');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('default_quota_recording', '0');

-- Stripe billing (monthly plans). Keys live in kv_settings:
--   stripe_secret, stripe_webhook_secret, billing_public_url (optional).
-- A plan grants quotas via user_entitlements (source 'plan:<id>').
CREATE TABLE IF NOT EXISTS billing_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  ivr_menus INTEGER NOT NULL DEFAULT 1,      -- quota granted while subscribed
  call_minutes INTEGER NOT NULL DEFAULT 0,   -- outside-call minutes / month (-1 = unlimited)
  voicemail INTEGER NOT NULL DEFAULT 0,      -- 1 = voicemail included
  messages INTEGER NOT NULL DEFAULT 0,       -- texts sent / month (-1 = unlimited)
  recording INTEGER NOT NULL DEFAULT 0,      -- 1 = "record my calls" included
  price_cents INTEGER NOT NULL,              -- per month
  currency TEXT NOT NULL DEFAULT 'usd',
  stripe_mode TEXT NOT NULL DEFAULT '',      -- test | live: which account the ids belong to
  stripe_product_id TEXT NOT NULL DEFAULT '',
  stripe_price_id TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,         -- offered to users
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Old Stripe price ids for a plan (prices are immutable; a price change
-- creates a new one). Lets existing subscribers on an old price map back.
CREATE TABLE IF NOT EXISTS billing_plan_prices (
  stripe_price_id TEXT PRIMARY KEY,
  plan_id INTEGER NOT NULL REFERENCES billing_plans(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS billing_customers (
  login_id INTEGER PRIMARY KEY REFERENCES logins(id) ON DELETE CASCADE,
  stripe_mode TEXT NOT NULL DEFAULT '',
  stripe_customer_id TEXT NOT NULL DEFAULT '',
  subscription_id TEXT NOT NULL DEFAULT '',
  subscription_item_id TEXT NOT NULL DEFAULT '',
  plan_id INTEGER DEFAULT NULL,
  status TEXT NOT NULL DEFAULT '',           -- Stripe subscription status
  current_period_end INTEGER NOT NULL DEFAULT 0,
  cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
  synced_at INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_billing_cust ON billing_customers(stripe_customer_id);
-- Webhook events already processed (Stripe retries; handle each once).
CREATE TABLE IF NOT EXISTS billing_events (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- inbound_routes.ivr_id (added by pbx-api's startup migration on existing DBs):
-- when set, the DID goes to that IVR instead of ringing ring_extens.

-- Public IP changes seen by the dynamic-DNS checker (pbx-api/ddns.py).
CREATE TABLE IF NOT EXISTS ip_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  old_ip TEXT NOT NULL DEFAULT '',
  new_ip TEXT NOT NULL
);
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('sip_tls_port', '5061');

-- API keys for the v1 REST API ("apis are the ext"). Bearer tokens; only the
-- sha256 hash is stored, the raw token is shown once at creation.
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login_id INTEGER NOT NULL REFERENCES logins(id) ON DELETE CASCADE,
  name TEXT NOT NULL DEFAULT '',
  key_hash TEXT NOT NULL UNIQUE,          -- sha256 hex of the bearer token
  prefix TEXT NOT NULL DEFAULT '',        -- first chars, for identification
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT,
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

-- Safety defaults: kill switch (0=off) halts all calls + registrations;
-- safety lock (0=off) makes the API/panel read-only for routine mutations.
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('kill_switch', '0');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('safety_lock', '0');

-- Security events (edge fail2ban bans via pbx-edge-notify, panel lockouts,
-- kill switch / safety lock). Shown in the dashboard Activity feed; deleted after 24 hours.
CREATE TABLE IF NOT EXISTS security_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  source TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  ip TEXT NOT NULL DEFAULT '',
  jail TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_security_events_at ON security_events(at);


-- E911: per-user emergency caller ID + the address registered for it with
-- the trunk provider. Edited only on the admin E911 page after unlocking it
-- with the E911 code. Users without a row (or disabled) use the office
-- fallback (kv e911_fallback_did / e911_fallback_address). 911 calls are
-- never blocked (pbx-brain/e911.py).
CREATE TABLE IF NOT EXISTS e911_users (
  login_id INTEGER PRIMARY KEY REFERENCES logins(id) ON DELETE CASCADE,
  enabled INTEGER NOT NULL DEFAULT 0,
  did TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_by TEXT NOT NULL DEFAULT ''
);

-- Ring groups: ring several extensions at once. Inbound routes can point at a
-- group (inbound_routes.group_id, added by pbx-api's startup migration), and
-- a group can have its own internal number. If nobody answers (vm_mode):
-- 'member' = vm_exten's voicemail (default: first member), 'all' = every
-- member gets their own copy, 'ext'/'group'/'ivr'/'number' = send the caller
-- on to noanswer_target (extension / ring group id / IVR id / outside
-- number), 'none' = hang up.
CREATE TABLE IF NOT EXISTS ring_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  exten TEXT DEFAULT NULL,
  members TEXT NOT NULL DEFAULT '[]',
  ring_seconds INTEGER NOT NULL DEFAULT 30,
  vm_mode TEXT NOT NULL DEFAULT 'member',
  vm_exten TEXT NOT NULL DEFAULT '',
  noanswer_target TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1
);

-- Numbers a user blocked (extensions or outside numbers, digits only; US
-- numbers as 10 digits). Blocked callers can't ring or text that user.
CREATE TABLE IF NOT EXISTS blocked_numbers (
  login_id INTEGER NOT NULL REFERENCES logins(id) ON DELETE CASCADE,
  number TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (login_id, number)
);

-- Calls / texts a user reported to the admin (admin Reports page).
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  reporter_id INTEGER REFERENCES logins(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,                        -- call | message
  ref_id INTEGER DEFAULT NULL,               -- cdr.id / messages.id
  other TEXT NOT NULL DEFAULT '',            -- the reported extension/number
  detail TEXT NOT NULL DEFAULT '',           -- snapshot (message text / call info)
  note TEXT NOT NULL DEFAULT '',             -- reporter's comment
  status TEXT NOT NULL DEFAULT 'open'        -- open | closed
);
