# CleaningAIOS capability audit

Initial audit date: 2026-08-13; updated 2026-09-21. Repository:
`texn1-crypto/CleaningAIOS`. Branch: `agent/cleaningai-os-production`.

## Runtime map

- Stack: Python 3.12 in production, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16,
  python-telegram-bot, a polling worker and scheduler, Docker Compose.
- Entry points: `app.main:app`, `python -m app.worker`, `python -m app.scheduler`,
  and `python -m app.bot`.
- Persistence: shared relational models in `app/models.py`; migrations `0001` through
  `0034`; SQLite is restricted to development/test.
- Core execution: `app/platform.py` (Event Bus, Company Brain, approvals and Agent
  Runtime) and `app/orchestrator.py` (policy, dispatch, retry and evidence gate).
- Interfaces: legacy `/api/*`, versioned `/api/v2/*`, public site/API, Mission Control,
  and Telegram calling the same protected HTTP API.
- Delivery: `.github/workflows/ci.yml` runs pytest/coverage, Ruff, targeted strict
  mypy, dependency audit, Compose config/build, PostgreSQL migrations, HTTP smoke,
  CodeQL and a blocking Trivy production-image scan with SARIF upload.

## Current verification evidence

The isolated integration branch was verified without modifying or deploying the
owner's dirty development checkout:

```text
pytest -q                         495 passed
python scripts/run_agent_evals.py 32 passed
alembic upgrade/current/check     0034 head; no drift
ruff + CI mypy targets            passed
Python 3.12 dependency audit      no known vulnerabilities; no ignored findings
```

Compose configuration and deployment/watchdog shell syntax passed. An independent
read-only Claude review was attempted twice but did not return within the bounded
limits; the working tree remained unchanged and the automated and manual reviews
continued without representing that timeout as approval.

## Capability matrix

| Area | Status | Evidence and limitation |
|---|---|---|
| FastAPI/PostgreSQL/Alembic | IMPLEMENTED | Real API, DB session, migrations and Compose health checks are wired. |
| RBAC and secret validation | IMPLEMENTED | Production derives roles from configured API keys and rejects default owner key. API keys are still static credentials, not a full identity provider. |
| Audit trail | PARTIAL | Durable audit rows exist for material API/runtime actions; database-level immutability/retention policy is not enforced. |
| Event Bus/outbox | PARTIAL | Persisted versioned envelopes, event/correlation/causation IDs, actor, retry/backoff, dead-letter state and durable idempotent consumer receipts are connected to the worker. Business-specific payload schemas and latency metrics are still missing. |
| Agent Runtime | PARTIAL | Registry, run history, evidence, cost, retries, policy gate, safe replay and a default-deny read-only tool gateway are connected. Local/optional stateless MCP tools have agent allowlists, call/time/result-size budgets, PII/credential screening and separate audit records. General cancellation, full-agent execution timeout and confidence schema are still missing. |
| Task/workflow engine | PARTIAL | Persistent tasks, guarded state transitions, immutable transition history, priorities, retry/backoff, schedule and owner manual steps work. Dependencies, compensation, cancellation and recurring workflow definitions are missing. |
| Approval Engine | PARTIAL | Bound approvals protect listed critical actions. Expiry, request-changes, signed Telegram decisions, immutable terminal records and exactly-once task resume are implemented. The scheduler now persists elapsed approvals through the same terminal decision, event and audit path without resuming protected work. A shared persisted gate checks global stop, per-tender stop and exact capability before tender approval; manager read, owner-only idempotent change and audit/outbox evidence are connected. Direct SMTP and social-provider workers share the global/capability layers. Other direct protected entry points and non-tender resource flags remain incomplete. Read-only agent tools are separately default-deny and budgeted; a unified policy for future write-capable tools remains intentionally absent. |
| Bounded autonomy | PARTIAL | Migration `0033` adds owner-created, scoped, expiring authority envelopes and append-only idempotent usage receipts for `AUTO_WITHIN_LIMIT` actions. Unknown and forbidden actions fail closed, explicit protected actions cannot be weakened, and missing provider/queue evidence blocks task completion. A consent-evidenced inbound email reply is connected through the SMTP queue and is rechecked for envelope revocation immediately before delivery. Supplier RFQ, calendar and advertising executors remain missing and are not claimed from policy authorization alone. |
| Decision Engine | PARTIAL | Structured decisions, approval links and measured outcomes exist. Orchestrator-to-agent routes now have idempotent PII-free decision metadata, expected-result risk, explicit `success`/`partial`/`fail` terminal outcomes, aggregate/per-agent success metrics, manager inspection and audit coverage. General recommendation provenance/freshness remains incomplete. |
| Company Brain | PARTIAL | Legacy versioned key/value knowledge remains available. Append-only documents now have provenance, checksums, source versions, ACL-aware immutable chunks, expiry, latest-version deterministic hybrid lexical/character retrieval, exact citations and retrieval regressions. Semantic embeddings, attachment parsing and an approved external ingestion pipeline are intentionally not claimed. |
| AI CEO and reports | PARTIAL | Deterministic review, optional aggregate-only LLM advice, task health and activity reports are wired. The hourly strategy review reconciles qualified owner handoffs from delivered report evidence, schedules a bounded CRM/Crawl4AI fallback when the goal is short, emits one daily accountable lead plan and escalates unavailable search providers without claiming success. A weekly cross-module CEO Brief joins primary-database sales, lead, contract, tender, marketing, finance, workflow and per-agent facts; carries per-source freshness, exact source links and a bounded seven-day KPI plan; and is delivered idempotently through the protected owner-notification queue. It exposes owner-review handoffs; separates current runnable work from future scheduled work; recognizes current and legacy configuration waits; separates reconciled historical failures from active recovery work; and distinguishes actionable alerts from superseded snapshots, resolved tasks and historical delivery failures without deleting audit evidence. System Admin notifies the owner only when incident state is new, changed or resolved. Longitudinal week-over-week snapshot retention and forecast accuracy remain incomplete. |
| CRM/Sales | PARTIAL | Leads, lifecycle rules, contacts, pipeline summary, loss reasons, CRM-backed proposal PDF and a consent-first public Telegram lead wizard with deterministic qualification/follow-up tasks work. A coordinator runs four source-focused public-business scouts, centralizes cited organization contacts, rejects personal/free-mail/uncited data, and immediately queues an idempotent PDF for every new or materially changed lead. Validated organization leads now enter `owner_review` immediately; the qualification cycle safely promotes legacy `researched` public-search cards, while preserving qualified, won and lost lifecycle states. The CEO fallback verifies one existing in-scope management-company website per audited Crawl4AI run and creates an `owner_review` CRM lead only after organization/domain evidence matches. Guarded Crawl4AI failures are accounted once per task, including repeated failures for the same company, and three failed checks move the candidate to manual review. A backlog-aware idempotent Sales task ranks every qualification candidate with an evidence-based score breakdown, explicit missing facts, responsible scout and next research step; new same-day batches get a material-change notification instead of being hidden behind the first daily digest. CEO now also schedules a bounded rotating queue of exact-organization public-evidence searches. The adapter accepts only allowlisted demand, scope, timing, buyer-route or economics facts whose exact HTTPS source appears in the provider citations, rejects organization mismatches, records an audit row and requeues materially enriched cards for qualification without creating outreach. `sales_ready` requires complete fit/demand/scope/timing/economics evidence and a verified, unsuppressed contact path; qualification does not create outreach. An optional audited Twenty REST projection creates or updates companies for verified lead statuses, persists the remote ID/fingerprint, reports missing credentials honestly and transmits neither contact fields nor outreach permission. CleaningAIOS remains the source of truth. Public availability is not outreach consent and no outreach is sent automatically. Forecasting and full cross-channel company/contact deduplication are incomplete. |
| Voice/telephony | PARTIAL | A carrier-neutral HTTPS adapter now queues only exact-owner-approved calls to CRM leads with recorded phone consent or a requested callback. It rechecks a separate `voice_call` capability, global stop, suppression, local hours and limits; uses idempotency, disables recording, requires AI disclosure, verifies callback HMAC and records terminal outcomes in CRM. No SIP carrier or LiveKit/Pipecat gateway credentials are configured or production-verified. |
| Tender Intelligence | PARTIAL | The generic HTTP(S) JSON-feed boundary keeps canonical append-only provider-item versions, scopes external identity to source/provider, distinguishes unchanged replay and emits one outbox event per real version; bearer authentication is HTTPS-only. Every configured source attempt has a durable final receipt with timing, HTTP/outcome counters, a secret-safe manager view and its own outbox event. A deterministic last-success SLO classifies source freshness, and System Admin deduplicates non-fresh incidents and resolves them after current success. Downloaded evidence uses checksum-addressed byte files, append-only version metadata and fail-closed replay/conflict checks. An immutable FAST DISQUALIFICATION snapshot requires a fixed hard-constraint taxonomy and exact evidence before supplier work. A separate immutable supplier quote snapshot captures typed commercial terms, freshness, eligible-prequalification binding and checksum evidence; only an exact `verified` quote can feed the decision passport. SPb/LO scope filtering, separate participation/submission approvals and an isolated per-tender kill switch are connected. Official portal adapters/cursors and acknowledgements, automated Company Digital Twin evidence, malware/archive sandbox, supplier discovery/RFQ and learned scoring remain unimplemented. See `docs/tender-autopilot/`. |
| Finance | PARTIAL | Records, payment calendar, object economics and approval policy exist. Money uses `float` in several models/calculations; invoices, AR/AP, taxes and deterministic Decimal/minor-unit accounting are incomplete. |
| HR | PARTIAL | Candidates, employees, vacancies, shifts and final-decision approval exist. Document expiry, onboarding, privacy ACL and performance workflows are incomplete. |
| Operations/Quality | PARTIAL | Company graph, shifts, complaints, SLA view and basic quality summary exist. Check-in/out, materials, inspections, evidence, corrective/reinspection workflows and root-cause analytics are incomplete. |
| Marketing/outreach | PARTIAL | Content, attribution, experiments, mailboxes, SMTP, suppression, unsubscribe, deduplication and rate limits are real. Direct SMTP delivery and social-provider publication now stop before provider access when the global or exact capability gate is closed, without consuming queued work. PostgreSQL advisory locking ensures only one worker replica polls IMAP in each transaction window, preventing four-worker duplicate mailbox reads and alerts. Advertising platform executors remain explicitly manual/adapter-required. |
| Money opportunities | PARTIAL | `GET /api/money-opportunities` and Mission Control show source-labelled tender economics, sales/outreach/contracts and stored campaign spend/revenue/ROI facts. Missing values remain empty and every tender/marketing card carries record or snapshot evidence. Specialized opportunity/meeting/revenue-event models and live advertising cost imports remain missing. |
| Simulation/Digital Twin | PARTIAL | Deterministic three-scenario site economics exists. Versioned input snapshots, persisted scenarios and capacity/SLA/quality effects are missing. |
| AI Evolution Council | PARTIAL | Actionable owner requests now run through independent configured business-model members in parallel, with anti-sycophancy prompt release `request_analysis@3.0.0`, explicit quorum/disagreement, a deterministic locally generated execution brief and honest fallback when members are unavailable. Improvements, deduplication, test plans, evidence, optional handoff, a fixed golden eval dataset and immutable prompt versions remain connected. Automated prompt promotion based on longitudinal outcome evidence and two-provider production credentials are still missing. |
| Telegram Control Center | PARTIAL | Exact user/chat RBAC bindings, pseudonymous denial audit, natural Russian text, assigned/due paginated task views, workflow correlation, signed durable approvals, acknowledged critical alerts and a source-linked CEO Brief work. Company Brain search is available through the protected API/tool gateway but has no dedicated Telegram ingestion wizard yet. |
| Proactive automation | PARTIAL | Scheduler detects tender deadlines, overdue payments and unfilled shifts with deduplication. Twenty work is not queued without complete local credentials, and recent Twenty or Perplexity credential failures apply a 24-hour provider-specific backoff instead of growing a false task backlog. Selected high/critical events have correlated retrying Telegram alerts, dead-letter state, acknowledgement and metrics. Configurable recipient groups and a broader trigger catalog remain missing. |
| Observability | PARTIAL | Health/readiness, task/agent state, structured redacted JSON logs with HTTP correlation IDs, an authenticated Prometheus metrics endpoint and explicit agent success/latency/staleness SLOs are connected. Distributed tracing, dashboards and SLO alerts are still missing. |
| CI/CD | PARTIAL | Tests, coverage, golden agent evals, Ruff, targeted strict mypy, dependency audit, CodeQL, Compose build, migrations and HTTP smoke are enforced. Trivy builds the production application image on every PR, main push and daily schedule, blocks fixed high/critical OS or library vulnerabilities and detected image secrets, and uploads SARIF for same-repository runs. A fully resolved lockfile and deployment environment approval gate are still missing. |
| Procurement execution | MISSING | No production ETP submission, signing, bidding, supplier ordering, payment or post-win procurement executor is connected. |
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
