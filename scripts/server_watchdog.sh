#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [[ ! "$APP_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
  echo "Invalid CleaningAIOS application path" >&2
  exit 2
fi

cd "$APP_DIR"
test -f .env || { echo "Production .env is missing" >&2; exit 2; }
command -v flock >/dev/null || { echo "flock is required" >&2; exit 2; }

# The deployment holds this same lock while checking out, building and replacing
# services. A watchdog cycle that overlaps it must not pull/recreate containers
# from a half-deployed Compose definition.
lock_path="/tmp/cleaningaios-deploy-$(id -u).lock"
exec 9>"$lock_path"
if ! flock -n 9; then
  echo "watchdog_skipped=deployment_in_progress"
  exit 0
fi

worker_replicas="$(sed -nE 's/^AGENT_WORKER_REPLICAS=([0-9]+)$/\1/p' .env | tail -n 1)"
worker_replicas="${worker_replicas:-4}"
if [[ ! "$worker_replicas" =~ ^[0-9]+$ ]] || (( worker_replicas < 1 || worker_replicas > 60 )); then
  echo "AGENT_WORKER_REPLICAS must be an integer from 1 to 60" >&2
  exit 2
fi

compose=(docker compose --env-file .env --profile telegram)
services=(db web worker scheduler bot)
jarvis_enabled=0
if docker compose --env-file .env --profile jarvis config --services 2>/dev/null | grep -qx openjarvis; then
  compose+=(--profile jarvis)
  services+=(ollama openjarvis)
  jarvis_enabled=1
fi
if docker compose --env-file .env --profile crawl config --services 2>/dev/null | grep -qx crawl4ai; then
  compose+=(--profile crawl)
  services+=(crawl4ai)
fi
repaired=0

for service in "${services[@]}"; do
  container_ids="$("${compose[@]}" ps -q "$service" 2>/dev/null || true)"
  expected_count=1
  [[ "$service" == "worker" ]] && expected_count="$worker_replicas"
  container_count="$(printf '%s\n' "$container_ids" | sed '/^$/d' | wc -l | tr -d ' ')"
  service_healthy=1
  [[ "$container_count" == "$expected_count" ]] || service_healthy=0
  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    state="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || echo missing)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || echo missing)"
    if [[ "$state" != "running" || "$health" == "unhealthy" ]]; then
      service_healthy=0
    fi
  done <<< "$container_ids"
  if (( service_healthy == 0 )); then
    echo "repairing service=$service containers=$container_count expected=$expected_count"
    if [[ "$service" == "worker" ]]; then
      "${compose[@]}" up -d --no-build --force-recreate \
        --scale "worker=$worker_replicas" worker
    else
      "${compose[@]}" up -d --no-build --force-recreate "$service"
    fi
    repaired=1
  fi
done

if (( jarvis_enabled )); then
  jarvis_model_id="$(
    "${compose[@]}" exec -T ollama ollama list |
      awk '$1 == "qwen3:0.6b" {print $2}'
  )"
  test "$jarvis_model_id" = "7df6b6e09427"
fi

published_web="$("${compose[@]}" port web 8000 | tail -n 1)"
published_port="${published_web##*:}"
[[ "$published_port" =~ ^[0-9]+$ ]]
health_url="http://127.0.0.1:$published_port/health"
for _ in $(seq 1 15); do
  if curl --fail --silent --max-time 5 "$health_url" >/dev/null; then
    echo "watchdog_health=ok repaired=$repaired"
    exit 0
  fi
  sleep 2
done

echo "CleaningAIOS remained unhealthy after watchdog repair" >&2
exit 1
