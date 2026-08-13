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
curl --fail http://127.0.0.1:8000/
curl --fail -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/api/integrations
```

On Yandex Cloud, if the standard Debian CDN is unreachable from the selected
availability zone, build against Yandex's regional Debian mirror instead:

```bash
docker compose build --build-arg DEBIAN_MIRROR=mirror.yandex.ru
```

The default remains `deb.debian.org`; the mirror override only affects the image
build and is not stored in the runtime environment.

The Telegram bot and worker services support `TELEGRAM_API_IP` as a routing
override for cloud providers that cannot reach Telegram's DNS-selected address.
The example default is a TLS-verified Telegram API endpoint. The worker needs the
same route because it delivers persisted owner notifications. If startup or
notification logs show `Network is unreachable`, verify a replacement without
including the bot token:

```bash
curl --resolve api.telegram.org:443:REPLACEMENT_IP https://api.telegram.org/
```

Set `TELEGRAM_API_IP` in the server `.env`, then recreate the bot and worker:

```bash
docker compose --profile telegram up -d --force-recreate bot worker
```

`OWNER_ACTIVITY_REPORT_INTERVAL_MINUTES=30` makes the scheduler create one
idempotent operational-report task per 30-minute window. The Orchestrator builds
the report from PostgreSQL and the worker delivers it through the persisted owner
notification queue. Set a value from 5 to 1440 minutes; delivery requires
`TELEGRAM_BOT_TOKEN` and `OWNER_TELEGRAM_ID`.

`CEO_DEVELOPMENT_CADENCE_HOURS=24` keeps a finite, deduplicated backlog for
website growth, sales, marketing channels and system quality. These recurring
tasks only analyse data and prepare recommendations. Publication, outreach,
spending and contractual actions still require the existing owner approvals.

Telegram's cloud Bot API cannot download files larger than 20 MB. For larger
commercial-proposal attachments, operate the official local Bot API server
separately, provide `api_id` and `api_hash` only to that server, expose it on the
private Docker network, and set `TELEGRAM_BOT_API_BASE_URL` to its root URL (for
example `http://telegram-bot-api:8081`). Until this is configured, the application
stores the request as blocked with an improvement and CEO incident report. Never
send `api_id`, `api_hash`, or bot tokens through Telegram.

Do not expose PostgreSQL publicly. Terminate TLS in a reverse proxy and restrict `/docs` in production if it is not needed.

`API_KEY` always grants the `owner` role. Configure `MANAGER_API_KEY`,
`OPERATOR_API_KEY`, and `VIEWER_API_KEY` for lower-privilege clients. In production,
the server derives the role from the matching key and ignores `X-Role`; this prevents
clients from raising their own privileges. Give each secret only to its intended
operator and rotate it if it is exposed.

Set `UNSUBSCRIBE_SECRET` to a separate random value to sign unsubscribe links. If it
is omitted, `API_KEY` is used as the signing secret. Changing the effective secret
invalidates links that have already been sent.

Set `LLM_API_KEY` for OpenAI and/or `ANTHROPIC_API_KEY` for Claude. The default
`LLM_PROVIDER=auto` assigns aggregate CEO/business synthesis to Claude, request and
product capability analysis to OpenAI, and falls back to the other configured
provider on a transient provider error. Pin `LLM_PROVIDER=openai` or `anthropic` if
needed. OpenAI defaults to `https://api.openai.com/v1` and `gpt-5.6-terra`; Claude
uses the native `https://api.anthropic.com/v1/messages` contract and defaults to
`claude-sonnet-4-6`. Override the corresponding base URL, model and timeout settings
only for a reviewed provider or gateway. Production rejects unencrypted HTTP
endpoints. Neither provider receives tools, secrets, banking credentials or authority
to execute protected actions. The application remains deterministic when both keys
are absent or providers are unavailable.

Optionally set `WORKSPACE_AGENT_TRIGGER_ID` and `WORKSPACE_AGENT_ACCESS_TOKEN`
to hand capability gaps to a published ChatGPT Workspace Agent API channel. Without
them the Request Analyst still stores a complete Codex prompt, acceptance criteria
and test plan in PostgreSQL and reports `credentials_required`.

Before exposing the website, set `COMPANY_NAME`, `COMPANY_LEGAL_NAME`,
`COMPANY_INN`, `COMPANY_PHONE`, `COMPANY_EMAIL`, `COMPANY_ADDRESS`,
`COMPANY_SERVICE_AREA` and `PRIVACY_CONTACT_EMAIL`. The production lead form remains
disabled until the legal operator name and a privacy contact email exist. Set
`OWNER_NOTIFICATION_EMAIL` plus SMTP credentials to receive hot leads by email.

Russian advertising tokens (`YANDEX_DIRECT_TOKEN`, `VK_ADS_TOKEN`,
`TWOGIS_BUSINESS_TOKEN`, `AVITO_CLIENT_ID`, `AVITO_CLIENT_SECRET`,
`TELEGRAM_ADS_TOKEN`) are optional and must remain server-side. In the current safe
release they expose readiness only; actual platform campaigns are bound by manually
recording their external ID after owner approval. See `docs/MARKETING_SITE.md`.

For every additional sender mailbox, set `secret_ref` to the name of an environment
variable mounted into `web` and `worker` (for example `SMTP_SALES_PASSWORD`). Do not
store SMTP passwords in API payloads or the database.

## Backup

```bash
docker compose exec -T db pg_dump -U cleaningai -Fc cleaningai > cleaningai.dump
```

## Rollback

Tag every deployed image. To roll application code back, set the previous image tag in Compose, run `docker compose up -d`, and verify `/health`. Database downgrades are intentionally manual: back up first, inspect the migration, then run `docker compose run --rm migrate alembic downgrade <revision>`. For a destructive/schema-incompatible incident, restore the matching `pg_dump` into a fresh PostgreSQL volume.
