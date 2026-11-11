#!/usr/bin/env bash
# Render the journal page and copy it to the web host. Never fails the poll.
# Host and directory come from CARHUNT_WEB_SSH / CARHUNT_WEB_DIR, in the environment
# or in the repo's .env (KEY=VALUE lines, parsed, never sourced). No host = render only.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"
out="$root/data/web/index.html"

host="${CARHUNT_WEB_SSH:-}"
dir="${CARHUNT_WEB_DIR:-}"
if [ -f "$root/.env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in
            CARHUNT_WEB_SSH=* | CARHUNT_WEB_DIR=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        value="${value%"${value##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        case "$value" in
            \"*\") value="${value#\"}"; value="${value%\"}" ;;
            \'*\') value="${value#\'}"; value="${value%\'}" ;;
        esac
        if [ "$key" = CARHUNT_WEB_SSH ] && [ -z "$host" ]; then host="$value"; fi
        if [ "$key" = CARHUNT_WEB_DIR ] && [ -z "$dir" ]; then dir="$value"; fi
    done < "$root/.env"
fi

if ! (cd "$root" && "$root/.venv/bin/carhunt" web --out "$out") >/dev/null; then
    echo "publish-web.sh: render failed; skipping publish" >&2
    exit 0
fi

if [ -z "$host" ]; then
    echo "publish skipped: CARHUNT_WEB_SSH not set" >&2
    exit 0
fi
if [ -z "$dir" ]; then
    echo "publish skipped: CARHUNT_WEB_DIR not set" >&2
    exit 0
fi

if ! ssh -o BatchMode=yes -o ConnectTimeout=15 "$host" \
    "mkdir -p '$dir' && cat > '$dir/index.html.tmp' && mv '$dir/index.html.tmp' '$dir/index.html'" \
    < "$out"; then
    echo "publish-web.sh: upload to $host:$dir failed" >&2
fi
exit 0
