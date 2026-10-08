#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Phase 1 installer for Debian 12/13. Builds Asterisk 22 LTS from source
# (Debian's packaged Asterisk lags), then lays out our stack.
set -euo pipefail

AST_TARBALL="${AST_TARBALL:-asterisk-22-current.tar.gz}"  # tracks latest stable 22.x; old point releases get purged upstream
PBX_USER="pbx"

apt-get update
apt-get install -y build-essential wget libssl-dev libxml2-dev libsqlite3-dev \
    uuid-dev libjansson-dev libedit-dev libsrtp2-dev libopus-dev \
    python3 python3-venv python3-pip sqlite3 sudo sox libsox-fmt-mp3

# --- Asterisk ---
cd /usr/src
if [ ! -f "$AST_TARBALL" ]; then
    wget "https://downloads.asterisk.org/pub/telephony/asterisk/${AST_TARBALL}"
fi
# Extract dir name from the tarball (e.g. asterisk-22.11.0)
# NB: tar gets SIGPIPE from head closing the pipe; that's expected, so
# pipefail is off for this one line.
set +o pipefail
AST_SRC_DIR=$(tar tzf "$AST_TARBALL" 2>/dev/null | head -1 | cut -d/ -f1)
set -o pipefail
tar xzf "$AST_TARBALL"
cd "$AST_SRC_DIR"
./configure --with-pjproject-bundled --with-srtp
make -j"$(nproc)"
make install
make config
ldconfig

# --- PBX user and dirs ---
id "${PBX_USER}" >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin "${PBX_USER}"
mkdir -p /var/lib/pbx /var/spool/pbx/monitor/admin /var/spool/pbx/monitor/user \
         /var/spool/pbx/voicemail /var/lib/pbx/ivr /var/lib/pbx/vmgreet /etc/pbx
chown -R "${PBX_USER}" /var/lib/pbx /var/spool/pbx
# ARI recordings land in /var/spool/asterisk/recording/ (Asterisk runs as root).
# The brain (pbx user) moves finished recordings out of there, which needs
# write permission on the source dir. Asterisk-as-root can still write into
# a pbx-owned dir, so this is safe.
mkdir -p /var/spool/asterisk/recording
chown "${PBX_USER}" /var/spool/asterisk/recording

# --- ARI password (localhost only) ---
if [ ! -f /etc/pbx/ari.pass ]; then
    # NB: head closes the pipe early -> tr gets SIGPIPE; expected, so
    # pipefail is off for this one line.
    set +o pipefail
    tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 40 > /etc/pbx/ari.pass
    set -o pipefail
    chmod 600 /etc/pbx/ari.pass
    chown "${PBX_USER}" /etc/pbx/ari.pass
fi
# TODO: sed the password into /etc/asterisk/ari.conf on deploy

# --- Python venvs ---
python3 -m venv /opt/pbx/brain-venv
/opt/pbx/brain-venv/bin/pip install -q requests websocket-client  # ari_client.py is our own (ari-py is py2-only)
python3 -m venv /opt/pbx/api-venv
/opt/pbx/api-venv/bin/pip install -q fastapi uvicorn bcrypt jinja2 python-multipart

# --- Configs ---
# TODO: copy config-gen/static/*.conf to /etc/asterisk/, run config-gen/gen.py
# TODO: systemd units for pbx-brain, pbx-api, asterisk enable

echo "install.sh done. Next: deploy configs, create admin login, start services."
