#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_SHA="${1:-}"
APP_DIR="${2:-}"
BUSINESS_CONFIG_PATH="${3:-}"

if [[ ! "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "A full 40-character release SHA is required" >&2
  exit 2
fi
if [[ ! "$APP_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
  echo "The application path must be an absolute path without spaces" >&2
  exit 2
fi
if [[ ! "$BUSINESS_CONFIG_PATH" =~ ^/tmp/cleaningaios-business-[0-9a-f]{40}\.env$ ]]; then
  echo "A validated temporary business configuration path is required" >&2
  exit 2
fi

if (( EUID != 0 )); then
  command -v sudo >/dev/null || { echo "Passwordless sudo is required" >&2; exit 2; }
  exec sudo -n -- "$0" "$TARGET_SHA" "$APP_DIR" "$BUSINESS_CONFIG_PATH"
fi

for command_name in docker git curl flock python3; do
  command -v "$command_name" >/dev/null || { echo "$command_name is required" >&2; exit 2; }
done

cd "$APP_DIR"
test -d .git || { echo "The production directory is not a Git repository" >&2; exit 2; }
test -f .env || { echo "Production .env is missing" >&2; exit 2; }
test -f "$BUSINESS_CONFIG_PATH" || { echo "Production business configuration is missing" >&2; exit 2; }

lock_path="/tmp/cleaningaios-deploy-$(id -u).lock"
exec 9>"$lock_path"
flock -n 9 || { echo "Another CleaningAIOS deployment is running" >&2; exit 3; }

worker_replicas="$(sed -nE 's/^AGENT_WORKER_REPLICAS=([0-9]+)$/\1/p' .env | tail -n 1)"
worker_replicas="${worker_replicas:-4}"
if [[ ! "$worker_replicas" =~ ^[0-9]+$ ]] || (( worker_replicas < 1 || worker_replicas > 60 )); then
  echo "AGENT_WORKER_REPLICAS must be an integer from 1 to 60" >&2
  exit 2
fi

git_safe() {
  git -c safe.directory="$APP_DIR" "$@"
}

for local_exclude in /backups/ /.runtime/; do
  grep -qxF "$local_exclude" .git/info/exclude 2>/dev/null || printf '%s\n' "$local_exclude" >> .git/info/exclude
done

if [[ -n "$(git_safe status --porcelain --untracked-files=normal)" ]]; then
  echo "Production checkout contains local changes; deployment refused" >&2
  exit 3
fi

deploy_ref="refs/cleaningaios/deploy/main"
git_safe update-ref -d "$deploy_ref" || true
git_safe fetch --quiet --force --no-tags origin \
  "refs/heads/main:$deploy_ref"
git_safe cat-file -e "$TARGET_SHA^{commit}"
remote_main="$(git_safe rev-parse "$deploy_ref^{commit}")"
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
  build_services=(web worker scheduler migrate bot)
  jarvis_enabled=0
  if docker compose --env-file .env --profile jarvis config --services 2>/dev/null | grep -qx openjarvis; then
    compose_profiles+=(--profile jarvis)
    runtime_services+=(ollama openjarvis)
    build_services+=(openjarvis)
    jarvis_enabled=1
  fi
  if docker compose --env-file .env --profile crawl config --services 2>/dev/null | grep -qx crawl4ai; then
    compose_profiles+=(--profile crawl)
    runtime_services+=(crawl4ai)
  fi
}

model_pull_network=""
ollama_container=""
cleanup_model_pull_network() {
  if [[ -n "$model_pull_network" ]]; then
    if [[ -n "$ollama_container" ]]; then
      docker network disconnect -f "$model_pull_network" "$ollama_container" \
        >/dev/null 2>&1 || true
    fi
    docker network rm "$model_pull_network" >/dev/null 2>&1 || true
    model_pull_network=""
    ollama_container=""
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
  cleanup_model_pull_network
  if [[ "$previous_sha" != "$TARGET_SHA" ]]; then
    echo "Deployment failed; restoring application release $previous_sha" >&2
    git_safe checkout --quiet --detach "$previous_sha" || true
    export RELEASE_SHA="$previous_sha"
    export BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    configure_runtime_services
    docker compose --env-file .env "${compose_profiles[@]}" build \
      "${build_services[@]}" || true
    docker compose --env-file .env "${compose_profiles[@]}" up -d --no-build \
      --scale "worker=$worker_replicas" \
      --remove-orphans "${runtime_services[@]}" || true
  fi
  exit "$exit_code"
}
trap rollback_release ERR

git_safe checkout --quiet --detach "$TARGET_SHA"
export RELEASE_SHA="$TARGET_SHA"
export BUILD_TIME="$build_time"
python3 - ".env" "$BUSINESS_CONFIG_PATH" <<'PY'
import os
from pathlib import Path
import re
import tempfile
import sys

environment_path = Path(sys.argv[1])
overlay_path = Path(sys.argv[2])
allowed = {
    "COMPANY_PHONE",
    "COMPANY_SERVICE_AREA",
    "MANAGEMENT_CONTACT_REGIONS",
}
phone_pattern = re.compile(r"^\+?[0-9 ()-]{7,32}$")

overlay: dict[str, str] = {}
for raw_line in overlay_path.read_text().splitlines():
    if not raw_line or "=" not in raw_line:
        raise SystemExit("Invalid production business configuration")
    key, value = raw_line.split("=", 1)
    if key not in allowed or key in overlay or not value or "\n" in value or "\r" in value:
        raise SystemExit("Invalid production business configuration")
    overlay[key] = value
if set(overlay) != allowed or not phone_pattern.fullmatch(overlay["COMPANY_PHONE"]):
    raise SystemExit("Invalid production business configuration")

lines = environment_path.read_text().splitlines()
positions: dict[str, int] = {}
for index, line in enumerate(lines):
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, _value = line.split("=", 1)
    if key in allowed:
        positions[key] = index
for key, value in overlay.items():
    replacement = f"{key}={value}"
    if key in positions:
        lines[positions[key]] = replacement
    else:
        lines.append(replacement)

descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{environment_path.name}.business.",
    dir=environment_path.parent,
)
temporary = Path(temporary_name)
try:
    with os.fdopen(descriptor, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, environment_path)
finally:
    temporary.unlink(missing_ok=True)
print("Production business configuration synchronized.")
PY
python3 scripts/bootstrap_secrets.py .env
grep -Eq '^OPENJARVIS_API_KEY=.+$' .env || {
  echo "OpenJarvis API key was not configured" >&2
  exit 3
}
configure_runtime_services

docker compose --env-file .env "${compose_profiles[@]}" config --quiet
docker compose --env-file .env "${compose_profiles[@]}" build \
  "${build_services[@]}"
docker compose --env-file .env up -d db
docker compose --env-file .env run --rm migrate
docker compose --env-file .env run --rm migrate python -m app.business_policy
if (( jarvis_enabled )); then
  docker compose --env-file .env "${compose_profiles[@]}" up -d --no-build ollama
  ollama_container="$(
    docker compose --env-file .env "${compose_profiles[@]}" ps -q ollama
  )"
  test -n "$ollama_container"
  model_pull_network="cleaningaios-model-pull"
  if docker network inspect "$model_pull_network" >/dev/null 2>&1; then
    echo "Stale OpenJarvis model-pull network exists" >&2
    exit 3
  fi
  docker network create --driver bridge \
    --label cleaningaios.purpose=openjarvis-model-pull \
    "$model_pull_network" >/dev/null
  docker network connect "$model_pull_network" "$ollama_container"
  docker compose --env-file .env "${compose_profiles[@]}" exec -T ollama \
    ollama pull qwen3:0.6b
  jarvis_model_id="$(
    docker compose --env-file .env "${compose_profiles[@]}" exec -T ollama \
      ollama list | awk '$1 == "qwen3:0.6b" {print $2}'
  )"
  test "$jarvis_model_id" = "7df6b6e09427"
  cleanup_model_pull_network
fi
docker compose --env-file .env "${compose_profiles[@]}" up -d --no-build \
  --scale "worker=$worker_replicas" \
  --remove-orphans "${runtime_services[@]}"

if (( jarvis_enabled )); then
  openjarvis_container="$(docker compose --env-file .env "${compose_profiles[@]}" ps -q openjarvis)"
  test -n "$openjarvis_container"
  openjarvis_health=""
  for _ in $(seq 1 45); do
    openjarvis_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$openjarvis_container")"
    [[ "$openjarvis_health" == "healthy" ]] && break
    [[ "$openjarvis_health" == "unhealthy" ]] && exit 1
    sleep 2
  done
  test "$openjarvis_health" = "healthy"
fi

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

if (( jarvis_enabled )); then
  docker compose --env-file .env "${compose_profiles[@]}" exec -T bot python - <<'PY'
import asyncio

from app.openjarvis_client import ask_openjarvis

answer = asyncio.run(ask_openjarvis("Ответь одним словом: работает"))
if not answer.strip():
    raise SystemExit("OpenJarvis returned an empty response")
print("openjarvis_advice=ok")
PY
fi

for service in "${runtime_services[@]}"; do
  container_ids="$(docker compose --env-file .env "${compose_profiles[@]}" ps -q "$service")"
  test -n "$container_ids"
  container_count="$(printf '%s\n' "$container_ids" | sed '/^$/d' | wc -l | tr -d ' ')"
  expected_count=1
  [[ "$service" == "worker" ]] && expected_count="$worker_replicas"
  test "$container_count" = "$expected_count"
  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    state="$(docker inspect --format '{{.State.Status}}' "$container_id")"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
    test "$state" = "running"
    test "$health" != "unhealthy"
  done <<< "$container_ids"
done

"$APP_DIR/scripts/install_server_watchdog.sh" "$APP_DIR"
docker compose --env-file .env "${compose_profiles[@]}" ps

trap - ERR
echo "deployed_release=$TARGET_SHA"
echo "agent_worker_replicas=$worker_replicas"
