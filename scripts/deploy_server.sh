#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_SHA="${1:-}"
APP_DIR="${2:-}"

if [[ ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "A full 40-character release SHA is required" >&2
  exit 2
fi
if [[ ! "$APP_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
  echo "The application path must be an absolute path without spaces" >&2
  exit 2
fi

if (( EUID != 0 )); then
  command -v sudo >/dev/null || { echo "Passwordless sudo is required" >&2; exit 2; }
  exec sudo -n -- "$0" "$TARGET_SHA" "$APP_DIR"
fi

for command_name in docker git curl flock python3; do
  command -v "$command_name" >/dev/null || { echo "$command_name is required" >&2; exit 2; }
done

cd "$APP_DIR"
test -d .git || { echo "The production directory is not a Git repository" >&2; exit 2; }
test -f .env || { echo "Production .env is missing" >&2; exit 2; }

git_safe() {
  git -c safe.directory="$APP_DIR" "$@"
}

for local_exclude in /backups/ /.runtime/; do
  grep -qxF "$local_exclude" .git/info/exclude 2>/dev/null || printf '%s\n' "$local_exclude" >> .git/info/exclude
done

lock_path="/tmp/cleaningaios-deploy-$(id -u).lock"
exec 9>"$lock_path"
flock -n 9 || { echo "Another CleaningAIOS deployment is running" >&2; exit 3; }

if [[ -n "$(git_safe status --porcelain --untracked-files=normal)" ]]; then
  echo "Production checkout contains local changes; deployment refused" >&2
  exit 3
fi

git_safe fetch --quiet --prune origin main
git_safe cat-file -e "$TARGET_SHA^{commit}"
remote_main="$(git_safe rev-parse origin/main)"
if [[ "$TARGET_SHA" != "$remote_main" ]]; then
  echo "Requested release is not the current origin/main commit" >&2
  exit 3
fi

previous_sha="$(git_safe rev-parse HEAD)"
build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
backup_root="${CLEANINGAIOS_BACKUP_DIR:-$(dirname "$APP_DIR")/cleaningaios-backups}"
mkdir -p "$backup_root"
chmod 700 "$backup_root"

configure_runtime_services() {
  compose_profiles=(--profile telegram)
  runtime_services=(db web worker scheduler bot)
  if docker compose --env-file .env --profile crawl config --services 2>/dev/null | grep -qx crawl4ai; then
    compose_profiles+=(--profile crawl)
    runtime_services+=(crawl4ai)
  fi
}

if db_container="$(docker compose --env-file .env ps -q db 2>/dev/null)" && [[ -n "$db_container" ]]; then
  backup_tmp="$backup_root/.cleaningaios-$previous_sha-$(date -u +%Y%m%dT%H%M%SZ).dump.tmp"
  backup_final="${backup_tmp%.tmp}"
  docker compose --env-file .env exec -T db pg_dump -U cleaningai -Fc cleaningai > "$backup_tmp"
  chmod 600 "$backup_tmp"
  mv "$backup_tmp" "$backup_final"
  echo "Database backup created before deployment"
fi

rollback_release() {
  local exit_code=$?
  trap - ERR
  if [[ "$previous_sha" != "$TARGET_SHA" ]]; then
    echo "Deployment failed; restoring application release $previous_sha" >&2
    git_safe checkout --quiet --detach "$previous_sha" || true
    export RELEASE_SHA="$previous_sha"
    export BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    configure_runtime_services
    docker compose --env-file .env build web worker scheduler migrate || true
    docker compose --env-file .env "${compose_profiles[@]}" up -d --no-build \
      "${runtime_services[@]}" || true
  fi
  exit "$exit_code"
}
trap rollback_release ERR

git_safe checkout --quiet --detach "$TARGET_SHA"
export RELEASE_SHA="$TARGET_SHA"
export BUILD_TIME="$build_time"
configure_runtime_services

docker compose --env-file .env config --quiet
docker compose --env-file .env build web worker scheduler migrate
docker compose --env-file .env up -d db
docker compose --env-file .env run --rm migrate
docker compose --env-file .env "${compose_profiles[@]}" up -d --no-build \
  "${runtime_services[@]}"

published_web="$(docker compose --env-file .env port web 8000 | tail -n 1)"
published_port="${published_web##*:}"
[[ "$published_port" =~ ^[0-9]+$ ]]
health_url="http://127.0.0.1:$published_port/health"
health_payload=""
for _ in $(seq 1 45); do
  if health_payload="$(curl --fail --silent --show-error --max-time 5 "$health_url" 2>/dev/null)"; then
    break
  fi
  sleep 2
done
test -n "$health_payload"
HEALTH_PAYLOAD="$health_payload" python3 - "$TARGET_SHA" <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["HEALTH_PAYLOAD"])
if payload.get("status") != "ok" or payload.get("database") != "ok":
    raise SystemExit("Application health check failed")
if payload.get("release_sha") != sys.argv[1]:
    raise SystemExit("Application release SHA does not match the deployed commit")
print("application_health=ok")
PY

docker compose --env-file .env --profile telegram exec -T bot python - <<'PY'
import json
import os
import urllib.request

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    raise SystemExit("TELEGRAM_BOT_TOKEN is missing in the bot container")
base = os.environ.get("TELEGRAM_BOT_API_BASE_URL", "").strip().rstrip("/") or "https://api.telegram.org"
with urllib.request.urlopen(f"{base}/bot{token}/getMe", timeout=10) as response:
    payload = json.load(response)
if not payload.get("ok") or not payload.get("result", {}).get("id"):
    raise SystemExit("Telegram getMe failed")
print("telegram_getme=ok")
PY

for service in "${runtime_services[@]}"; do
  container_id="$(docker compose --env-file .env "${compose_profiles[@]}" ps -q "$service")"
  test -n "$container_id"
  state="$(docker inspect --format '{{.State.Status}}' "$container_id")"
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
  test "$state" = "running"
  test "$health" != "unhealthy"
done

"$APP_DIR/scripts/install_server_watchdog.sh" "$APP_DIR"
docker compose --env-file .env "${compose_profiles[@]}" ps

trap - ERR
echo "deployed_release=$TARGET_SHA"
