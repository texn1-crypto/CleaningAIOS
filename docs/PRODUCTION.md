# Production deployment and rollback

## Prerequisites

- Linux host with Docker Engine and Compose v2
- DNS/TLS reverse proxy for `PUBLIC_BASE_URL`
- PostgreSQL volume backup destination

## Deploy

```bash
cp .env.example .env
# fill every required secret
docker compose pull
docker compose build
docker compose up -d db
docker compose run --rm migrate
docker compose up -d web worker scheduler
docker compose --profile telegram up -d bot
docker compose ps
curl --fail http://127.0.0.1:8000/health
```

Do not expose PostgreSQL publicly. Terminate TLS in a reverse proxy and restrict `/docs` in production if it is not needed.

## Backup

```bash
docker compose exec -T db pg_dump -U cleaningai -Fc cleaningai > cleaningai.dump
```

## Rollback

Tag every deployed image. To roll application code back, set the previous image tag in Compose, run `docker compose up -d`, and verify `/health`. Database downgrades are intentionally manual: back up first, inspect the migration, then run `docker compose run --rm migrate alembic downgrade <revision>`. For a destructive/schema-incompatible incident, restore the matching `pg_dump` into a fresh PostgreSQL volume.
