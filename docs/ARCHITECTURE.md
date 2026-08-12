# CleaningAI OS 2.0 architecture

## Layers

1. **System of Record** stores leads, tenders, finance entries, campaigns,
   candidates and the linked company graph: client → site → contract/shift/complaint.
2. **System of Intelligence** contains Company Brain, goals/KPIs, tender scoring,
   object economics, scenario simulation, structured decisions and measured outcomes.
3. **System of Action** contains the transactional Event Bus, scheduler, persistent
   task queue, Agent Runtime, outreach worker, Telegram and Mission Control.
4. **Public Growth Surface** contains the company website, consent-aware lead intake,
   UTM attribution, published website content, media assets, marketing providers,
   experiments and owner notifications. It writes into the same records and events;
   it is not a separate CRM.

Request Analyst evaluates each owner Telegram message before normal execution. A
supported request follows its normal route. A missing credential is reported as
configuration work. A genuine capability gap is stored in the durable improvement
queue with a redacted Codex prompt, acceptance criteria and test plan. Repeated
requests reuse the same deduplication key. Optional Workspace Agent handoff uses an
idempotency key; local Codex automation can consume the same queue without sharing
application secrets.

## Controlled execution

Every domain write and its event are committed in one database transaction. The
worker routes the event to Sales, Tender, HR, Finance or Marketing. Agent Runtime
records input, output, evidence, cost and errors. Failed work is retried with backoff
up to `max_attempts`; exhausted work remains visible as failed.

Decision Engine is deterministic. Financial, legal, contractual, final HR, tender
submission and bulk outreach actions create a separate owner approval. An approval
is bound to its exact action and resource, so it cannot authorize another task or
campaign. Task approval automatically returns the blocked task to the queue.

Marketing invoices and paid experiments use the same financial approval class.
Approval only records the owner's decision. Invoice state becomes
`approved_for_manual_payment`, and experiment state becomes `approved`; neither path
contains a payment executor. External campaign activation is recorded only when an
operator supplies the campaign ID created in the platform account.

Bulk-outreach approvals are also bound to a digest of the normalized recipient list
and the exact campaign content. Changing recipients, subject, body, sender mailbox or
schedule creates a new approval request.

## Autonomous loop

The scheduler checks tender deadlines, overdue payments and unfilled shifts. It also
queues a periodic CEO review. AI CEO reads goals, task health and site economics,
then creates deduplicated Finance/HR recovery tasks for material deviations. When
configured, the OpenAI-compatible Responses adapter receives only this aggregate
snapshot and returns a strict JSON advisory review. Safe analysis/planning
recommendations can become deduplicated tasks; protected recommendations stay
advisory and require the owner's normal approval path. LLM failures fall back to the
deterministic review. Meta Brain measures telemetry gaps and the success rate of
decisions whose outcomes have been recorded.

## External integrations

- Telegram polling starts only when bot token and owner ID are provided.
- SMTP passwords are supplied through environment variables. Additional mailbox
  records contain only an environment-variable name in `secret_ref`, never a password.
- `TENDER_SOURCES` declares comma-separated HTTP JSON feeds. A feed returns either a
  list or `{ "items": [...] }`; each item needs `external_id`, `title`, and may include
  `deadline_at`, scoring data and `documents`. `TENDER_SOURCE_TOKEN` supplies an
  optional bearer token. Portal-specific authentication or non-JSON formats still
  require a legal provider adapter. No fabricated tenders are used.
- `LLM_BASE_URL` targets an OpenAI-compatible Responses API; the default is OpenAI.
  `LLM_API_KEY` is sent only in the Authorization header, `store=false` is requested,
  and the response must match the configured JSON schema. The adapter has no tools
  and cannot perform business actions. Decisions, scoring, approvals and simulations
  remain deterministic and auditable.
- The provider router exposes task-specific, least-privilege scopes. Public content
  may reach image/video providers; CRM personal data, banking credentials and secrets
  are forbidden. Image/video adapter gaps are reported rather than simulated.
- Owner notifications use a durable queue with retries. Hot leads use transactional
  SMTP without unsubscribe markup; marketing invoices use Telegram inline approval
  buttons handled by the backward-compatible bot.
