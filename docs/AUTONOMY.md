# Bounded commercial autonomy

CleaningAIOS uses deterministic, owner-controlled authority envelopes to reduce
approval fatigue without turning payments, signatures or tender submission into
unattended actions.

## Modes

- `AUTO_SAFE`: deterministic read, analysis, drafting and reporting actions.
- `AUTO_WITHIN_LIMIT`: an external action may run only inside a current owner-created
  envelope with exact scope and numeric limits.
- `APPROVAL_REQUIRED`: the existing exact resource/action/payload approval remains
  mandatory.
- `FORBIDDEN`: the runtime refuses unattended payment, bank-detail changes,
  electronic or contract signature, tender submission, kill-switch disable,
  irreversible deletion and transfer of secrets/personal data to AI.

Unknown autonomy action names fail closed. Supplying an `autonomy_action` never
weakens an explicit protected `action_kind`; when both are present, both policies
must pass.

The code-owned catalog is available to managers at:

```text
GET /api/autonomy/policy
```

## Creating an envelope

Only the owner can create or revoke an envelope. This call is the exact owner grant;
the server persists its canonical SHA-256 digest, scope, limits, actor and expiry and
emits audit/outbox evidence.

Example for one deterministic answer to a proven inbound request:

```json
POST /api/autonomy/envelopes
{
  "envelope_key": "inbound-email-pilot-2026-09",
  "action": "inbound_lead_reply",
  "scope": {
    "channels": ["email"],
    "recipient_categories": ["inbound_consented_lead"],
    "template_keys": ["inbound-reply-v1"]
  },
  "limits": {
    "max_actions_total": 20,
    "max_actions_per_day": 5,
    "max_recipients_per_action": 1
  },
  "expires_at": "2026-10-01T00:00:00Z",
  "rationale": "Reply to customers who explicitly requested contact"
}
```

Example for a small Yandex Direct operating envelope (the advertising executor is
not implemented yet, so a task still cannot claim a provider effect without an
official receipt):

```json
{
  "envelope_key": "yandex-pilot-2026-09",
  "action": "marketing_campaign_manage",
  "scope": {"channels": ["yandex_direct"]},
  "limits": {
    "max_actions_total": 100,
    "max_amount_per_action": "500.00",
    "max_daily_amount": "2000.00",
    "max_monthly_amount": "30000.00"
  },
  "expires_at": "2026-10-01T00:00:00Z",
  "rationale": "One limited advertising pilot"
}
```

The server rejects an unbounded envelope. Marketing actions require an explicit
monetary cap. Envelopes cannot last more than 366 days.

Inspect current grants and aggregate usage without recipient data:

```text
GET /api/autonomy/envelopes
```

Revoke immediately:

```json
POST /api/autonomy/envelopes/{id}/revoke
{"reason": "Pilot stopped by owner"}
```

## Runtime contract

An `AUTO_WITHIN_LIMIT` task supplies:

```json
{
  "autonomy_action": "inbound_lead_reply",
  "autonomy_idempotency_key": "inbound-reply:conversation-123:first",
  "autonomy_context": {
    "channel": "email",
    "recipient_category": "inbound_consented_lead",
    "template_key": "inbound-reply-v1",
    "recipients": 1
  }
}
```

Policy locks a matching envelope row, evaluates its total/daily/weekly/monthly
limits, records one append-only usage receipt and injects the exact receipt ID into
the task. Replaying the same idempotency key and context reuses the receipt; changing
the action or context under that key is rejected.

Authorization is not evidence of external completion. The orchestrator requires an
action-specific queue/provider receipt. Missing evidence makes the task `blocked`
and creates the existing improvement/incident evidence instead of marking it done.

The first connected executor is `inbound_lead_reply`:

1. the lead must have an email and stored inbound consent;
2. an InboxMessage linked to the lead must also carry consent evidence;
3. suppression is checked;
4. the fixed `inbound-reply-v1` template is generated from CRM facts;
5. an OutboundMessage is created with the exact authority-use ID;
6. the worker rechecks that the envelope is active immediately before SMTP;
7. revocation/expiry changes the message to `blocked_authority` without provider
   access.

Supplier RFQ, calendar-provider and advertising actions are present in policy but
remain **executor gaps**. They intentionally block after authorization unless the
corresponding implementation returns the required persisted/provider receipt. An
authority envelope must never be presented as proof that those integrations work.

## Money opportunities

Managers can inspect the factual commercial snapshot at:

```text
GET /api/money-opportunities
```

Mission Control renders the same response. It reads tender assessment snapshots,
CRM records, outbound/inbound messages, contracts, campaign spend and stored won
revenue. Every card carries record IDs or snapshot hashes. Missing data remains
missing; the endpoint does not create opportunities or infer revenue.
