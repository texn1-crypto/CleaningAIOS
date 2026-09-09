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

Tender Autopilot extends these same layers. Its current production foundation is an
append-only, evidence-bound decision snapshot with Decimal economics and a guarded
participation approval card. Target architecture, state machine, domain model,
security boundaries and the honest L6 gap analysis live in
[`docs/tender-autopilot/`](tender-autopilot/README.md).

Request Analyst evaluates each owner Telegram message before normal execution. A
supported request follows its normal route. A missing credential is reported as
configuration work. A genuine capability gap is stored in the durable improvement
queue with a redacted Codex prompt, acceptance criteria and test plan. Repeated
requests reuse the same deduplication key. Optional Workspace Agent handoff uses an
idempotency key; local Codex automation can consume the same queue without sharing
application secrets.

Contextual text requests use the same durable request history. “Улучши это” can use
an explicit Telegram reply, while a request for feedback on the previous letter
resolves the latest saved request for the same channel and user. The copywriter
returns feedback and a revised draft through Task/Agent Runtime; it never sends the
text externally. Stored request text is credential-redacted before later retrieval.

For owner Telegram tasks, Orchestrator applies a post-run evidence gate. Requests
for files cannot become `done` without an artifact; credentials and adapter gaps are
also incomplete. The gate creates one deduplicated improvement and a linked AI CEO
incident task with the source task, agent, reason, handoff and responsible party.

## Controlled execution

Every domain write and its event are committed in one database transaction. Each event
has a stable UUID, schema version, actor, correlation/causation identifiers and UTC
occurrence time. The worker routes it to Sales, Tender, HR, Finance or Marketing and
stores a per-consumer receipt in the same transaction as the routed task. A succeeded
receipt prevents the same consumer from executing the event again after a retry or
restart. Agent Runtime records input, output, evidence, cost and errors. Failed work is retried with backoff
up to `max_attempts`; exhausted work remains visible as failed.

Optional agent tool requests pass through one default-deny read-only gateway.
Each local or remote MCP tool has an exact agent allowlist, call/time/result-size
budgets and a separate aggregate audit row. Production remote MCP endpoints must
be HTTPS, stateless, explicitly configured and limited to named tools; shell,
browser, filesystem, arbitrary HTTP and write operations are absent. Remote input
is screened for credential, personal and financial data, and all returned content
is marked untrusted.

Company Brain document storage is append-only by source version. Ingestion requires
a manager role and an idempotency key, records content checksums and provenance,
splits text into bounded immutable chunks and keeps document bodies out of audit and
event payloads. Retrieval applies document ACLs and expiry before deterministic
lexical/character-ngram ranking, searches only the latest version of each source and
returns a citation bound to the document/chunk checksums. Retrieved text is untrusted
evidence, not instructions, and is not automatically sent to any external AI
provider. The legacy key/value Company Brain remains backward compatible.

Task status changes go through the shared state machine. Every initial state and
transition stores actor, reason, correlation, details and an idempotency key in
`task_transitions`. The production database protects this ledger with an immutable
trigger; APIs expose history but no mutation endpoint.

Decision Engine is deterministic. Financial, legal, contractual, final HR, tender
submission and bulk outreach actions create a separate owner approval. An approval
is bound to its exact action and resource, so it cannot authorize another task or
campaign. Pending approvals are versioned and expire after a configured TTL. A
terminal decision is claimed atomically, stored once in an append-only decision
ledger, and published as a domain event. Task approval automatically returns the
blocked task to the queue through an idempotent transition key, so concurrent or
duplicate decisions cannot resume the workflow twice. “Request changes” records a
terminal review outcome without resuming execution.

Telegram is a channel adapter over this same service, not a second approval
implementation. Every update is authorized server-side against an exact
`user_id`/`chat_id` binding with the roles owner/admin/manager/operator/viewer and
deny-by-default behavior. Audit entries use stable pseudonyms rather than raw
Telegram identifiers. Approval cards carry risk, amount when present, rationale,
requester and expiry. Their callbacks are short HMAC-signed envelopes bound to the
approval, action, version and expiry; the bot sends the opaque envelope back to the
server and never trusts or interprets an approval ID from the button. Legacy numeric
approval callbacks are rejected.

Selected high/critical domain events are projected from the transactional outbox by
the separate `critical_alerts` consumer. Each alert carries severity, recipient and
event/correlation identifiers behind one idempotency key. The owner-notification
worker retries transport failures with exponential backoff and moves exhausted
delivery to `dead_letter`. High/critical Telegram alerts include a separate signed
acknowledgement callback. Acknowledgement is an idempotent server-side record with an
audit entry and domain event; it never performs the underlying business action.
The notification API exposes delivery and acknowledgement metrics without secrets.

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
deterministic review. Every task delegated by the orchestrator creates an
idempotent, PII-free routing decision containing the source/delegated task IDs,
normalized task type, selected agent, creation time, deterministic expected result
and an initial `success_expected`/`at_risk` assessment. Terminal outcomes are
measured automatically and added to Meta Brain's aggregate decision success rate;
an unmet integration is measured as a missed expectation, while a task waiting for
the owner's approval remains pending until the protected workflow is resumed.
Managers can inspect the records through `GET /api/orchestrator-decisions`; task
titles, payloads, customer data and credentials are never copied into this view or
its audit events.

The deterministic CEO strategy portfolio covers every registered agent type with a
bounded recurring checkpoint containing a horizon, objective, deliverable and
success metric. Agent Runtime handles the exact checkpoint action consistently and
stores its evidence through the normal Task/AgentRun workflow. An hourly delayed CEO review
checks role coverage and completion evidence. Missing, failed or technically blocked
lanes create one deduplicated critical System Admin task for the review cycle; no
external action or protected approval is inferred. The operating strategy and
multi-year horizons are documented in `docs/AI_AGENT_STRATEGY.md`.

`GET /api/ceo/brief` is a read-only primary-database snapshot with one freshness
timestamp and explicit source endpoints/record IDs. Facts and deterministic
recommendations are separate fields; facts are never attributed to an LLM. Telegram
renders the same response. Its only write option creates a normal analytical CEO
task through the shared Tasks API, and the payload explicitly forbids an automatic
critical action.

Tasks can carry an exact internal assignee and due date. Telegram-created tasks are
assigned to the already-authorized pseudonymous identity. The Control Center queries
the server for paginated `all`, `mine`, `overdue`, `critical`, and
`critical_events` views; callbacks contain only a validated view/page cursor. Task
rows expose the latest workflow transition and correlation ID, while critical-event
rows link back to their source resource. Navigation never infers task state locally.

## External integrations

- Telegram polling starts only when bot token and owner ID are provided. The bot
  does not expose a webhook endpoint, so there is no unauthenticated webhook path.
  Commands and callbacks pass through server-side Telegram RBAC middleware.
- Telegram DOCX/PDF proposal attachments use protected shared storage and run through
  Orchestrator, Copywriter and Creative. Files over the cloud 20 MB download limit
  require a separately operated local Bot API endpoint.
- SMTP passwords are supplied through environment variables. Additional mailbox
  records contain only an environment-variable name in `secret_ref`, never a password.
- `TENDER_SOURCES` declares comma-separated HTTP JSON feeds. A feed returns either a
  list or `{ "items": [...] }`; each item needs `external_id`, `title`, and may include
  `deadline_at`, scoring data and `documents`. `TENDER_SOURCE_TOKEN` supplies an
  optional bearer token. Portal-specific authentication or non-JSON formats still
  require a legal provider adapter. No fabricated tenders are used.
- The advisory router supports three native contracts. `LLM_BASE_URL` targets the
  OpenAI Responses API and sends `LLM_API_KEY` only in the Authorization header with
  `store=false`. `ANTHROPIC_BASE_URL` targets Claude Messages API and sends
  `ANTHROPIC_API_KEY` only as `x-api-key`, with an explicit `anthropic-version`.
  `GEMINI_BASE_URL` targets Google's Generate Content API and sends `GEMINI_API_KEY`
  only as `x-goog-api-key`. All adapters require JSON-schema structured output and
  have no application tools.
  With `LLM_PROVIDER=auto`, Claude is preferred for aggregate business synthesis,
  OpenAI for product/request capability analysis, and Gemini 3.7 Flash is the fast
  structured fallback after a transient primary-provider failure. Decisions,
  scoring, approvals and simulations remain deterministic and auditable.
- AI prompts are immutable releases with semantic versions and SHA-256 digests.
  Stable/candidate selection is deterministic per canonical input, defaults to a
  zero-percent candidate cohort and can be rolled back without rewriting run
  history. Only prompt metadata, never raw prompt text, is exposed by the manager
  API and persisted with advisory results.
- Historical agent runs can be replayed only through a new idempotent task and a
  fresh exact owner approval. Replay creation strips old approval artifacts and
  credential-like fields, creates a new correlation ID and never executes work in
  the request transaction. Approval only returns the task to the normal queue.
- The provider router exposes task-specific, least-privilege scopes. Public content
  may reach image/video providers; CRM personal data, banking credentials and secrets
  are forbidden. Image/video adapter gaps are reported rather than simulated.
- Owner notifications use a durable queue with retries, dead-letter state,
  severity/correlation metadata, signed acknowledgement and delivery metrics. Hot
  leads use transactional SMTP without unsubscribe markup; approvals use signed
  Telegram actions handled by the backward-compatible polling bot.
- Public-business lead research uses the advisory Perplexity adapter and accepts
  only cited HTTPS pages, organization-level contacts and an explicit target-region
  allowlist. Deterministic validation rejects personal/free-mail contacts, private
  network URLs and uncited results before deduplicated CRM persistence. A public
  address is stored with `outreach_consent=not_verified`; the capability creates no
  consent record and sends no message.
- A lead coordinator fans each bounded research wave out to specialized management,
  commercial-property, tender and public-social scouts. They share the same durable
  lead/contact registry rather than separate lists. Every newly discovered or
  materially changed lead creates an idempotent, checksum-bound PDF and queues it
  for immediate Telegram delivery; repeated observations do not resend the same
  report. Manager-only API endpoints retain downloadable report history.
- Management-company discovery reuses that cited public-source boundary and writes
  normalized emails into one durable contact directory. Owner-uploaded baseline
  addresses are marked and excluded from new-contact exports. New public addresses
  create only `outreach_candidate` records with consent/suppression/owner-approval
  state; they never create outbound messages directly. A weekly idempotent task
  generates checksum-bound PDF, XLSX and CSV artifacts and marks contacts only after
  a successful issue, so later issues contain only newly discovered addresses.
