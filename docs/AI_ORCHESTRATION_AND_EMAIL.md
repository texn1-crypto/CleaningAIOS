# AI orchestration and compliant email delivery

## Provider responsibilities

CleaningAI OS uses providers through one least-privilege router. No provider gets
database credentials, recipient lists, banking data, raw customer messages or
application tools by default.

| Layer | Provider | Responsibility | Write authority |
| --- | --- | --- | --- |
| Orchestration | CleaningAI OS | State, RBAC, audit, approvals, idempotency, queues | Controlled local writes |
| Request analysis | OpenAI Responses | Structured intent and capability analysis | None |
| Business review | Anthropic Messages | Alternative strategic review and risk critique | None |
| Agent quality research | Perplexity Sonar | Research-grounded review of aggregate telemetry and evaluation proposals | None |
| Product implementation | Codex/Workspace Agent | Code changes with tests and review | Repository only |
| Business execution | Deterministic local adapters | Approved publication, delivery and platform operations | Policy-gated |

Perplexity is an evaluator, not a self-modifying trainer. Meta Brain sends only
agent names, status counts, telemetry-gap names and aggregate decision-outcome
statistics. Every recommendation remains advisory, is stored with the task result,
and must pass local regression/evaluation checks before a prompt or route changes.

## Approval flow

Every `approval.requested` event must have a Telegram notification containing
signed **approve**, **reject** and **request changes** buttons. A generic fallback
creates the approval card when a domain module did not create its own card. Existing
cards are reused so the owner is not sent duplicates. Approval never performs a
financial payment, contract signature or tender submission automatically.

## Email delivery

Consumer Mail.ru, Gmail and Yandex mailboxes are unsuitable as a rotating bulk-mail
infrastructure. Creating extra accounts or varying text to avoid provider controls
is not a supported delivery strategy and damages domain reputation.

For CleaningAI OS on Yandex Cloud, the preferred application transport is **Yandex
Cloud Postbox** with a company-owned domain. It is SMTP-compatible with the existing
outbox and provides domain authentication and delivery logs. Configure and verify:

1. A sender domain owned by the company.
2. DKIM (required), SPF and DMARC DNS records.
3. A monitored reply address and complaint/bounce processing.
4. Existing consent provenance, suppression, unsubscribe and per-mailbox limits.
5. Postbox SMTP credentials through server environment secrets, never Telegram.

For marketing campaigns that need list management, double opt-in, unsubscribe pages,
webhooks and campaign analytics, use **UniSender** through a future provider adapter.
No provider can guarantee zero blocks. Legitimate deliverability depends on consent,
domain authentication, reputation, bounce/complaint handling and relevant content.

Activation is intentionally `credentials_required` until the owner chooses a company
domain, verifies DNS and creates provider credentials. A provider switch must not
requeue or replay an already attempted campaign.
