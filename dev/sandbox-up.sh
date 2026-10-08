#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Bring up the sandbox Asterisk with OUR configs only.
set -euo pipefail

REPO="$HOME/workspace/own-pbx"
export PBX_DB=/var/lib/pbx/pbx.db AST_CFG_DIR=/etc/asterisk
API_VENV=/opt/pbx/api-venv

# --- ARI password (localhost only) ---
mkdir -p /etc/pbx
if [ ! -f /etc/pbx/ari.pass ]; then
    tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40 > /etc/pbx/ari.pass
    chmod 600 /etc/pbx/ari.pass
fi
ARI_PASS=$(cat /etc/pbx/ari.pass)

# --- static configs ---
cp "$REPO/config-gen/static/extensions.conf" /etc/asterisk/extensions.conf
sed "s/CHANGE_ME_AT_INSTALL/$ARI_PASS/" "$REPO/config-gen/static/ari.conf" \
    > /etc/asterisk/ari.conf
cat > /etc/asterisk/http.conf <<'EOF'
[general]
enabled=yes
bindaddr=127.0.0.1
bindport=8088
EOF

# --- seed DB + render pjsip.conf ---
"$API_VENV/bin/python" "$REPO/scripts/seed-test-db.py"
"$API_VENV/bin/python" "$REPO/config-gen/gen.py"

# --- (re)start asterisk ---
asterisk -rx "core stop now" >/dev/null 2>&1 || true
sleep 1
asterisk
sleep 3
echo "--- endpoints ---"
asterisk -rx "pjsip show endpoints" | head -12
echo "--- ARI ---"
asterisk -rx "module show like res_ari" | head -8
echo "UP. ARI pass in /etc/pbx/ari.pass"
