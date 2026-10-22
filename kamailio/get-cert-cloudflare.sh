#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# get-cert-cloudflare.sh — get a free Let's Encrypt certificate for your SIP
# domain using Cloudflare DNS (no web server or open port 80 needed), install
# it for Kamailio, and renew it automatically.
#
#   CF_Token=<cloudflare token> ./get-cert-cloudflare.sh sip.example.com you@example.com
#
# The token needs Zone -> DNS -> Edit for the domain's zone (the same token
# you put on the PBX Network page works). Run as root on the Kamailio LXC.
# Renewal: acme.sh's cron job renews ~every 60 days and restarts Kamailio.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

DOMAIN="${1:-}"
EMAIL="${2:-}"
[ -n "$DOMAIN" ] && [ -n "$EMAIL" ] || { sed -n '2,10p' "$0"; exit 1; }
[ -n "${CF_Token:-}" ] || { echo "set CF_Token=<your Cloudflare API token>"; exit 1; }
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || { echo "bad domain: $DOMAIN"; exit 1; }

ACME="$HOME/.acme.sh/acme.sh"
if [ ! -x "$ACME" ]; then
    echo "== installing acme.sh =="
    apt-get install -y -qq curl socat cron >/dev/null
    curl -fsSL https://get.acme.sh | sh -s email="$EMAIL"
fi

echo "== requesting certificate for $DOMAIN (Cloudflare DNS challenge) =="
export CF_Token
"$ACME" --set-default-ca --server letsencrypt
"$ACME" --issue --dns dns_cf -d "$DOMAIN" --keylength ec-256 || {
    rc=$?
    # 2 = already issued and not due for renewal: still (re)install below.
    [ "$rc" -eq 2 ] || exit "$rc"
}

echo "== installing for Kamailio =="
mkdir -p /etc/kamailio/tls
"$ACME" --install-cert -d "$DOMAIN" --ecc \
    --key-file /etc/kamailio/tls/key.pem \
    --fullchain-file /etc/kamailio/tls/cert.pem \
    --reloadcmd "chown root:kamailio /etc/kamailio/tls/key.pem; chmod 640 /etc/kamailio/tls/key.pem; systemctl restart kamailio"

# Kamailio must treat the domain as itself (requests addressed to it).
if ! grep -qx "alias=$DOMAIN" /etc/kamailio/kamailio.cfg; then
    sed -i "0,/^alias=/s//alias=$DOMAIN\nalias=/" /etc/kamailio/kamailio.cfg
    kamailio -c -f /etc/kamailio/kamailio.cfg >/dev/null && systemctl restart kamailio
    echo "added alias=$DOMAIN to kamailio.cfg"
fi

echo
openssl x509 -in /etc/kamailio/tls/cert.pem -noout -subject -enddate
echo "done: phones can now connect to $DOMAIN:5061 (TLS) without certificate warnings."
echo "check: cd /root/kamailio && PUBLIC_HOST=$DOMAIN ./verify-edge.sh"
