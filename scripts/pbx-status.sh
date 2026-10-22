#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Read-only Asterisk status for the pbx API/dashboard. Runs as root via sudo
# from the pbx service user; see /etc/sudoers.d/pbx-apply-config.
# Outputs JSON: {"contacts": [{aor, ip, port, transport, status, rtt_ms}]}.
# Takes no arguments.
set -euo pipefail
asterisk -rx 'pjsip show contacts' | /opt/pbx/bin/pbx-status-parse.py
