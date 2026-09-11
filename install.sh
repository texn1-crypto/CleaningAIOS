#!/usr/bin/env bash
set -euo pipefail

command -v docker >/dev/null || { echo "Docker Engine with Compose v2 is required"; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose v2 is required"; exit 1; }
[[ -f .env ]] || { echo "Create .env from .env.example and fill required secrets"; exit 1; }

docker compose build
docker compose up -d db
docker compose run --rm migrate
docker compose up -d web worker scheduler

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:${WEB_PORT:-8000}/health >/dev/null 2>&1; then
    docker compose ps
    echo "CleaningAI OS is healthy"
    exit 0
  fi
  sleep 2
done

docker compose logs --tail=100
exit 1
