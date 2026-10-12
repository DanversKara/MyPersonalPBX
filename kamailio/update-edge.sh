#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# update-edge.sh — bring an existing edge box up to date with this package
# without reinstalling: re-renders kamailio.cfg from the new template
# (keeping your IPs and domain aliases), installs the fail2ban filters/jails
# and the PBX alert hook, then restarts Kamailio and fail2ban. If the new
# config fails to start, the old one is put back automatically.
#
#   cd /root/kamailio && ./update-edge.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="${KCFG:-/etc/kamailio/kamailio.cfg}"
[ -f "$CFG" ] || { echo "no $CFG — run install.sh first"; exit 1; }

# Values from the running config.
ASTERISK_IP=$(grep -oP '\$du = "sip:\K[0-9.]+(?=:5060")' "$CFG" | head -1)
LAN_IP=$(grep -oP '^listen=udp:\K[0-9.]+' "$CFG" | head -1)
PUBLIC_IP=$(grep -oP '^interface\s*=\s*external/[0-9.]+!\K[0-9.]+' "${RTPCFG:-/etc/rtpengine/rtpengine.conf}" 2>/dev/null | head -1 || true)
[ -n "$PUBLIC_IP" ] || PUBLIC_IP=$(grep -oP '^listen=tls:\S+ advertise \K[0-9.]+' "$CFG" | head -1 || true)
# Domain: the one already advertised, else the alias the TLS cert covers.
PUBLIC_HOST=$(grep -oP '^listen=tls:\S+ advertise \K[^:\s]+' "$CFG" | grep -v '^[0-9.]*$' | head -1 || true)
if [ -z "$PUBLIC_HOST" ]; then
    CERT="${KCERT:-/etc/kamailio/tls/cert.pem}"
    for h in $(grep -oP '^alias=\K\S+' "$CFG" | grep -v '^[0-9.]*$' || true); do
        if [ -f "$CERT" ] && openssl x509 -in "$CERT" -noout -checkhost "$h" 2>/dev/null | grep -q 'does match'; then
            PUBLIC_HOST="$h"; break
        fi
    done
    [ -n "$PUBLIC_HOST" ] || PUBLIC_HOST=$(grep -oP '^alias=\K\S+' "$CFG" | grep -v '^[0-9.]*$' | head -1 || true)
fi
for v in ASTERISK_IP LAN_IP PUBLIC_IP PUBLIC_HOST; do
    [ -n "${!v}" ] || { echo "couldn't read $v from $CFG; not changing anything"; exit 1; }
done
echo "PBX $ASTERISK_IP · edge LAN $LAN_IP · public $PUBLIC_IP · domain $PUBLIC_HOST"

NEW="$(mktemp)"
sed -e "s/@@LAN_IP@@/$LAN_IP/g" -e "s/@@PUBLIC_IP@@/$PUBLIC_IP/g" \
    -e "s/@@PUBLIC_HOST@@/$PUBLIC_HOST/g" -e "s/@@ASTERISK_IP@@/$ASTERISK_IP/g" \
    "$SCRIPT_DIR/kamailio.cfg.template" > "$NEW"
# Keep every extra alias= line (e.g. added by get-cert-cloudflare.sh).
grep -oP '^alias=\S+' "$CFG" | while read -r a; do
    grep -qxF "$a" "$NEW" || sed -i "0,/^alias=/s//$a\nalias=/" "$NEW"
done
# Keep the TLS config path if it was customised.
TLSCFG=$(grep -oP 'modparam\("tls", "config", "\K[^"]+' "$CFG" | head -1)
[ -n "$TLSCFG" ] && sed -i "s#modparam(\"tls\", \"config\", \"[^\"]*\")#modparam(\"tls\", \"config\", \"$TLSCFG\")#" "$NEW"

if ! kamailio -c -f "$NEW" >/dev/null 2>&1; then
    echo "new config failed the check; nothing changed:"; kamailio -c -f "$NEW" 2>&1 | grep -i error | head -5
    rm -f "$NEW"; exit 1
fi
[ -n "${KCFG:-}" ] && { cp "$NEW" "$CFG.new"; rm -f "$NEW"; echo "test mode: wrote $CFG.new"; exit 0; }

BK="$CFG.before-update-$(date +%Y%m%d-%H%M%S)"
cp "$CFG" "$BK"
install -m 644 "$NEW" "$CFG"; rm -f "$NEW"
systemctl restart kamailio; sleep 2
if ! systemctl is-active --quiet kamailio; then
    echo "kamailio didn't start with the new config; restoring $BK"
    cp "$BK" "$CFG"; systemctl restart kamailio
    journalctl -u kamailio -n 15 --no-pager; exit 1
fi
echo "kamailio: updated and running (backup: $BK)"

cp "$SCRIPT_DIR"/fail2ban/filter.d/*.conf /etc/fail2ban/filter.d/
cp "$SCRIPT_DIR"/fail2ban/action.d/*.conf /etc/fail2ban/action.d/
cp "$SCRIPT_DIR/fail2ban/jail.d/kamailio-edge.conf" /etc/fail2ban/jail.d/
install -m 755 "$SCRIPT_DIR/pbx-edge-notify" /usr/local/sbin/pbx-edge-notify
if [ ! -f /etc/fail2ban/jail.d/zz-local-ignore.conf ]; then
    LAN_CIDR="$(echo "$LAN_IP" | cut -d. -f1-3).0/24"
    printf '[DEFAULT]\nignoreip = 127.0.0.1/8 ::1 %s\n' "$LAN_CIDR" > /etc/fail2ban/jail.d/zz-local-ignore.conf
    echo "fail2ban: never bans the office network $LAN_CIDR (/etc/fail2ban/jail.d/zz-local-ignore.conf)"
fi
systemctl restart fail2ban; sleep 2
fail2ban-client status | sed -n 's/.*Jail list:\s*/fail2ban jails: /p'
[ -r /etc/pbx-edge/notify.env ] || echo "next: connect alerts to the PBX:  pbx-edge-notify --setup http://$ASTERISK_IP:8001 <admin API key>"
