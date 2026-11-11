#!/usr/bin/env bash
# Disable and remove the carhunt systemd user units.
set -euo pipefail

dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now carhunt-poll.timer carhunt-summary.timer 2>/dev/null || true
for f in carhunt-poll.service carhunt-poll.timer carhunt-summary.service carhunt-summary.timer; do
    rm -f "$dest/$f"
done
systemctl --user daemon-reload
echo "carhunt timers removed"
