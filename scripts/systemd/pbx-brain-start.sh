#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Wrapper for pbx-brain: reads ARI password from secure file.
set -e
export ARI_PASS=$(cat /etc/pbx/ari.pass)
exec /opt/pbx/brain-venv/bin/python /opt/pbx/pbx-brain/app.py
