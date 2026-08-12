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
curl --fail -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/api/integrations
```

Do not expose PostgreSQL publicly. Terminate TLS in a reverse proxy and restrict `/docs` in production if it is not needed.

`API_KEY` always grants the `owner` role. Configure `MANAGER_API_KEY`,
`OPERATOR_API_KEY`, and `VIEWER_API_KEY` for lower-privilege clients. In production,
the server derives the role from the matching key and ignores `X-Role`; this prevents
clients from raising their own privileges. Give each secret only to its intended
operator and rotate it if it is exposed.

Set `UNSUBSCRIBE_SECRET` to a separate random value to sign unsubscribe links. If it
is omitted, `API_KEY` is used as the signing secret. Changing the effective secret
invalidates links that have already been sent.

Set `LLM_API_KEY` to enable the advisory AI CEO review. The defaults use
`https://api.openai.com/v1`, model `gpt-5.6-terra`, low reasoning effort and
`store=false`; override `LLM_BASE_URL`, `LLM_MODEL`, `LLM_REASONING_EFFORT`,
`LLM_TIMEOUT_SECONDS` or `LLM_MAX_OUTPUT_TOKENS` when needed. Production rejects an
unencrypted HTTP LLM endpoint. The application works deterministically when the key
is absent or the provider is unavailable.

Optionally set `WORKSPACE_AGENT_TRIGGER_ID` and `WORKSPACE_AGENT_ACCESS_TOKEN`
to hand capability gaps to a published ChatGPT Workspace Agent API channel. Without
them the Request Analyst still stores a complete Codex prompt, acceptance criteria
and test plan in PostgreSQL and reports `credentials_required`.

For every additional sender mailbox, set `secret_ref` to the name of an environment
variable mounted into `web` and `worker` (for example `SMTP_SALES_PASSWORD`). Do not
store SMTP passwords in API payloads or the database.

## Backup

```bash
docker compose exec -T db pg_dump -U cleaningai -Fc cleaningai > cleaningai.dump
```

## Rollback

Tag every deployed image. To roll application code back, set the previous image tag in Compose, run `docker compose up -d`, and verify `/health`. Database downgrades are intentionally manual: back up first, inspect the migration, then run `docker compose run --rm migrate alembic downgrade <revision>`. For a destructive/schema-incompatible incident, restore the matching `pg_dump` into a fresh PostgreSQL volume.
