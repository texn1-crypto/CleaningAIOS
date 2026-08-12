# CleaningAI OS

Production-oriented operating system for a cleaning business. The original MVP ZIP remains in the repository; its source is now unpacked into a normal, testable project structure.

## What works

- FastAPI Mission Control with PostgreSQL/SQLite, health and readiness checks;
- shared records for tenders, leads/CRM, candidates, campaigns, finance and source data;
- persistent orchestrator queue and autonomous worker/scheduler loops;
- AI CEO, Research, Tender, Sales, Marketing, HR, Finance and Meta Brain deterministic agents;
- owner-approval gate for tender submissions, contracts, legal/financial commitments, final HR decisions and bulk outreach tasks;
- RBAC headers/API key, immutable audit events, decisions, tasks and agent heartbeats;
- outreach queue with suppression, unsubscribe, campaign/recipient deduplication, minute/day limits and SMTP delivery;
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

Open `http://localhost:8000/docs`. In development the default API key is accepted; in production set a strong `API_KEY` and pass `X-API-Key`, `X-Actor`, and `X-Role` headers.

## Required production secrets

- `POSTGRES_PASSWORD`, `API_KEY`
- `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID` for Telegram
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` for delivery
- `LLM_API_KEY` only when a chosen LLM adapter is added
- `TENDER_SOURCES` and per-source credentials/API keys for external tender ingestion

See [production deployment](docs/PRODUCTION.md) and [baseline failures](docs/BASELINE.md).
