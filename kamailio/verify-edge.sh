#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# verify-edge.sh — smoke-test the own-pbx Kamailio edge LXC after install.sh.
# Run as root on the edge LXC. Zero dependencies beyond what install.sh left.
# Exit 0 = all green, 1 = at least one check failed.
#
# Usage:  ./verify-edge.sh   (or PUBLIC_HOST=sip.example.com ./verify-edge.sh)
set -uo pipefail

PASS=0; FAIL=0; WARN=0
GREEN='\033[0;32m'; RED='\033[0;31m'; YEL='\033[0;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}PASS${NC} $1"; PASS=$((PASS+1)); }
no()   { echo -e "${RED}FAIL${NC} $1"; FAIL=$((FAIL+1)); }
warn() { echo -e "${YEL}WARN${NC} $1"; WARN=$((WARN+1)); }

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

EXPECTED_HOST="${PUBLIC_HOST:-}"
CFG=/etc/kamailio/kamailio.cfg
RTPCFG=/etc/rtpengine/rtpengine.conf
CERT=/etc/kamailio/tls/cert.pem
KEY=/etc/kamailio/tls/key.pem

echo "== 1. services =="
for s in kamailio rtpengine fail2ban nftables; do
    if systemctl is-active --quiet "$s"; then ok "$s active"; else no "$s NOT active"; fi
done

echo "== 2. kamailio config =="
[ -f "$CFG" ] || no "$CFG missing"
if kamailio -c -f "$CFG" 2>&1 | grep -qi "config ok\|config file ok\|syntax ok"; then ok "kamailio.cfg syntax ok"; else no "kamailio -c reported an error"; fi

echo "== 3. TLS listener 5061 =="
LAN_IP="$(grep -oP '^listen=tls:\K[0-9.]+' "$CFG" 2>/dev/null | head -1)"
[ -z "$LAN_IP" ] && LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')"
if ss -ltn 2>/dev/null | grep -qE ":5061\b"; then ok "TCP 5061 listening"; else no "nothing listening on TCP 5061"; fi

echo "== 4. TLS certificate =="
[ -f "$CERT" ] || no "$CERT missing"
[ -f "$KEY" ]  || no "$KEY missing"
if [ -f "$CERT" ]; then
    SAN="$(openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -o 'DNS:[^,]*' | head -1)"
    CN="$(openssl x509 -in "$CERT" -noout -subject 2>/dev/null | grep -oP 'CN\s*=\s*\K[^/,]+')"
    # Domain to check: PUBLIC_HOST if given, else the domain aliases in
    # kamailio.cfg (the cert must cover at least one of them).
    if [ -n "$EXPECTED_HOST" ]; then HOSTS="$EXPECTED_HOST"
    else HOSTS="$(grep -oP '^alias=\K\S+' "$CFG" 2>/dev/null | grep -v '^[0-9.]*$' | tr '\n' ' ')"; fi
    COVERED=""
    for h in $HOSTS; do
        if openssl x509 -in "$CERT" -noout -checkhost "$h" 2>/dev/null | grep -q 'does match'; then COVERED="$h"; break; fi
    done
    if [ -n "$COVERED" ]; then
        ok "cert covers $COVERED (SAN=$SAN CN=$CN)"
    else
        no "cert does not cover ${HOSTS:-any domain alias} (SAN=$SAN CN=$CN)"
    fi
    EXP="$(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2)"
    if openssl x509 -in "$CERT" -noout -checkend $((14*86400)) >/dev/null; then
        echo "     expires: $EXP"
    else
        warn "cert expires soon ($EXP); check acme.sh renewal: ~/.acme.sh/acme.sh --cron"
    fi
fi
if [ -f "$KEY" ] && [ "$(stat -c '%a %U:%G' "$KEY")" = "640 root:kamailio" ]; then ok "key.pem 640 root:kamailio (kamailio can read it, others can't)"
else warn "key.pem should be 640 root:kamailio: chown root:kamailio $KEY && chmod 640 $KEY"; fi

echo "== 5. TLS handshake to kamailio =="
if timeout 8 openssl s_client -connect "${LAN_IP:-127.0.0.1}:5061" -servername "$EXPECTED_HOST" </dev/null 2>/dev/null \
    | grep -q "Verify return code"; then
    ok "TLS handshake on 5061 succeeds"
else
    no "TLS handshake on 5061 failed"
fi

echo "== 6. rtpengine media ports =="
RTP_PORTS="$(ss -lun 2>/dev/null | awk '{print $5}' | grep -oE ':[0-9]+$' | tr -d ':' | sort -un)"
FOUND=0
for p in $RTP_PORTS; do
    if [ "$p" -ge 20000 ] && [ "$p" -le 20099 ]; then FOUND=1; break; fi
done
if [ "$FOUND" -eq 1 ]; then ok "rtpengine media ports in use (calls active)"; else ok "no calls right now (rtpengine opens 20000-20099 ports per call)"; fi
if ss -lun 2>/dev/null | grep -q "127.0.0.1:2223"; then ok "rtpengine ng control 127.0.0.1:2223/udp up"; else no "rtpengine ng control 127.0.0.1:2223/udp not listening (calls will have no audio): journalctl -u rtpengine -n 30"; fi

echo "== 7. nftables =="
NFT="$(nft list chain inet filter input 2>/dev/null)"
echo "$NFT" | grep -qE 'tcp dport 5061.*accept'        && ok "nft: 5061/tcp accepted"          || no "nft: no 5061/tcp accept rule"
echo "$NFT" | grep -qE 'udp dport 20000-20099.*accept' && ok "nft: UDP 20000-20099 accepted"   || no "nft: no RTP range accept rule"
echo "$NFT" | grep -qE 'udp dport 5060.*ip saddr.*accept' && ok "nft: 5060/udp LAN-only"      || warn "nft: no LAN-only 5060/udp rule"

echo "== 8. fail2ban =="
if fail2ban-client status kamailio-edge >/dev/null 2>&1; then
    ok "fail2ban jail kamailio-edge active"
else
    no "fail2ban jail kamailio-edge not running"
fi
if fail2ban-client status kamailio-edge-flood >/dev/null 2>&1; then
    ok "fail2ban jail kamailio-edge-flood active"
else
    warn "fail2ban flood jail not running (run ./update-edge.sh)"
fi
grep -q 'pike_check_req' "$CFG" 2>/dev/null && ok "flood limit (pike) in kamailio.cfg" || warn "no flood limit in kamailio.cfg (run ./update-edge.sh)"
if [ -r /etc/pbx-edge/notify.env ]; then
    ok "alerts to PBX configured (/etc/pbx-edge/notify.env)"
else
    warn "alerts to PBX not set up: pbx-edge-notify --setup http://<pbx>:8001 <admin API key>"
fi

echo "== 9. path to Asterisk (PBX side) =="
AST_IP="$(grep -oP '\$du = "sip:\K[0-9.]+' "$CFG" 2>/dev/null | head -1)"
if [ -n "$AST_IP" ]; then
    if timeout 3 bash -c "echo > /dev/udp/$AST_IP/5060" 2>/dev/null; then
        ok "Asterisk $AST_IP:5060 reachable from edge"
    else
        warn "Asterisk $AST_IP:5060 not reachable — check LAN link before pointing phones here"
    fi
else
    warn "could not parse Asterisk IP from kamailio.cfg"
fi

echo "== 10. advertised public IP still current =="
ADVERTISED="$(grep -oP '^interface\s*=\s*external/[0-9.]+!\K[0-9.]+' "$RTPCFG" 2>/dev/null | head -1)"
NOW="$(curl -fsSL --max-time 8 https://api.ipify.org 2>/dev/null || curl -fsSL --max-time 8 https://ifconfig.me 2>/dev/null || true)"
if [ -n "$ADVERTISED" ] && [ -n "$NOW" ]; then
    if [ "$ADVERTISED" = "$NOW" ]; then
        ok "advertised public IP $ADVERTISED matches current"
    else
        warn "public IP changed ($ADVERTISED -> $NOW); run ./update-public-ip.sh"
    fi
else
    warn "could not compare public IP (advertised=$ADVERTISED current=$NOW)"
fi

echo ""
echo "----------------------------------------"
echo -e "result: ${GREEN}$PASS pass${NC}  ${RED}$FAIL fail${NC}  ${YEL}$WARN warn${NC}"
[ "$FAIL" -eq 0 ]
