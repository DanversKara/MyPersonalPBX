#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Read-only Asterisk status for the pbx API/dashboard. Runs as root via sudo
# from the pbx service user; see /etc/sudoers.d/pbx-apply-config.
# Outputs JSON: {"contacts": [{aor, ip, port, transport, status, rtt_ms}],
#                "fax_ready": true|false}  (fax_ready: res_fax_spandsp loaded).
# Takes no arguments.
set -euo pipefail
PBX_FAX_READY=$(asterisk -rx 'module show like res_fax_spandsp' 2>/dev/null | grep -c '1 modules loaded' || true)
export PBX_FAX_READY
asterisk -rx 'pjsip show contacts' | /opt/pbx/bin/pbx-status-parse.py
