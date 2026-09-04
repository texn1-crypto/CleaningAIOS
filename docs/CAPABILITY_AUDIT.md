# CleaningAIOS capability audit

Audit date: 2026-08-13. Repository: `texn1-crypto/CleaningAIOS`. Branch at audit:
`agent/cleaningai-os-production`; baseline commit: `48f4a16`.

## Runtime map

- Stack: Python 3.12 in production, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16,
  python-telegram-bot, a polling worker and scheduler, Docker Compose.
- Entry points: `app.main:app`, `python -m app.worker`, `python -m app.scheduler`,
  and `python -m app.bot`.
- Persistence: shared relational models in `app/models.py`; migrations `0001` through
  `0010`; SQLite is restricted to development/test.
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
| Agent Runtime | PARTIAL | Registry, run history, evidence, cost, retries, policy gate, safe replay and a default-deny read-only tool gateway are connected. Local/optional stateless MCP tools have agent allowlists, call/time/result-size budgets, PII/credential screening and separate audit records. General cancellation, full-agent execution timeout and confidence schema are still missing. |
| Task/workflow engine | PARTIAL | Persistent tasks, guarded state transitions, immutable transition history, priorities, retry/backoff, schedule and owner manual steps work. Dependencies, compensation, cancellation and recurring workflow definitions are missing. |
| Approval Engine | PARTIAL | Bound approvals protect listed critical actions. Expiry, request-changes, signed Telegram decisions, immutable terminal records and exactly-once task resume are implemented. Read-only agent tools are separately default-deny and budgeted; a unified policy for future write-capable tools remains intentionally absent. |
| Decision Engine | PARTIAL | Structured decisions, approval links and measured outcomes exist. Orchestrator-to-agent routes now have idempotent PII-free decision metadata, expected-result risk, automatic terminal outcome measurement, manager inspection and audit coverage. General recommendation provenance/freshness remains incomplete. |
| Company Brain | PARTIAL | Legacy versioned key/value knowledge remains available. Append-only documents now have provenance, checksums, source versions, ACL-aware immutable chunks, expiry, latest-version deterministic hybrid lexical/character retrieval, exact citations and retrieval regressions. Semantic embeddings, attachment parsing and an approved external ingestion pipeline are intentionally not claimed. |
| AI CEO and reports | PARTIAL | Deterministic review, optional aggregate-only LLM advice, task health and activity reports are wired. A cross-module daily/weekly CEO Brief with freshness and source links is missing. |
| CRM/Sales | PARTIAL | Leads, lifecycle rules, contacts, pipeline summary, loss reasons, CRM-backed proposal PDF and a consent-first public Telegram lead wizard with deterministic qualification/follow-up tasks work. Forecasting and full cross-channel company/contact deduplication are incomplete. |
| Tender Intelligence | PARTIAL | Legal JSON-feed boundary, deduplication, SPb/LO scope filter, evidence-bound economics, cash-gap/legal stop factors, separate participation/submission approvals and document checklist exist. Official portal adapters, automatic requirement extraction and learned result scoring still require sources/implementation. |
| Finance | PARTIAL | Records, payment calendar, object economics and approval policy exist. Money uses `float` in several models/calculations; invoices, AR/AP, taxes and deterministic Decimal/minor-unit accounting are incomplete. |
| HR | PARTIAL | Candidates, employees, vacancies, shifts and final-decision approval exist. Document expiry, onboarding, privacy ACL and performance workflows are incomplete. |
| Operations/Quality | PARTIAL | Company graph, shifts, complaints, SLA view and basic quality summary exist. Check-in/out, materials, inspections, evidence, corrective/reinspection workflows and root-cause analytics are incomplete. |
| Marketing/outreach | PARTIAL | Content, attribution, experiments, mailboxes, SMTP, suppression, unsubscribe, deduplication and rate limits are real. Advertising platform executors remain explicitly manual/adapter-required. |
| Simulation/Digital Twin | PARTIAL | Deterministic three-scenario site economics exists. Versioned input snapshots, persisted scenarios and capacity/SLA/quality effects are missing. |
| AI Evolution Council | PARTIAL | Improvements, deduplication, test plans, evidence, optional handoff, a fixed golden eval dataset and immutable prompt versions are connected. Deterministic stable/candidate cohorts default off and support immediate configuration rollback; automated promotion based on longitudinal outcome evidence is still missing. |
| Telegram Control Center | PARTIAL | Exact user/chat RBAC bindings, pseudonymous denial audit, natural Russian text, assigned/due paginated task views, workflow correlation, signed durable approvals, acknowledged critical alerts and a source-linked CEO Brief work. Company Brain search is available through the protected API/tool gateway but has no dedicated Telegram ingestion wizard yet. |
| Proactive automation | PARTIAL | Scheduler detects tender deadlines, overdue payments and unfilled shifts with deduplication. Selected high/critical events have correlated retrying Telegram alerts, dead-letter state, acknowledgement and metrics. Configurable cooldown/recipient groups and a broader trigger catalog remain missing. |
| Observability | PARTIAL | Health/readiness, task/agent state, structured redacted JSON logs with HTTP correlation IDs, an authenticated Prometheus metrics endpoint and explicit agent success/latency/staleness SLOs are connected. Distributed tracing, dashboards and SLO alerts are still missing. |
| CI/CD | PARTIAL | Tests, coverage, golden agent evals, Ruff, targeted strict mypy, dependency audit, CodeQL, Compose build, migrations and HTTP smoke are enforced. A fully resolved lockfile, container-image scanner and deployment environment approval gate are still missing. |
| Procurement | MISSING | No connected procurement domain/workflow was found. |
| Production restore drill | UNVERIFIED | Backup/rollback commands are documented, but a restore verification result is not stored. |

No production path was classified as IMPLEMENTED solely from a class or filename;
each implemented row above has a connected runtime, persistence and/or tested API
entry point. External provider readiness states are not treated as integrations.

## Next P0 slices

Completed in migration `0008`: versioned event envelope, correlation/causation/actor
and durable idempotent consumer receipts.

Completed in migration `0010`: guarded task states and immutable transition history.

1. Add workflow dependencies, cancellation and compensation semantics.
2. Enforce agent capabilities, timeout/cancellation and replay diagnostics.
3. Replace financial floats with integer minor units or Decimal under a safe migration.
4. Add lint, type checking and dependency/container scanning to CI.
