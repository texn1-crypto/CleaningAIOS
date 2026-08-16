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

`SYSTEM_ADMIN_INTERVAL_MINUTES=5` enables the structured system-administrator
agent. Each cycle correlates failed/blocked/stale tasks, outbound-email failures,
owner-notification transport failures, agent state, Request Analyst traces,
audit records and deduplicated improvement handoffs. The agent records whether
an incident is new, still not fixed, repeated, or cleared by a later check. It
never replays an outreach campaign or another protected business action. Use
`SYSTEM_ADMIN_STALE_TASK_MINUTES=15` to define when a running task becomes an
incident, and `/sysadmin` in Telegram to request the latest live check.

Selected task-failure, agent-incident, overdue-payment, near-tender-deadline,
outreach-complaint and task-approval events become deduplicated high/critical
Telegram alerts. Transport failures retry with backoff and become `dead_letter`
after five attempts. The acknowledgement button records receipt only; it does not
approve or execute the underlying action. Monitor
`GET /api/owner-notifications/metrics` and investigate any dead-letter count.

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

Set `OWNER_TELEGRAM_CHAT_ID` to the exact private chat authorized for the configured
`OWNER_TELEGRAM_ID` (normally both values are equal). Set
`TELEGRAM_CALLBACK_SECRET` to an independent random secret of at least 32 bytes; the
server refuses production startup when it is missing. It signs short-lived approval
callbacks and must be mounted into both `web`, `worker`, and `bot` without being
printed in logs. Additional Telegram identities are bound by an owner through
`PUT /api/telegram/control/identities`; do not grant `owner` merely to make a command
work. The current runtime uses polling only and exposes no Telegram webhook route.

Set `UNSUBSCRIBE_SECRET` to a separate random value to sign unsubscribe links. If it
is omitted, `API_KEY` is used as the signing secret. Changing the effective secret
invalidates links that have already been sent.

`OUTREACH_PER_DAY` limits messages in one local delivery window rather than a
rolling 24-hour period. `OUTREACH_TIMEZONE=Europe/Moscow` and
`OUTREACH_DAILY_START_HOUR=9` open a fresh window at 09:00 Moscow time; before that
hour delivery remains paused. After every accepted SMTP message the worker queues a
deduplicated Telegram progress update such as `1/50`, plus the cumulative progress
for the approved recipient file. Suppression, unsubscribe, consent and exact owner
approval checks remain mandatory.

SMTP authentication failures and explicit provider blocks put the affected transport
(`default` or one `SenderMailbox`) into a persistent database quarantine. Queued
messages remain intact and no further login or delivery attempt is made until the
owner fixes the mailbox and presses `Повторно проверить почту` in `/outreach` (or
calls `POST /api/outreach/mailboxes/{mailbox_key}/resume`). A transient SMTP 421/454
or quota response uses a separate automatic cooldown, 24 hours by default through
`OUTREACH_RATE_LIMIT_COOLDOWN_HOURS`; it never rotates to another mailbox to evade a
provider restriction. The resume endpoint only requeues existing approved messages
and does not bypass consent, suppression, unsubscribe or daily limits.

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

Set `IMAGE_GENERATION_API_KEY` and explicitly opt in with
`IMAGE_GENERATION_ENABLED=true` to activate `/image` and natural-language image
requests in Telegram. `IMAGE_GENERATION_MODEL` defaults to `gpt-image-2`, while
`IMAGE_GENERATION_SIZE`, `IMAGE_GENERATION_QUALITY` and
`IMAGE_GENERATION_TIMEOUT_SECONDS` control the bounded provider request. The worker
validates the returned base64, PNG/JPEG signature, file-size limit and SHA-256 before
delivering the file through Telegram. It never publishes a direct result. Set
`SOCIAL_IMAGE_GENERATION_ENABLED=true` separately to use paid generation for daily
social visuals; otherwise those plans continue with the original local media pool.
Store the key only in the production secret store or `.env`, never in a bot message,
database field, log or prompt. A 401/403 becomes a terminal `credentials_required`
job until configuration is fixed and the request is explicitly requeued, preventing
an accidental paid or hot retry loop.

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
For a Mail.ru mailbox, create a dedicated external-application password in Mail.ru
and run `python scripts/set_mail_password.py` locally. The command accepts the value
with hidden input and writes it to both SMTP and IMAP variables without printing it;
never paste that password into chat, source control or an API request.
For reply collection, also set `imap_host`, `imap_username`, `imap_secret_ref` and
`inbound_enabled=true`, then mount the referenced IMAP secret into `web` and `worker`.
Set `OWNER_NOTIFICATION_EMAIL` to the primary inbox. Inbound replies are forwarded
through the originating mailbox when its SMTP secret is ready, so a separate default
SMTP account is optional; the default SMTP remains the fallback transport. SMTP port
465 uses implicit TLS and other configured ports use STARTTLS. The Telegram outreach
panel reports receiving and forwarding readiness without returning any secret values.

The `/mailing` Telegram wizard accepts the recipient registry first, then consent
evidence, subject and body. Its preview can include one PDF, Word, Excel, ODT, CSV,
TXT, PNG or JPG attachment up to `MAX_ATTACHMENT_BYTES`. The attachment is stored on
the shared protected document volume, its checksum is bound to the owner approval,
and the worker adds it to every resulting MIME message only after that exact campaign
is approved. Do not put recipient data or SMTP credentials inside the attachment.

## Backup

```bash
docker compose exec -T db pg_dump -U cleaningai -Fc cleaningai > cleaningai.dump
```

## Rollback

Tag every deployed image. To roll application code back, set the previous image tag in Compose, run `docker compose up -d`, and verify `/health`. Database downgrades are intentionally manual: back up first, inspect the migration, then run `docker compose run --rm migrate alembic downgrade <revision>`. For a destructive/schema-incompatible incident, restore the matching `pg_dump` into a fresh PostgreSQL volume.
