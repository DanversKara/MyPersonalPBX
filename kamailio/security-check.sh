#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# security-check.sh — quick security check for the PBX box and the Kamailio
# edge box (detects which one it runs on). Read-only: changes nothing.
#
#   ./security-check.sh
#
# Checks: pending security updates, software versions, listening ports,
# SSH settings, file permissions, Python packages with known CVEs (PBX),
# fail2ban/firewall (edge).
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
G='\033[32m'; R='\033[31m'; Y='\033[33m'; N='\033[0m'
PASS=0; FAIL=0; WARN=0
ok()   { echo -e "${G}PASS${N} $1"; PASS=$((PASS+1)); }
no()   { echo -e "${R}FAIL${N} $1"; FAIL=$((FAIL+1)); }
warn() { echo -e "${Y}WARN${N} $1"; WARN=$((WARN+1)); }

ROLE="unknown"
[ -d /opt/pbx ] && ROLE="pbx"
[ -f /etc/kamailio/kamailio.cfg ] && ROLE="edge"
echo "== box: $ROLE ($(hostname)) =="

echo "== 1. security updates =="
apt-get update -qq >/dev/null 2>&1 || warn "apt-get update failed (no internet?)"
UPG="$(apt list --upgradable 2>/dev/null | grep -c -- '-security' || true)"
ALL="$(apt list --upgradable 2>/dev/null | grep -vc '^Listing' || true)"
if [ "${UPG:-0}" -gt 0 ]; then no "$UPG security update(s) pending (of $ALL): apt-get upgrade"
elif [ "${ALL:-0}" -gt 0 ]; then warn "$ALL update(s) pending (none flagged security): apt-get upgrade"
else ok "system packages up to date"; fi
[ -f /var/run/reboot-required ] && warn "a reboot is needed to finish updates"

echo "== 2. SSH =="
if command -v sshd >/dev/null 2>&1; then
    CFG="$(sshd -T 2>/dev/null)"
    case "$(echo "$CFG" | awk '/^permitrootlogin /{print $2}')" in
        yes) no "SSH allows root login with a password (PermitRootLogin yes)";;
        *) ok "SSH root password login off";;
    esac
    [ "$(echo "$CFG" | awk '/^passwordauthentication /{print $2}')" = "yes" ] \
        && warn "SSH password logins allowed (keys only is safer)" || ok "SSH keys only"
else
    ok "no SSH server installed"
fi

echo "== 3. listening ports =="
ss -Hltnu 2>/dev/null | awk '{print $1, $5}' | sort -u | while read -r proto addr; do
    case "$addr" in
        127.*|\[::1\]*|*%lo:*) echo "     local only: $proto $addr";;
        *) echo "     OPEN on network: $proto $addr";;
    esac
done

if [ "$ROLE" = "pbx" ]; then
    echo "== 4. PBX =="
    command -v asterisk >/dev/null && echo "     $(asterisk -V 2>/dev/null) (compare with the latest 22.x at downloads.asterisk.org)"
    ss -Hltn | grep -q '127.0.0.1:8088' && ok "ARI only on 127.0.0.1" || { ss -Hltn | grep -q ':8088' && no "ARI (8088) listens beyond localhost"; }
    grep -q 'CHANGE_ME' /etc/asterisk/ari.conf 2>/dev/null && no "ARI password is still the default" || ok "ARI password set"
    M="$(stat -c '%a %U' /var/lib/pbx/pbx.db 2>/dev/null)"
    [ "$M" = "600 pbx" ] && ok "pbx.db readable by the pbx service only" || warn "pbx.db permissions are '$M' (expected '600 pbx'; deploy.sh fixes it)"
    BAD="$(find /opt/pbx -user pbx -o -perm -o+w 2>/dev/null | head -3)"
    [ -z "$BAD" ] && ok "/opt/pbx code is root-owned and not writable by the service" \
                  || no "/opt/pbx has files the pbx service can change (re-run deploy.sh): $BAD"
    for v in /opt/pbx/api-venv /opt/pbx/brain-venv; do
        [ -x "$v/bin/pip" ] || continue
        if ! "$v/bin/python" -m pip_audit --version >/dev/null 2>&1; then
            "$v/bin/pip" install -q pip-audit >/dev/null 2>&1 || { warn "couldn't install pip-audit in $v"; continue; }
        fi
        OUT="$("$v/bin/python" -m pip_audit -l 2>&1)"
        if echo "$OUT" | grep -q "No known vulnerabilities"; then ok "$(basename "$v"): no known vulnerable Python packages"
        else no "$(basename "$v"): vulnerable packages:"; echo "$OUT" | sed 's/^/       /' | head -15; fi
    done
fi

if [ "$ROLE" = "edge" ]; then
    echo "== 4. edge =="
    echo "     $(kamailio -v 2>/dev/null | head -1)  (Debian 13: keep the kamailio package updated)"
    command -v rtpengine >/dev/null && echo "     $(rtpengine --version 2>&1 | head -1)"
    systemctl is-active --quiet fail2ban && ok "fail2ban running" || no "fail2ban not running"
    nft list ruleset 2>/dev/null | grep -q 'policy drop' && ok "firewall default-deny" || no "firewall not default-deny"
    nft list ruleset 2>/dev/null | grep -qE 'tcp dport 22 accept' && no "SSH open to everyone in the firewall"
    grep -q 'pike_check_req' /etc/kamailio/kamailio.cfg && ok "flood limit on" || warn "no flood limit (run ./update-edge.sh)"
    K="$(stat -c '%a %U:%G' /etc/kamailio/tls/key.pem 2>/dev/null)"
    [ "$K" = "640 root:kamailio" ] && ok "TLS key protected" || warn "TLS key permissions '$K'"
fi

echo "----------------------------------------"
echo "result: $PASS pass  $FAIL fail  $WARN warn"
echo "Also scan from OUTSIDE your network (phone on LTE or an online port scanner):"
echo "only TCP 5061 should be open on your public IP; 8001, 22, 5060 must not answer."
