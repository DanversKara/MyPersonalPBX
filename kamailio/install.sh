#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# install.sh — Kamailio edge proxy for own-pbx on a fresh Debian 13 LXC.
#
# What you get:
#   - Kamailio 6.x listening TLS-only on 5061 (public edge)
#   - rtpengine relaying RTP/RTCP (UDP 20000-20099) for NAT traversal
#   - fail2ban watching the edge for scanners/brute-forcers
#   - nftables: only 5061/tcp + RTP range public; SIP UDP LAN-only
#
# Run as root on the new LXC. Takes ~10-15 min (rtpengine builds from source).
set -euo pipefail

# -------------------- tunables --------------------
PUBLIC_HOST="${PUBLIC_HOST:-}"   # public hostname of this proxy (TLS cert CN), e.g. sip.example.com
ASTERISK_IP="${ASTERISK_IP:-}"   # LAN IP of the own-pbx server (stays private), e.g. 192.168.1.10
RTP_MIN="${RTP_MIN:-20000}"
RTP_MAX="${RTP_MAX:-20099}"
# --------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then echo "run as root"; exit 1; fi
# Resolve our own folder before any "cd" below (configs are copied from here).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if ! grep -qi debian /etc/os-release; then echo "Debian expected"; exit 1; fi

# Required settings (ask if not given on the command line).
if [ -z "$PUBLIC_HOST" ]; then read -rp "Public SIP hostname (e.g. sip.example.com): " PUBLIC_HOST; fi
if [ -z "$ASTERISK_IP" ]; then read -rp "LAN IP of the PBX server (e.g. 192.168.1.10): " ASTERISK_IP; fi
[[ "$PUBLIC_HOST" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || { echo "PUBLIC_HOST must be a hostname like sip.example.com"; exit 1; }
[[ "$ASTERISK_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "ASTERISK_IP must be an IPv4 address"; exit 1; }

LAN_IP="${LAN_IP:-$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')}"
[ -n "$LAN_IP" ] || { echo "could not detect LAN IP; set LAN_IP="; exit 1; }
LAN_CIDR="${LAN_CIDR:-$(echo "$LAN_IP" | cut -d. -f1-3).0/24}"

# Fresh Debian containers don't ship curl; install it before using it.
if ! command -v curl >/dev/null 2>&1; then
    echo "== installing curl =="
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null
fi
echo "== detecting public IP =="
PUBLIC_IP="${PUBLIC_IP:-$(curl -4fsSL --max-time 10 https://api.ipify.org 2>/dev/null || curl -4fsSL --max-time 10 https://ipv4.icanhazip.com 2>/dev/null || curl -4fsSL --max-time 10 https://ifconfig.me 2>/dev/null || true)}"
PUBLIC_IP="$(echo "$PUBLIC_IP" | tr -d '[:space:]')"
[ -n "$PUBLIC_IP" ] || { echo "could not detect public IP; set PUBLIC_IP="; exit 1; }

echo "LAN_IP=$LAN_IP  LAN_CIDR=$LAN_CIDR  PUBLIC_IP=$PUBLIC_IP  PUBLIC_HOST=$PUBLIC_HOST  ASTERISK_IP=$ASTERISK_IP"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl gnupg openssl fail2ban nftables git build-essential pkg-config lsb-release ca-certificates

echo "== installing Kamailio =="
CODENAME="$(lsb_release -sc)"
KAM_OK=0
# Try the kamailio.org repo (v6.x) first, fall back to the distro package.
curl -fsSL --max-time 20 http://deb.kamailio.org/kamailiodebkey.gpg -o /tmp/kamailio-key.gpg 2>/dev/null || true
if [ -s /tmp/kamailio-key.gpg ]; then
    if grep -q "BEGIN PGP" /tmp/kamailio-key.gpg; then
        gpg --batch --yes --dearmor -o /etc/apt/trusted.gpg.d/kamailio.gpg /tmp/kamailio-key.gpg
    else
        cp /tmp/kamailio-key.gpg /etc/apt/trusted.gpg.d/kamailio.gpg
    fi
    echo "deb http://deb.kamailio.org/kamailio60 $CODENAME main" > /etc/apt/sources.list.d/kamailio.list
    apt-get update && apt-get install -y kamailio kamailio-tls-modules && KAM_OK=1 || KAM_OK=0
fi
if [ "$KAM_OK" -ne 1 ]; then
    echo "kamailio.org repo unavailable — using distro package"
    rm -f /etc/apt/sources.list.d/kamailio.list
    apt-get update
    apt-get install -y kamailio kamailio-tls-modules
fi

echo "== installing rtpengine =="
# Debian 12/13 ship rtpengine (rtpengine-daemon). No kernel module in an LXC,
# so skip recommends (rtpengine-kernel-dkms) and run in userspace.
if apt-get install -y --no-install-recommends rtpengine-daemon; then
    echo "rtpengine: Debian package"
    # We run our own unit with our config; keep the packaged unit off.
    for u in rtpengine-daemon ngcp-rtpengine-daemon; do
        systemctl disable --now "$u" >/dev/null 2>&1 || true
    done
else
    echo "== no rtpengine package: building from source (release branch) =="
    for pkg in debhelper gperf pkg-config libavcodec-dev libavfilter-dev libavformat-dev \
        libavutil-dev libswresample-dev libglib2.0-dev libcurl4-openssl-dev libssl-dev \
        libevent-dev libpcap-dev libwebsockets-dev libxmlrpc-core-c3-dev libhiredis-dev \
        libmosquitto-dev libpcre2-dev libjson-glib-dev zlib1g-dev libsystemd-dev markdown \
        libncurses-dev libopus-dev libspandsp-dev libjwt-dev libmnl-dev libnftnl-dev \
        libiptc-dev libxtables-dev liburing-dev; do
        apt-get install -y --no-install-recommends "$pkg" >/dev/null 2>&1 || echo "  (skipped $pkg)"
    done
    rm -rf /tmp/rtpengine
    git clone --depth 1 --branch mr12.5.1 https://github.com/sipwise/rtpengine.git /tmp/rtpengine \
        || git clone --depth 1 https://github.com/sipwise/rtpengine.git /tmp/rtpengine
    make -C /tmp/rtpengine/daemon -j"$(nproc)"
    install -m 0755 /tmp/rtpengine/daemon/rtpengine /usr/sbin/rtpengine
fi
RTPENGINE_BIN="$(command -v rtpengine || true)"
[ -n "$RTPENGINE_BIN" ] || { echo "rtpengine binary not found after install"; exit 1; }
cd /
cat > /etc/systemd/system/rtpengine.service <<EOF
[Unit]
Description=rtpengine media relay for Kamailio edge proxy
After=network.target
[Service]
Type=simple
ExecStart=$RTPENGINE_BIN --config-file=/etc/rtpengine/rtpengine.conf --pidfile=/run/rtpengine.pid --foreground
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

echo "== TLS certificate (self-signed; replace with Let's Encrypt anytime) =="
mkdir -p /etc/kamailio/tls
openssl req -x509 -newkey rsa:2048 \
    -keyout /etc/kamailio/tls/key.pem -out /etc/kamailio/tls/cert.pem \
    -days 825 -nodes -subj "/CN=$PUBLIC_HOST" \
    -addext "subjectAltName=DNS:$PUBLIC_HOST"
# Kamailio runs as user "kamailio": it must be able to read the key.
chown root:kamailio /etc/kamailio/tls/key.pem 2>/dev/null || true
chmod 640 /etc/kamailio/tls/key.pem
chmod 644 /etc/kamailio/tls/cert.pem

echo "== writing configs =="
subst() { sed -e "s/@@LAN_IP@@/$LAN_IP/g" -e "s|@@LAN_CIDR@@|$LAN_CIDR|g" \
              -e "s/@@PUBLIC_IP@@/$PUBLIC_IP/g" -e "s/@@PUBLIC_HOST@@/$PUBLIC_HOST/g" \
              -e "s/@@ASTERISK_IP@@/$ASTERISK_IP/g" -e "s/@@RTP_MIN@@/$RTP_MIN/g" \
              -e "s/@@RTP_MAX@@/$RTP_MAX/g" "$1"; }
subst "$SCRIPT_DIR/kamailio.cfg.template" > /etc/kamailio/kamailio.cfg
cp "$SCRIPT_DIR/tls.cfg" /etc/kamailio/tls.cfg
mkdir -p /etc/rtpengine
subst "$SCRIPT_DIR/rtpengine.conf.template" > /etc/rtpengine/rtpengine.conf

echo "== fail2ban =="
cp "$SCRIPT_DIR"/fail2ban/filter.d/*.conf /etc/fail2ban/filter.d/
cp "$SCRIPT_DIR"/fail2ban/action.d/*.conf /etc/fail2ban/action.d/
cp "$SCRIPT_DIR/fail2ban/jail.d/kamailio-edge.conf" /etc/fail2ban/jail.d/
install -m 755 "$SCRIPT_DIR/pbx-edge-notify" /usr/local/sbin/pbx-edge-notify
# Never ban the office network (phones at home that reach sip.example.com via
# the router's NAT loopback show up as the router's LAN address).
[ -f /etc/fail2ban/jail.d/zz-local-ignore.conf ] || printf '[DEFAULT]\nignoreip = 127.0.0.1/8 ::1 %s\n' "$LAN_CIDR" > /etc/fail2ban/jail.d/zz-local-ignore.conf
systemctl enable --now fail2ban

echo "== firewall (nftables) =="
nft flush ruleset 2>/dev/null || true
nft add table inet filter
nft add chain inet filter input '{ type filter hook input priority 0; policy drop; }'
nft add rule inet filter input iif lo accept
nft add rule inet filter input ct state established,related accept
nft add rule inet filter input icmp type echo-request accept
nft add rule inet filter input tcp dport 22 ip saddr "$LAN_CIDR" accept
nft add rule inet filter input tcp dport 5061 accept
nft add rule inet filter input udp dport "$RTP_MIN-$RTP_MAX" accept
nft add rule inet filter input udp dport 5060 ip saddr "$LAN_CIDR" accept
nft list ruleset > /etc/nftables.conf
systemctl enable --now nftables

echo "== starting services =="
systemctl daemon-reload
systemctl enable --now rtpengine
sleep 2
systemctl enable --now kamailio
sleep 2
systemctl is-active kamailio rtpengine fail2ban

echo "== following public IP changes (pbx-edge-ip.timer, every minute) =="
"$(dirname "$(readlink -f "$0")")/update-public-ip.sh" --install-timer

echo
echo "== edge proxy up =="
echo "  Kamailio TLS : ${LAN_IP}:5061  (forward router TCP 5061 -> here)"
echo "  rtpengine    : UDP ${RTP_MIN}-${RTP_MAX}  (forward router UDP ${RTP_MIN}-${RTP_MAX} -> here)"
echo "  Asterisk stays private at ${ASTERISK_IP}; nothing else is public."
echo
echo "Next:"
echo "  1. Router: forward TCP 5061 and UDP ${RTP_MIN}-${RTP_MAX} to ${LAN_IP}."
echo "  2. DNS: ${PUBLIC_HOST} -> ${PUBLIC_IP} (or use the IP in Zoiper)."
echo "  3. Zoiper: domain=${PUBLIC_HOST}, outbound proxy=${PUBLIC_HOST}:5061;transport=tls"
echo "  4. On the PBX: re-render pjsip (support_path) via the panel/API or deploy.sh."
echo "  5. Real certificate for ${PUBLIC_HOST}: CF_Token=... ./get-cert-cloudflare.sh ${PUBLIC_HOST} you@example.com"
echo "  6. PBX panel -> Network: set the domain + Cloudflare token so DNS follows IP changes."
