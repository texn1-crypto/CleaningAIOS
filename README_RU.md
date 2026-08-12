# CleaningAI OS

Production-oriented operating system for a cleaning business. The original MVP ZIP remains in the repository; its source is now unpacked into a normal, testable project structure.

## What works

- FastAPI Mission Control with PostgreSQL/SQLite, health and readiness checks;
- shared records for tenders, leads/CRM, candidates, campaigns, finance and source data;
- linked business graph: clients, sites, contracts, employees, shifts, vacancies and complaints;
- persistent orchestrator queue and autonomous worker/scheduler loops;
- transactional Event Bus/outbox with idempotency, retries and dead-letter state;
- versioned Company Brain facts with confidence, source and expiry;
- deterministic Decision Engine, separate owner Approval Engine and auditable Agent Runtime runs;
- automatic event routing from Sales, Tenders, HR, Finance and Marketing records to their agents;
- AI CEO, Research, Tender, Sales, Marketing, HR, Finance and Meta Brain deterministic agents;
- business goals/KPI progress, structured decisions, outcome measurement and three-scenario simulator;
- tender opportunity scoring and document registry with structured analysis;
- configurable HTTP JSON-feed tender collection and size-limited document download with SHA-256 verification;
- owner-approval gate for tender submissions, contracts, legal/financial commitments, final HR decisions and bulk outreach tasks;
- key-derived production RBAC, immutable audit events, decisions, tasks and agent heartbeats;
- CSV/XLSX lead import and outreach queue with multiple mailboxes, templates, real attachments, delivery journal, bounce/complaint handling, suppression, unsubscribe, campaign/recipient deduplication, approvals, minute/day limits and SMTP delivery;
- unified inbound message inbox, content plan, staffing/reserve view, vacancy Telegram drafts, payment calendar and complaint/SLA control;
- backward-compatible Telegram commands plus Mission Control sections;
- Alembic migration, Docker Compose, CI, tests and rollback guide.

Agents produce deterministic operational analysis from the shared database. An LLM is optional and is not claimed as active until `LLM_API_KEY` and a provider integration are configured. Tender collection similarly reports missing sources instead of inventing results.

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
- `LLM_API_KEY` only when a chosen LLM adapter is added
- `TENDER_SOURCES` and per-source credentials/API keys for external tender ingestion

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
`/api/outreach/campaigns/launch`. Apply migrations through `0004` before deploying.

Domain records now have guarded lifecycle transitions through `PATCH /api/records/{id}`.
CRM touches are stored through `/api/records/{id}/contacts`, and
`/api/modules/summary` returns one operational snapshot for Sales, Tenders, HR,
Finance and Marketing. Domain rules reject incomplete finance entries, active
tenders without deadlines and lost leads without a recorded reason.

Open `/` for the live Mission Control dashboard and `/docs` for all operations.
`/api/integrations` truthfully reports which external credentials or adapters are
still missing. External tender portals and an LLM are never reported as active until
their source adapter is configured; the deterministic operating system works without
them.

See [architecture](docs/ARCHITECTURE.md) for the event flow and module boundaries.
