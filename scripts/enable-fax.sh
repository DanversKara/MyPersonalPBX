#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# One-time: rebuild Asterisk with fax support (spandsp), so numbers with
# "Detect faxes" (Routes) can receive faxes. Run as root:
#
#   pbx-update --with-fax        (or: bash scripts/enable-fax.sh)
#
# Compiling takes roughly 10-20 minutes; the PBX keeps working meanwhile.
# Asterisk then restarts once, when no call is in progress ("when
# convenient"), which takes under a minute. Safe to run again: it stops
# early if fax support is already there.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

has_fax() { asterisk -rx "module show like res_fax_spandsp" 2>/dev/null | grep -q "1 modules loaded"; }

if has_fax; then
    echo "Fax support is already in Asterisk. Nothing to do."
    exit 0
fi

echo "== Installing the fax libraries =="
apt-get install -y -qq libspandsp-dev libtiff-dev libtiff-tools ghostscript

VER=$(asterisk -V 2>/dev/null | awk '{print $2}')      # e.g. 22.5.2
echo "== Asterisk $VER: finding its source =="
cd /usr/src
SRC=""
[ -n "$VER" ] && [ -d "asterisk-$VER" ] && SRC="asterisk-$VER"
if [ -z "$SRC" ] && [ -n "$VER" ]; then
    # The exact version keeps everything else identical. Old point releases
    # move to releases/ upstream, so try there too.
    for url in "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-$VER.tar.gz" \
               "https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-$VER.tar.gz"; do
        if wget -q -O "asterisk-$VER.tar.gz" "$url"; then
            tar xzf "asterisk-$VER.tar.gz" && SRC="asterisk-$VER" && break
        fi
    done
fi
if [ -z "$SRC" ]; then
    echo "Couldn't get the source for $VER; using the latest Asterisk 22 instead (same LTS line)."
    wget -q -O asterisk-22-current.tar.gz https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22-current.tar.gz
    set +o pipefail
    SRC=$(tar tzf asterisk-22-current.tar.gz 2>/dev/null | head -1 | cut -d/ -f1)
    set -o pipefail
    tar xzf asterisk-22-current.tar.gz
fi
cd "/usr/src/$SRC"

echo "== Configuring (with fax) =="
./configure --with-pjproject-bundled --with-srtp > /tmp/asterisk-configure.log 2>&1 || {
    tail -30 /tmp/asterisk-configure.log; echo "configure failed (full log: /tmp/asterisk-configure.log)"; exit 1; }
grep -q "^#define HAVE_SPANDSP 1" include/asterisk/autoconfig.h || {
    echo "configure didn't find spandsp (libspandsp-dev). Nothing was changed."; exit 1; }
make menuselect.makeopts >/dev/null
menuselect/menuselect --enable res_fax --enable res_fax_spandsp menuselect.makeopts

echo "== Compiling: this takes a while; the PBX keeps working =="
start=$(date +%s)
make -j"$(nproc)" > /tmp/asterisk-build.log 2>&1 || {
    tail -40 /tmp/asterisk-build.log; echo "Build failed (full log: /tmp/asterisk-build.log). Nothing was installed."; exit 1; }
echo "Compiled in $(( ($(date +%s) - start) / 60 )) min."

echo "== Installing =="
make install > /tmp/asterisk-install.log 2>&1 || { tail -20 /tmp/asterisk-install.log; exit 1; }
ldconfig

echo "== Restarting Asterisk as soon as no call is in progress =="
asterisk -rx "core restart when convenient" >/dev/null 2>&1 || systemctl restart asterisk
for _ in $(seq 1 120); do
    sleep 5
    has_fax && break
done
if has_fax; then
    systemctl restart pbx-brain pbx-agi 2>/dev/null || true
    echo "Fax support is on. Turn on Routes -> (a number) -> Detect faxes."
else
    echo "Asterisk hasn't come back with fax support yet. If calls were in progress it is still"
    echo "waiting for them to end; check later with: asterisk -rx 'module show like fax'"
fi
