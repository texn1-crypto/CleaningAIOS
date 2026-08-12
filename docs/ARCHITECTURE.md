# CleaningAI OS 2.0 architecture

## Layers

1. **System of Record** stores leads, tenders, finance entries, campaigns,
   candidates and the linked company graph: client → site → contract/shift/complaint.
2. **System of Intelligence** contains Company Brain, goals/KPIs, tender scoring,
   object economics, scenario simulation, structured decisions and measured outcomes.
3. **System of Action** contains the transactional Event Bus, scheduler, persistent
   task queue, Agent Runtime, outreach worker, Telegram and Mission Control.

## Controlled execution

Every domain write and its event are committed in one database transaction. The
worker routes the event to Sales, Tender, HR, Finance or Marketing. Agent Runtime
records input, output, evidence, cost and errors. Failed work is retried with backoff
up to `max_attempts`; exhausted work remains visible as failed.

Decision Engine is deterministic. Financial, legal, contractual, final HR, tender
submission and bulk outreach actions create a separate owner approval. An approval
is bound to its exact action and resource, so it cannot authorize another task or
campaign. Task approval automatically returns the blocked task to the queue.

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
