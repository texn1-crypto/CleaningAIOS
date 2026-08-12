# CleaningAIOS capability audit

Audit date: 2026-08-13. Repository: `texn1-crypto/CleaningAIOS`. Branch at audit:
`agent/cleaningai-os-production`; baseline commit: `48f4a16`.

## Runtime map

- Stack: Python 3.12 in production, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16,
  python-telegram-bot, a polling worker and scheduler, Docker Compose.
- Entry points: `app.main:app`, `python -m app.worker`, `python -m app.scheduler`,
  and `python -m app.bot`.
- Persistence: shared relational models in `app/models.py`; migrations `0001` through
  `0009`; SQLite is restricted to development/test.
- Core execution: `app/platform.py` (Event Bus, Company Brain, approvals and Agent
  Runtime) and `app/orchestrator.py` (policy, dispatch, retry and evidence gate).
- Interfaces: legacy `/api/*`, versioned `/api/v2/*`, public site/API, Mission Control,
  and Telegram calling the same protected HTTP API.
- Delivery: `.github/workflows/ci.yml` runs pytest/coverage, Compose config, container
  build, PostgreSQL migrations and HTTP smoke checks. It does not yet run a formatter,
  linter, type checker, dependency scanner or image scanner.

## Baseline evidence

Clean `HEAD` was exported to a temporary directory and tested with the repository
virtual environment:

```text
pytest -q
66 passed, 1 failed
```

The failing test was Telegram application construction after an earlier
`asyncio.run()` had closed the process event loop. The current working change creates
a loop when absent and has a regression test. No production deployment was performed.

## Capability matrix

| Area | Status | Evidence and limitation |
|---|---|---|
| FastAPI/PostgreSQL/Alembic | IMPLEMENTED | Real API, DB session, migrations and Compose health checks are wired. |
| RBAC and secret validation | IMPLEMENTED | Production derives roles from configured API keys and rejects default owner key. API keys are still static credentials, not a full identity provider. |
| Audit trail | PARTIAL | Durable audit rows exist for material API/runtime actions; database-level immutability/retention policy is not enforced. |
| Event Bus/outbox | PARTIAL | Persisted versioned envelopes, event/correlation/causation IDs, actor, retry/backoff, dead-letter state and durable idempotent consumer receipts are connected to the worker. Business-specific payload schemas and latency metrics are still missing. |
| Agent Runtime | PARTIAL | Registry, run history, evidence, cost, retries and policy gate are connected. Capability/tool allowlists, cancellation, enforced timeout, confidence schema and replay API are missing. |
| Task/workflow engine | PARTIAL | Persistent tasks, priorities, retry/backoff, schedule and owner manual steps work. Dependencies, explicit transition history, compensation, cancellation and recurring workflow definitions are missing. |
| Approval Engine | PARTIAL | Bound approvals and Telegram decisions protect listed critical actions. Expiry, request-changes, risk/amount policy and database-level single-execution command keys are missing. |
| Decision Engine | PARTIAL | Structured decisions, approval link and measured outcomes exist. Provenance/freshness and a complete enforced recommendation schema are incomplete. |
| Company Brain | PARTIAL | Versioned key/value knowledge with provenance/confidence/expiry exists. Document ingestion, ACL-aware chunks, embeddings/hybrid retrieval, citations and retrieval evals are missing; this is not RAG yet. |
| AI CEO and reports | PARTIAL | Deterministic review, optional aggregate-only LLM advice, task health and activity reports are wired. A cross-module daily/weekly CEO Brief with freshness and source links is missing. |
| CRM/Sales | PARTIAL | Leads, lifecycle rules, contacts, pipeline summary, loss reasons and CRM-backed proposal PDF work. Forecasting and full company/contact deduplication are incomplete. |
| Tender Intelligence | PARTIAL | Legal JSON-feed boundary, deduplication, documents, scoring, deadlines and submission approval exist. Portal adapters, requirement extraction, document checklist and learned result scoring require sources/implementation. |
| Finance | PARTIAL | Records, payment calendar, object economics and approval policy exist. Money uses `float` in several models/calculations; invoices, AR/AP, taxes and deterministic Decimal/minor-unit accounting are incomplete. |
| HR | PARTIAL | Candidates, employees, vacancies, shifts and final-decision approval exist. Document expiry, onboarding, privacy ACL and performance workflows are incomplete. |
| Operations/Quality | PARTIAL | Company graph, shifts, complaints, SLA view and basic quality summary exist. Check-in/out, materials, inspections, evidence, corrective/reinspection workflows and root-cause analytics are incomplete. |
| Marketing/outreach | PARTIAL | Content, attribution, experiments, mailboxes, SMTP, suppression, unsubscribe, deduplication and rate limits are real. Advertising platform executors remain explicitly manual/adapter-required. |
| Simulation/Digital Twin | PARTIAL | Deterministic three-scenario site economics exists. Versioned input snapshots, persisted scenarios and capacity/SLA/quality effects are missing. |
| AI Evolution Council | PARTIAL | Improvements, deduplication, test plans, evidence and optional handoff exist. Eval datasets, prompt/policy versions, staged rollout and rollback workflow are missing. |
| Telegram Control Center | PARTIAL | Owner allowlist, navigation, natural Russian text, approvals and API-backed tasks work. Multi-user bindings, pagination, Company Brain search and critical alert delivery are incomplete. |
| Proactive automation | PARTIAL | Scheduler detects tender deadlines, overdue payments and unfilled shifts with deduplication. Configurable cooldown/severity/recipients and the broader trigger catalog are missing. |
| Observability | PARTIAL | Health/readiness, task/agent state and logs exist. Structured JSON logs, metrics endpoint, tracing, SLOs, dashboards and alerts are missing. |
| CI/CD | PARTIAL | Tests, coverage, Compose build, migration and HTTP smoke are enforced. Lockfile, lint, type check, security scans and deployment environment gates are missing. |
| Procurement | MISSING | No connected procurement domain/workflow was found. |
| Production restore drill | UNVERIFIED | Backup/rollback commands are documented, but a restore verification result is not stored. |

No production path was classified as IMPLEMENTED solely from a class or filename;
each implemented row above has a connected runtime, persistence and/or tested API
entry point. External provider readiness states are not treated as integrations.

## Next P0 slices

Completed in migration `0008`: versioned event envelope, correlation/causation/actor
and durable idempotent consumer receipts.

1. Add explicit task state transitions/dependencies and an immutable transition log.
2. Enforce agent capabilities, timeout/cancellation and replay diagnostics.
3. Replace financial floats with integer minor units or Decimal under a safe migration.
4. Add lint, type checking and dependency/container scanning to CI.
