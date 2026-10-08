#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Own PBX production deploy. Run as root on the target Debian 12/13 server.
# Assumes install.sh has already run (Asterisk 22, venvs, pbx user, dirs).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PBX_USER="pbx"

echo "== Deploying own-pbx =="

# --- Database ---
if [ ! -f /var/lib/pbx/pbx.db ]; then
    echo "Creating database..."
    sqlite3 /var/lib/pbx/pbx.db < "$SRC_DIR/db/schema.sql"
    if [ -f "$SRC_DIR/db/prod-seed.sql" ]; then
        sqlite3 /var/lib/pbx/pbx.db < "$SRC_DIR/db/prod-seed.sql"
    fi
    chown "$PBX_USER" /var/lib/pbx/pbx.db
else
    echo "Database exists, running schema (idempotent)..."
    sqlite3 /var/lib/pbx/pbx.db < "$SRC_DIR/db/schema.sql"
fi

# The DB holds SIP passwords and API/SMTP/Stripe/Cloudflare secrets:
# readable by the pbx services only.
chown "$PBX_USER":"$PBX_USER" /var/lib/pbx/pbx.db
chmod 600 /var/lib/pbx/pbx.db
chmod 750 /var/lib/pbx

# IVR greetings: uploaded via the panel (pbx user), played by Asterisk (root).
# sox converts uploads (WAV/MP3) to the 8 kHz mono WAV Asterisk plays.
mkdir -p /var/lib/pbx/ivr /var/lib/pbx/vmgreet
chown "$PBX_USER" /var/lib/pbx/ivr /var/lib/pbx/vmgreet
if ! command -v sox >/dev/null 2>&1; then
    echo "Installing sox (IVR greeting conversion)..."
    apt-get install -y -qq sox libsox-fmt-mp3 || echo "WARNING: sox install failed; only 8kHz mono WAV greetings will be accepted"
fi

# ARI recording spool must be writable by the brain (pbx user) so it can
# move finished recordings out. Asterisk runs as root and can still write in.
mkdir -p /var/spool/asterisk/recording
chown "$PBX_USER" /var/spool/asterisk/recording

# --- Code ---
mkdir -p /opt/pbx/pbx-brain /opt/pbx/pbx-api /opt/pbx/config-gen
cp "$SRC_DIR/pbx-brain/"*.py /opt/pbx/pbx-brain/
cp "$SRC_DIR/pbx-api/"*.py /opt/pbx/pbx-api/
cp -r "$SRC_DIR/config-gen/"* /opt/pbx/config-gen/
cp "$SRC_DIR/db/schema.sql" /opt/pbx/
mkdir -p /opt/pbx/db
cp "$SRC_DIR/db/schema.sql" /opt/pbx/db/
# Code, venvs and helper scripts are root-owned and NOT writable by the pbx
# service user: the sudo-allowed wrappers run /opt/pbx code as root, so if
# pbx could edit any of it, a bug in the web app would become root access.
# The pbx user only writes to /var/lib/pbx, /var/spool/pbx and the
# recording spool.
chown -R root:root /opt/pbx
chmod -R u+rwX,go+rX,go-w /opt/pbx

# --- Static Asterisk configs ---
cp "$SRC_DIR/config-gen/static/extensions.conf" /etc/asterisk/extensions.conf
cp "$SRC_DIR/config-gen/static/ari.conf" /etc/asterisk/ari.conf
cp "$SRC_DIR/config-gen/static/modules.conf" /etc/asterisk/modules.conf
cp "$SRC_DIR/config-gen/static/asterisk.conf" /etc/asterisk/asterisk.conf
cp "$SRC_DIR/config-gen/static/http.conf" /etc/asterisk/http.conf
# Inject the ARI password
ARI_PASS=$(cat /etc/pbx/ari.pass)
sed -i "s/^password *=.*/password = $ARI_PASS/" /etc/asterisk/ari.conf

# --- Generate PJSIP from DB ---
PBX_DB=/var/lib/pbx/pbx.db AST_CFG_DIR=/etc/asterisk \
    /opt/pbx/api-venv/bin/python /opt/pbx/config-gen/gen.py

# --- systemd units ---
cp "$SRC_DIR/scripts/systemd/"*.service /etc/systemd/system/
mkdir -p /opt/pbx/bin
cp "$SRC_DIR/scripts/systemd/pbx-brain-start.sh" /opt/pbx/bin/
chmod 755 /opt/pbx/bin/pbx-brain-start.sh
# Privileged config-apply wrapper: lets the pbx user regenerate Asterisk
# configs + reload PJSIP without direct write access to /etc/asterisk
# or the Asterisk control socket.
cp "$SRC_DIR/scripts/pbx-apply-config.sh" /opt/pbx/bin/pbx-apply-config.sh
chown root:root /opt/pbx/bin/pbx-apply-config.sh
chmod 755 /opt/pbx/bin/pbx-apply-config.sh
# Read-only status wrapper (device registrations for the dashboard)
cp "$SRC_DIR/scripts/pbx-status.sh" /opt/pbx/bin/pbx-status.sh
cp "$SRC_DIR/scripts/pbx-status-parse.py" /opt/pbx/bin/pbx-status-parse.py
chown root:root /opt/pbx/bin/pbx-status.sh /opt/pbx/bin/pbx-status-parse.py
chmod 755 /opt/pbx/bin/pbx-status.sh /opt/pbx/bin/pbx-status-parse.py
# sudo must be present for the sudoers.d drop-in (install.sh installs it,
# but be robust if deploy runs on a box that skipped it)
if [ ! -d /etc/sudoers.d ]; then
    apt-get update -qq && apt-get install -y -qq sudo
fi
# Re-assert ownership of everything created above (bin/, scripts).
chown -R root:root /opt/pbx
chmod -R go-w /opt/pbx
cp "$SRC_DIR/scripts/sudoers-pbx-apply-config" /etc/sudoers.d/pbx-apply-config
chmod 440 /etc/sudoers.d/pbx-apply-config
visudo -cf /etc/sudoers.d/pbx-apply-config
systemctl daemon-reload
systemctl enable pbx-brain pbx-api pbx-agi
# restart (not just start) so already-running services pick up new code
systemctl restart pbx-brain pbx-api pbx-agi

# --- Asterisk reload ---
# NB: Asterisk 22 removed the `pjsip reload` CLI command; reload the module instead.
asterisk -rx "dialplan reload" || true
asterisk -rx "module reload res_pjsip.so" || true

# Asterisk must be running (and start at boot).
systemctl enable --now asterisk >/dev/null 2>&1 || true

# First install: no admin login yet -> create one.
if [ "$(sqlite3 /var/lib/pbx/pbx.db "SELECT COUNT(*) FROM logins WHERE role='admin' AND enabled=1;" 2>/dev/null || echo 0)" = "0" ]; then
    if [ -t 0 ]; then
        echo ""; echo "== No admin login yet: create one now =="
        /opt/pbx/api-venv/bin/python "$SRC_DIR/scripts/create-admin.py"
    else
        echo "NOTE: no admin login yet. Run: /opt/pbx/api-venv/bin/python $SRC_DIR/scripts/create-admin.py"
    fi
fi

echo ""
echo "Deploy done. Check:"
echo "  systemctl status pbx-brain pbx-api pbx-agi"
echo "  curl -s http://127.0.0.1:8001/healthz"
echo "  asterisk -rx 'pjsip show endpoints'"
