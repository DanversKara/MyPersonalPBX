# SPDX-License-Identifier: GPL-2.0-or-later
# Login notice: "an own-pbx update is available, run pbx-update".
# deploy.sh installs this as /etc/profile.d/pbx-update-check.sh with
# @@SRC_DIR@@ filled in. It is sourced by every login shell, so: no exit,
# no set -e, quiet on any error, and it only ever tells - it never updates
# (an update restarts the phone services and would drop live calls).
# At most one check against GitHub per hour; a slow network gives up after 5 s.
if [ "$(id -u)" -eq 0 ] && [ -t 1 ] && [ -d "@@SRC_DIR@@/.git" ]; then
    (
        cd "@@SRC_DIR@@" || exit 0
        stamp=/run/pbx-update-check
        if [ ! -f "$stamp" ] || [ -n "$(find "$stamp" -mmin +60 2>/dev/null)" ]; then
            timeout 5 git fetch -q origin main 2>/dev/null && touch "$stamp"
        fi
        n=$(git rev-list --no-merges --count HEAD..origin/main 2>/dev/null) || exit 0
        if [ "${n:-0}" -gt 0 ]; then
            echo ""
            echo "  own-pbx: $n new update(s) on GitHub:"
            git log --no-merges --format='    - %s' HEAD..origin/main 2>/dev/null | head -5
            echo "  Run: pbx-update   (restarts phone services: pick a quiet moment)"
            echo ""
        fi
    )
fi
