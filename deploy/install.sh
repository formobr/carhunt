#!/usr/bin/env bash
# Render carhunt systemd user units and enable both timers. Idempotent.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"
dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

claude_path="$(command -v claude || true)"
if [ -z "$claude_path" ]; then
    echo "install.sh: 'claude' not found on PATH; install it first" >&2
    exit 1
fi
claude_dir="$(dirname "$claude_path")"

interval="$(cd "$root" && PYTHONPATH="$root/src" .venv/bin/python -c 'from carhunt.config import load_settings; print(load_settings().poll.interval_min)' 2>/dev/null || true)"
case "$interval" in
    ''|*[!0-9]*) interval=15 ;;
esac

timezone="$(cd "$root" && PYTHONPATH="$root/src" .venv/bin/python -c 'from carhunt.config import load_settings; print(load_settings().report.timezone)' 2>/dev/null || true)"
case "$timezone" in
    ''|*[!A-Za-z0-9_+/-]*) timezone=America/New_York ;;
esac

mkdir -p "$dest"
for f in carhunt-poll.service carhunt-poll.timer carhunt-summary.service carhunt-summary.timer; do
    sed -e "s|@CLAUDE_DIR@|$claude_dir|g" -e "s|@INTERVAL_MIN@|$interval|g" \
        -e "s|@TIMEZONE@|$timezone|g" \
        "$here/$f" > "$dest/$f"
done

systemctl --user daemon-reload
systemctl --user enable --now carhunt-poll.timer carhunt-summary.timer
systemctl --user list-timers 'carhunt*'
