#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# One-command update: pull the latest code, deploy the PBX, and - only if the
# kamailio/ folder changed - copy it to the edge box and run update-edge.sh
# there. Run as root on the PBX:
#
#   pbx-update          (deploy.sh installs this shortcut)
#   pbx-update --edge   also update the edge even if kamailio/ didn't change
#   pbx-update --with-fax  also rebuild Asterisk with fax support (one time,
#                          ~15 min compile, then a short restart between calls)
#
# The edge's address is asked for once and kept in /etc/pbx/edge-host.
set -euo pipefail
# Everything is inside main() so bash reads the whole script before running:
# `git pull` may replace this very file while it runs.
main() {
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }

SRC_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
EDGE_FILE=/etc/pbx/edge-host
FORCE_EDGE=0
WITH_FAX=0
for arg in "$@"; do
    case "$arg" in
        --edge) FORCE_EDGE=1 ;;
        --with-fax) WITH_FAX=1 ;;
    esac
done

cd "$SRC_DIR"
command -v git >/dev/null || apt-get install -y -qq git
[ -d .git ] || { echo "$SRC_DIR is not a git copy; download it with git clone first"; exit 1; }

echo "== 1/3 Getting the latest code =="
git checkout -q main
OLD=$(git rev-parse HEAD)
git pull --ff-only
NEW=$(git rev-parse HEAD)
if [ "$OLD" = "$NEW" ]; then
    echo "Already up to date."
fi

echo "== 2/3 Updating the PBX =="
bash "$SRC_DIR/scripts/deploy.sh"

if [ "$WITH_FAX" -eq 1 ]; then
    echo "== Fax support (one-time Asterisk rebuild) =="
    bash "$SRC_DIR/scripts/enable-fax.sh"
fi

echo "== 3/3 Edge server =="
if [ "$FORCE_EDGE" -eq 0 ] && git diff --quiet "$OLD" "$NEW" -- kamailio/; then
    echo "No edge changes. Done."
    return 0
fi
EDGE=$(cat "$EDGE_FILE" 2>/dev/null || true)
if [ -z "$EDGE" ]; then
    read -r -p "Edge server address (e.g. 192.168.8.115, blank = skip): " EDGE
    [ -n "$EDGE" ] || { echo "Edge skipped. Run 'pbx-update --edge' later."; return 0; }
    echo "$EDGE" > "$EDGE_FILE"
fi
echo "Updating the edge at $EDGE (you may be asked for its root password twice)..."
scp -qr "$SRC_DIR/kamailio" "root@$EDGE:/root/"
ssh "root@$EDGE" "cd /root/kamailio && bash ./update-edge.sh"
echo "Done."
}
main "$@"
