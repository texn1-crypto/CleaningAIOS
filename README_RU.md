# CleaningAI OS

Production-oriented operating system for a cleaning business. The original MVP ZIP remains in the repository; its source is now unpacked into a normal, testable project structure.

## What works

- FastAPI Mission Control with PostgreSQL/SQLite, health and readiness checks;
- public minimalist cleaning-company website with responsive layout, SEO endpoints, original ImageGen visuals and a privacy-aware lead form;
- shared records for tenders, leads/CRM, candidates, campaigns, finance and source data;
- linked business graph: clients, sites, contracts, employees, shifts, vacancies and complaints;
- persistent orchestrator queue and autonomous worker/scheduler loops;
- transactional Event Bus/outbox with idempotency, retries and dead-letter state;
- versioned Company Brain facts with confidence, source and expiry;
- deterministic Decision Engine, separate owner Approval Engine and auditable Agent Runtime runs;
- automatic event routing from Sales, Tenders, HR, Finance and Marketing records to their agents;
- AI CEO, Research, Tender, Sales, Marketing, HR, Finance and Meta Brain agents;
- optional OpenAI-compatible Responses API advisor with strict JSON output, aggregate-only input and deterministic fallback;
- business goals/KPI progress, structured decisions, outcome measurement and three-scenario simulator;
- tender opportunity scoring and document registry with structured analysis;
- configurable HTTP JSON-feed tender collection and size-limited document download with SHA-256 verification;
- owner-approval gate for tender submissions, contracts, legal/financial commitments, final HR decisions and bulk outreach tasks;
- key-derived production RBAC, immutable audit events, decisions, tasks and agent heartbeats;
- CSV/XLSX lead import and outreach queue with multiple mailboxes, templates, real attachments, delivery journal, bounce/complaint handling, suppression, unsubscribe, campaign/recipient deduplication, approvals, minute/day limits and SMTP delivery;
- unified inbound message inbox, content plan, staffing/reserve view, vacancy Telegram drafts, payment calendar and complaint/SLA control;
- public lead capture into CRM/inbox with consent, abuse protection, UTM attribution, deterministic hot-lead scoring, Sales tasks and owner-email notifications;
- Russian marketing-provider registry, trackable hypotheses/experiments, manual external campaign binding, media queue and evidence-backed attribution analytics;
- masked company requisites and marketing invoices routed to Telegram owner approval with no automatic payment;
- least-privilege AI provider routing for reasoning, product improvements, images and video, with truthful credential/adapter states;
- backward-compatible Telegram commands plus Mission Control sections;
- natural-language Russian Telegram dialogue that maps ordinary phrases to read views or auditable agent tasks, without requiring slash commands;
- Request Analyst Agent that records real capability gaps and prepares a redacted Codex prompt with acceptance criteria and a mandatory test plan;
- Alembic migration, Docker Compose, CI, tests and rollback guide.

Agents produce operational analysis from the shared database. The core remains deterministic without an LLM. When `LLM_API_KEY`, `LLM_BASE_URL` and `LLM_MODEL` are configured, AI CEO also requests a structured advisory review and may create only analysis/planning tasks. LLM recommendations that declare an owner decision are not queued, and the policy layer remains authoritative for every protected action. Tender collection similarly reports missing sources instead of inventing results.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn app.main:app --reload
```

Open `http://localhost:8000/docs`. In development role headers are available for tests. In production set a strong `API_KEY` and pass `X-API-Key`; the role is derived from that key and cannot be raised with a header.

## Required production secrets

- `POSTGRES_PASSWORD`, `API_KEY`; optionally separate `MANAGER_API_KEY`, `OPERATOR_API_KEY`, `VIEWER_API_KEY`
- `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID` for Telegram
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` for delivery
- `UNSUBSCRIBE_SECRET` for independently rotatable signed unsubscribe links (falls back to `API_KEY`)
- `LLM_API_KEY`; optionally override `LLM_BASE_URL` and `LLM_MODEL` (default: OpenAI Responses API and `gpt-5.6-terra`)
- optional `WORKSPACE_AGENT_TRIGGER_ID` and `WORKSPACE_AGENT_ACCESS_TOKEN` for server-side handoff to a published ChatGPT Workspace Agent
- `TENDER_SOURCES` and per-source credentials/API keys for external tender ingestion
- `COMPANY_LEGAL_NAME`, `COMPANY_INN`, company contacts/address and `PRIVACY_CONTACT_EMAIL` before enabling the public production lead form
- `OWNER_NOTIFICATION_EMAIL` plus SMTP for hot-lead alerts; optional Russian advertising account credentials listed in `.env.example`

See [production deployment](docs/PRODUCTION.md) and [baseline failures](docs/BASELINE.md).

## CleaningAI OS 2.0 flow

Domain changes and their events are committed together. The worker publishes pending
events and creates tasks for the responsible domain agent. Before an agent runs, the
Decision Engine checks policy. Tender submission, legal/contract/financial actions,
final HR decisions and bulk outreach are blocked until the owner decides a dedicated
approval request. Every agent execution is recorded in `agent_runs`; durable company
facts live in `company_knowledge` instead of prompts or chat history.

The operational APIs include `/api/events`, `/api/brain`, `/api/agent-runs`,
`/api/approvals`, `/api/entities`, `/api/company/graph`, `/api/goals`,
`/api/finance/site-economics`, `/api/simulations`, `/api/tenders/{id}/score`,
`/api/imports/leads`, `/api/inbox`, `/api/hr/staffing`, `/api/finance/payment-calendar`,
`/api/marketing/content`, `/api/operations/quality` and
`/api/outreach/campaigns/launch`. Apply migrations through `0006` before deploying.

Domain records now have guarded lifecycle transitions through `PATCH /api/records/{id}`.
CRM touches are stored through `/api/records/{id}/contacts`, and
`/api/modules/summary` returns one operational snapshot for Sales, Tenders, HR,
Finance and Marketing. Domain rules reject incomplete finance entries, active
tenders without deadlines and lost leads without a recorded reason.

Open `/` for the public company website, `/mission-control` for the internal dashboard and `/docs` for all operations.
`/api/integrations` truthfully reports which external credentials or source adapters
are still missing. External tender portals are never reported as active until their
source is configured; the LLM is never reported as configured without its key and
model. The deterministic operating system works without either integration.

See [architecture](docs/ARCHITECTURE.md) for the event flow and module boundaries.
See [website and Marketing OS](docs/MARKETING_SITE.md) for the lead, media, advertising-platform, invoice and credential flows.

## Общение с Telegram-ботом

Владелец может писать боту обычным русским текстом: «покажи задачи», «что с
тендерами», «найди тендеры по уборке БЦ», «создай задачу связаться с клиентом» или
«проанализируй финансы». Чтение данных выполняется сразу, а деловые поручения
превращаются в задачи подходящего агента. Неизвестные поручения передаются
Orchestrator. Оплата, договоры, подача заявки на тендер, окончательные кадровые
решения и массовые рассылки сохраняют обязательное подтверждение владельца.

## Request Analyst и контур улучшений

Каждая обычная фраза владельца дополнительно анализируется агентом
`request_analyst`. Запросы, которые уже поддерживаются, продолжают выполняться
обычным маршрутом. Отсутствующие credentials отмечаются как необходимость
настройки, а не как новая функция. Если бот может только сохранить поручение, но
не способен гарантировать реальный результат, создаётся запись в
`improvement_requests` со следующими полями:

- исходный текст с удалёнными токенами и паролями;
- причина неполной поддержки и недостающие возможности;
- готовый промт для Codex;
- критерии приёмки и обязательный тест-план;
- состояние handoff, реализации и тестовые доказательства.

Очередь доступна через `GET /api/improvements`, а в Telegram — кнопкой
«🛠 Улучшения» или фразой «покажи улучшения». Повтор одинакового запроса не
создаёт дубликат, а увеличивает его счётчик. Локальный Codex может периодически
забирать очередь; для полностью серверной передачи можно опционально подключить
опубликованный ChatGPT Workspace Agent. Без его credentials система честно
показывает `credentials_required` и сохраняет готовый handoff в PostgreSQL.
