#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${1:-$(pwd)}"
if [[ ! "$APP_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
  echo "The application path must be absolute and contain no spaces" >&2
  exit 2
fi
command -v crontab >/dev/null || { echo "crontab is required for the server watchdog" >&2; exit 2; }

runtime_dir="${CLEANINGAIOS_RUNTIME_DIR:-$(dirname "$APP_DIR")/cleaningaios-runtime}"
watchdog="$APP_DIR/scripts/server_watchdog.sh"
marker="# CleaningAIOS watchdog managed entry"
mkdir -p "$runtime_dir"
chmod 700 "$runtime_dir"
chmod 700 "$watchdog"

current_cron="$(mktemp)"
next_cron="$(mktemp)"
trap 'rm -f "$current_cron" "$next_cron"' EXIT
crontab -l > "$current_cron" 2>/dev/null || true
grep -Fv "$marker" "$current_cron" | grep -Fv "$watchdog" > "$next_cron" || true
{
  printf '%s\n' "$marker"
  printf '*/2 * * * * /usr/bin/flock -n /tmp/cleaningaios-watchdog-%s.lock %s %s >> %s/watchdog.log 2>&1\n' \
    "$(id -u)" "$watchdog" "$APP_DIR" "$runtime_dir"
} >> "$next_cron"
crontab "$next_cron"

"$watchdog" "$APP_DIR"
echo "watchdog_install=ok interval_minutes=2"
