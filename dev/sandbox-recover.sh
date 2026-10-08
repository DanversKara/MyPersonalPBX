#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Rebuild the whole sandbox Phase 1 environment from scratch.
# Idempotent: safe to run after a VM replacement or any time.
set -euo pipefail

REPO="$HOME/workspace/own-pbx"

echo "== apt: asterisk =="
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq asterisk

echo "== venvs =="
mkdir -p /opt/pbx
[ -d /opt/pbx/api-venv ] || python3 -m venv /opt/pbx/api-venv
/opt/pbx/api-venv/bin/pip install -q fastapi uvicorn bcrypt jinja2 requests
[ -d /opt/pbx/brain-venv ] || python3 -m venv /opt/pbx/brain-venv
/opt/pbx/brain-venv/bin/pip install -q requests websocket-client

echo "== asterisk + configs + seed DB =="
bash "$REPO/scripts/sandbox-up.sh"

echo "== pbx-api :8001 =="
pkill -f "uvicorn app:app" 2>/dev/null || true
cd "$REPO/pbx-api"
nohup /opt/pbx/api-venv/bin/uvicorn app:app --host 127.0.0.1 --port 8001 \
    >/tmp/pbx-api.log 2>&1 &
sleep 2

echo "== pbx-brain =="
pkill -f "pbx-brain/app.py" 2>/dev/null || true
cd "$REPO/pbx-brain"
ARI_PASS=$(cat /etc/pbx/ari.pass) nohup /opt/pbx/brain-venv/bin/python app.py \
    >/tmp/pbx-brain.log 2>&1 &
sleep 3

echo "== checks =="
curl -s http://127.0.0.1:8001/healthz; echo
PASS=$(cat /etc/pbx/ari.pass)
curl -s -u "pbxbrain:$PASS" http://127.0.0.1:8088/ari/applications/pbx-brain \
    | head -c 120; echo
asterisk -rx "pjsip show endpoints" | grep -cE "phone88" | xargs echo "endpoints:"
echo "RECOVER DONE"
