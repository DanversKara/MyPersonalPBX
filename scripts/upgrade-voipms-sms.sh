#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Upgrade an existing working own-pbx system to the voip.ms SMS/MMS build.
#
# Run as root on the PBX server (the same box where install.sh/deploy.sh ran).
# Safe to re-run: every step is idempotent.
#
# What it does:
#   1. Backs up /var/lib/pbx/pbx.db and /opt/pbx
#   2. Applies the new DB schema (new tables are CREATE TABLE IF NOT EXISTS)
#   3. Copies the new code into /opt/pbx (via the normal deploy path)
#   4. Restarts pbx-api, pbx-brain, pbx-agi and reloads Asterisk
#   5. Verifies the webhook and services
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PBX_USER="pbx"
DB="/var/lib/pbx/pbx.db"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "== own-pbx voip.ms SMS upgrade =="
echo "Source: $SRC_DIR"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root (sudo $0)" >&2
  exit 1
fi

# --- 1. Backups ---
echo "-- Backups --"
mkdir -p /var/backups/pbx
if [ -f "$DB" ]; then
  cp -a "$DB" "/var/backups/pbx/pbx.db.$STAMP.bak"
  echo "DB backed up to /var/backups/pbx/pbx.db.$STAMP.bak"
else
  echo "WARNING: $DB not found; is this the PBX server?" >&2
fi
if [ -d /opt/pbx ]; then
  cp -a /opt/pbx "/opt/pbx.bak.$STAMP"
  echo "Code backed up to /opt/pbx.bak.$STAMP"
fi

# --- 2. DB schema (idempotent) ---
echo "-- Database schema --"
if [ -f "$DB" ]; then
  sqlite3 "$DB" < "$SRC_DIR/db/schema.sql"
  echo "schema applied"
  # sanity: new tables exist?
  for t in did_sms_routes voipms_sms_dedupe; do
    if sqlite3 "$DB" "SELECT name FROM sqlite_master WHERE type='table' AND name='$t';" | grep -q "$t"; then
      echo "  table $t OK"
    else
      echo "  ERROR: table $t missing" >&2
      exit 1
    fi
  done
  chown "$PBX_USER":"$PBX_USER" "$DB"
  chmod 600 "$DB"
else
  echo "Skipping schema: no DB file (fresh install uses deploy.sh instead)"
fi

# --- 3. Deploy code (reuses the normal deploy path) ---
echo "-- Deploying code --"
if [ -x "$SRC_DIR/scripts/deploy.sh" ]; then
  bash "$SRC_DIR/scripts/deploy.sh"
else
  echo "ERROR: $SRC_DIR/scripts/deploy.sh not found/executable" >&2
  exit 1
fi

# --- 4. Verify ---
echo "-- Verify --"
systemctl is-active --quiet pbx-api && echo "pbx-api: active" || echo "pbx-api: NOT active (check journalctl -u pbx-api)"
systemctl is-active --quiet pbx-brain && echo "pbx-brain: active" || echo "pbx-brain: NOT active (check journalctl -u pbx-brain)"
systemctl is-active --quiet pbx-agi && echo "pbx-agi: active" || echo "pbx-agi: NOT active (check journalctl -u pbx-agi)"

# Webhook should answer (locally, no token configured yet -> "ok")
if curl -sf --max-time 5 "http://127.0.0.1:8001/hooks/voipms-sms" | grep -q "ok"; then
  echo "webhook /hooks/voipms-sms: OK"
else
  echo "webhook check FAILED (curl http://127.0.0.1:8001/hooks/voipms-sms)" >&2
fi

echo ""
echo "== Upgrade complete =="
echo "Next:"
echo "  1. Open http://<pbx-ip>:8001/sms-routes (admin) and set:"
echo "     voip.ms API username/password, webhook token, and your 4 DID -> exten routes."
echo "  2. In voip.ms portal, paste the webhook URL into each DID's SMS URL Callback."
echo "  3. Send a test SMS to one DID; watch: journalctl -u pbx-api -f"
echo ""
echo "Rollback if needed:"
echo "  cp -a /var/backups/pbx/pbx.db.$STAMP.bak $DB"
echo "  rm -rf /opt/pbx && cp -a /opt/pbx.bak.$STAMP /opt/pbx"
echo "  systemctl restart pbx-api pbx-brain pbx-agi"
