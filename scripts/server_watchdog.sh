#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [[ ! "$APP_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]]; then
  echo "Invalid CleaningAIOS application path" >&2
  exit 2
fi

cd "$APP_DIR"
test -f .env || { echo "Production .env is missing" >&2; exit 2; }

compose=(docker compose --env-file .env --profile telegram)
services=(db web worker scheduler bot)
if docker compose --env-file .env --profile crawl config --services 2>/dev/null | grep -qx crawl4ai; then
  compose+=(--profile crawl)
  services+=(crawl4ai)
fi
repaired=0

for service in "${services[@]}"; do
  container_id="$("${compose[@]}" ps -q "$service" 2>/dev/null || true)"
  state="missing"
  health="none"
  if [[ -n "$container_id" ]]; then
    state="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || echo missing)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || echo missing)"
  fi
  if [[ "$state" != "running" || "$health" == "unhealthy" ]]; then
    echo "repairing service=$service state=$state health=$health"
    "${compose[@]}" up -d --no-build --force-recreate "$service"
    repaired=1
  fi
done

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
