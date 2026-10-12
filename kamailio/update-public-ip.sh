#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# update-public-ip.sh — refresh the public IP in kamailio.cfg (advertise,
# alias, record-route) and rtpengine.conf (address advertised in SDP) when
# your ISP changes it. Run as root on the Kamailio LXC.
#
#   ./update-public-ip.sh                 check once, update if changed
#   ./update-public-ip.sh 203.0.113.7     force a specific IP
#   ./update-public-ip.sh --quiet         only print when something changes
#   ./update-public-ip.sh --install-timer check every minute automatically
#                                          (systemd: pbx-edge-ip.timer)
#
# The PBX panel (Network page) keeps the Cloudflare DNS record in sync; this
# keeps the edge proxy itself in sync. Without it, after an IP change calls
# still connect (DNS is right) but have no audio, because SIP/SDP would
# still advertise the old IP.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

SELF="$(readlink -f "$0")"
QUIET=0
NEW_IP=""
for a in "$@"; do
    case "$a" in
        --quiet) QUIET=1 ;;
        --install-timer)
            cat > /etc/systemd/system/pbx-edge-ip.service <<EOF
[Unit]
Description=own-pbx edge: follow public IP changes
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$SELF --quiet
EOF
            cat > /etc/systemd/system/pbx-edge-ip.timer <<'EOF'
[Unit]
Description=own-pbx edge: check public IP every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF
            systemctl daemon-reload
            systemctl enable --now pbx-edge-ip.timer
            echo "installed: pbx-edge-ip.timer (checks every minute; logs: journalctl -u pbx-edge-ip)"
            exit 0 ;;
        -h|--help) sed -n '2,16p' "$SELF"; exit 0 ;;
        *) NEW_IP="$a" ;;
    esac
done

say() { [ "$QUIET" -eq 1 ] || echo "$@"; }

# One run at a time (the timer and a manual run must not race).
exec 9>/run/pbx-edge-ip.lock
flock -n 9 || { say "another update is running"; exit 0; }

valid_ip() {
    [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1}"; do [ "$o" -le 255 ] || return 1; done
    case "$1" in 10.*|127.*|192.168.*|169.254.*|0.*) return 1 ;; esac
    [[ "$1" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]] && return 1
    return 0
}

detect_ip() {
    local ip
    for url in https://api.ipify.org https://ipv4.icanhazip.com https://ifconfig.me/ip; do
        ip="$(curl -4fsSL --max-time 8 "$url" 2>/dev/null | tr -d '[:space:]')" || true
        if valid_ip "$ip"; then echo "$ip"; return 0; fi
    done
    ip="$(curl -4fsSL --max-time 8 https://cloudflare.com/cdn-cgi/trace 2>/dev/null | sed -n 's/^ip=//p')" || true
    valid_ip "$ip" && { echo "$ip"; return 0; }
    return 1
}

[ -n "$NEW_IP" ] || NEW_IP="$(detect_ip)" || { echo "could not detect public IP" >&2; exit 1; }
valid_ip "$NEW_IP" || { echo "not a public IPv4 address: $NEW_IP" >&2; exit 1; }

# Current public IP = the address rtpengine advertises in SDP (kamailio.cfg
# now advertises the domain name, which doesn't change).
OLD_IP="$(grep -oP '^interface\s*=\s*external/[0-9.]+!\K[0-9.]+' /etc/rtpengine/rtpengine.conf | head -1 || true)"
[ -n "$OLD_IP" ] || OLD_IP="$(grep -oP '^listen=tls:\S+ advertise \K[0-9.]+' /etc/kamailio/kamailio.cfg | head -1 || true)"
if [ "$OLD_IP" = "$NEW_IP" ]; then
    say "public IP $NEW_IP: no change"
    exit 0
fi
echo "public IP changed: ${OLD_IP:-unknown} -> $NEW_IP"
logger -t pbx-edge-ip "public IP changed: ${OLD_IP:-unknown} -> $NEW_IP"

cp /etc/kamailio/kamailio.cfg /etc/kamailio/kamailio.cfg.bak
cp /etc/rtpengine/rtpengine.conf /etc/rtpengine/rtpengine.conf.bak
if [ -n "$OLD_IP" ]; then
    O="${OLD_IP//./\\.}"
    sed -i -e "s/advertise ${O}:/advertise ${NEW_IP}:/" \
           -e "s/record_route_preset(\"${O}:/record_route_preset(\"${NEW_IP}:/" \
           -e "s/^alias=${O}\$/alias=${NEW_IP}/" /etc/kamailio/kamailio.cfg
    sed -i "s|!${O}\b|!${NEW_IP}|" /etc/rtpengine/rtpengine.conf
fi

if ! kamailio -c -f /etc/kamailio/kamailio.cfg > /dev/null 2>&1; then
    echo "kamailio.cfg check failed; restoring backup" >&2
    cp /etc/kamailio/kamailio.cfg.bak /etc/kamailio/kamailio.cfg
    cp /etc/rtpengine/rtpengine.conf.bak /etc/rtpengine/rtpengine.conf
    exit 1
fi
systemctl restart rtpengine kamailio
echo "done: new public IP $NEW_IP live"
logger -t pbx-edge-ip "kamailio + rtpengine now advertise $NEW_IP"
