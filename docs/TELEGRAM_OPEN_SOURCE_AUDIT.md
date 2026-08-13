# Telegram Control Center: open-source audit

Audit date: 2026-08-13. The source revisions below were resolved from each
repository's `HEAD` and all files were read at that immutable revision. No remote
installer or deployment script was executed.

## CleaningAIOS baseline

- Branch: `agent/cleaningai-os-production`.
- Telegram framework: `python-telegram-bot==21.10`, async polling.
- Application: FastAPI, SQLAlchemy 2, PostgreSQL 16, Alembic, a database-backed
  task queue, transactional domain-event outbox, deterministic Decision Engine,
  Approval Engine, worker and scheduler.
- Existing strengths: deny-by-default owner ID check, API RBAC, protected-action
  policy, task pause/resume, audit log, idempotent event publication, consumer
  receipts, notification retries and dead-letter states.
- Confirmed gaps before this work: Telegram handlers share a privileged API
  identity; callbacks contain trusted numeric approval IDs; approval decisions
  have no expiry/version/immutable decision record; callback transitions are not
  protected against concurrent decisions; denied Telegram access is not audited;
  the channel adapter owns too much navigation and rendering logic.
- Baseline: `.venv/bin/pytest -q` -> `120 passed in 3.77s`.
- Repository licensing: there is no `LICENSE` or `NOTICE` file. Therefore no
  third-party implementation is copied. Only unprotectable architectural ideas
  are reimplemented from scratch in the existing Python stack.

## Sources reviewed

| Source | Immutable revision | License/security gate | Decision |
|---|---|---|---|
| [aiogram/aiogram](https://github.com/aiogram/aiogram/commit/7a7c3502874cb63bf9537bc45498176ffb60e012) | `7a7c3502874cb63bf9537bc45498176ffb60e012` | MIT; no root `SECURITY.md` at this revision | Adopt router/middleware/FSM concepts, not the dependency. The installed `python-telegram-bot` already provides async handlers, typed filters, callbacks and polling; migration cost is not justified. |
| [ilyarolf/AiogramShopBot](https://github.com/ilyarolf/AiogramShopBot/commit/c322b09c9628385961f5435b2cd9765844e0e85f) | `c322b09c9628385961f5435b2cd9765844e0e85f` | MIT; repository documents a security policy | Adopt handlers/services/repositories separation as a structural comparison only. Reject shop, crypto, payment, referral, deployment-script and multibot logic. |
| [agentkitai/agentgate](https://github.com/agentkitai/agentgate/commit/abb46ee216c2420eae04f35de504b11862a078b5) | `abb46ee216c2420eae04f35de504b11862a078b5` | MIT; no root `SECURITY.md` at this revision | Reimplement policy outcomes, immutable decisions, expiry, notification retries and separation of approval core from notification channels. Reject the additional TypeScript service. |
| [jamjet-labs/jamjet](https://github.com/jamjet-labs/jamjet/commit/6a484a646132a18645746e8ca3838368dc90cfc3) | `6a484a646132a18645746e8ca3838368dc90cfc3` | Apache-2.0; `SECURITY.md` present | Reimplement pre-execution policy enforcement, durable pause/resume, idempotent replay and audit concepts. Reject a new runtime dependency because CleaningAIOS already has equivalent extension points. |
| [getsynkora/synkora-ai](https://github.com/getsynkora/synkora-ai/commit/cd86b41d7c1a0fe5675389d6d05e2476b49632bf) | `cd86b41d7c1a0fe5675389d6d05e2476b49632bf` | MIT; `SECURITY.md` present | Retain the channel-adapter/service separation, RBAC, worker ownership and observability concepts. Reject the multi-tenant platform, Redis/Celery and RAG stack as unnecessary for this slice. |
| [tgoai/tgo](https://github.com/tgoai/tgo/commit/995da4577f6f91edb87d0f56fc9ea4c129f1a4eb) | `995da4577f6f91edb87d0f56fc9ea4c129f1a4eb` | Modified Apache-2.0 with multi-tenant, branding and contributor restrictions; no root `SECURITY.md` | Architectural orientation only. Do not copy code, UI, branding, scripts or dependencies. Its bootstrap script was not executed. |

## Compatibility decision

Adding aiogram, AgentGate, JamJet, Redis or Celery would duplicate existing
capabilities and enlarge the trusted/dependency surface. The compatible path is:

1. keep Telegram as a thin `python-telegram-bot` adapter;
2. resolve Telegram identity and RBAC on the server;
3. generate short HMAC-signed callback tokens bound to approval ID, action,
   version and expiry;
4. execute every decision through one transactional Approval application service;
5. persist one immutable decision record and resume a blocked workflow once;
6. use the existing Event Bus and owner-notification queue for alerts and retries;
7. keep facts/queries in shared application services so Telegram, API and future
   web interfaces use the same data path.

No third-party code or dependency is added, so `docs/open-source-attribution.md`
is intentionally not created.

## Implemented compatibility slices

- Durable approval core: expiring/versioned requests, one append-only decision,
  atomic terminal claim and exactly-once workflow resume.
- Telegram identity/RBAC: exact user/chat bindings, five roles, deny-by-default,
  pseudonymous denial audit and role-aware handler middleware.
- Protected callbacks: HMAC-bound approval/action/version/expiry payloads within
  Telegram's 64-byte limit, server-side approver validation, three decision actions,
  stale/expired/forged rejection and idempotent duplicate handling.
- Polling remains the only Telegram update transport; no unverified webhook endpoint
  was introduced.
