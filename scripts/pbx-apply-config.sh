#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Regenerate Asterisk configs from the PBX DB and reload PJSIP.
# Runs as root via sudo from the pbx service user; see
# /etc/sudoers.d/pbx-apply-config (installed by deploy.sh).
# Single privileged entry point so the API/panel never need direct
# write access to /etc/asterisk or the Asterisk control socket.
set -euo pipefail
PBX_DB=/var/lib/pbx/pbx.db AST_CFG_DIR=/etc/asterisk \
    /opt/pbx/api-venv/bin/python /opt/pbx/config-gen/gen.py
asterisk -rx "module reload res_pjsip.so"
