# Consent-first voice gateway

CleaningAIOS owns lead state, consent, approval, suppression, rate limits and CRM
history. A separately operated voice gateway owns SIP/PSTN transport and the live
conversation runtime. The gateway may be implemented with LiveKit Agents and SIP,
Pipecat plus an approved carrier, or another reviewed provider behind the same small
HTTPS contract. It never receives database credentials or general CleaningAIOS API
access.

## Runtime boundary

1. An operator creates `POST /api/telephony/calls` for one CRM lead, exact purpose,
   script, schedule and idempotency key.
2. The Sales task is blocked until the owner approves the exact task under the
   separate `voice_call` capability.
3. Queueing verifies stored phone consent or a customer-requested callback and checks
   suppression. The full phone number remains in the lead record; the call queue
   stores only its last four digits.
4. Immediately before provider access the worker rechecks the global stop,
   `voice_call` capability, consent, suppression, configured local hours and minute/
   daily limits. PostgreSQL serializes the aggregate rate check and reservation with
   a transaction-scoped advisory lock, so horizontally scaled workers cannot each
   consume the same remaining rate slot.
5. Before transmission the row becomes `reconciliation_required`. This prevents an
   unknown provider outcome from being retried as a second call after a timeout.
6. The gateway returns a provider call ID. It sends HMAC-SHA256 callbacks for later
   status changes. Duplicate event IDs and backward state transitions are ignored.
7. A terminal outcome creates one CRM `ContactEvent`. An `opt_out` callback also adds
   the number to suppression.

Recording is always sent as `false`; the gateway must reject any request that tries
to override it. The request also contains `ai_disclosure_required=true`. CAPTCHA,
anti-bot bypass, caller-ID spoofing, bulk cold calling and calls without documented
consent are outside this contract.

## Gateway request

CleaningAIOS sends `POST TELEPHONY_GATEWAY_URL` with bearer authentication and an
`Idempotency-Key` header:

```json
{
  "call_attempt_id": "voice-campaign-42-lead-7",
  "to": "+79990001234",
  "purpose": "Confirm the requested site survey",
  "script": "Disclose AI assistance and ask whether now is convenient.",
  "callback_url": "https://cleaning.example.com/api/telephony/callback",
  "recording_enabled": false,
  "ai_disclosure_required": true
}
```

The accepted response is bounded JSON:

```json
{"provider_call_id":"provider-123","status":"ringing"}
```

## Signed callback

The gateway sends compact JSON to `POST /api/telephony/callback`. The
`X-Telephony-Signature` header is `sha256=<hex HMAC-SHA256>` over the exact raw body
using `TELEPHONY_WEBHOOK_SECRET`.

```json
{
  "call_attempt_id": "voice-campaign-42-lead-7",
  "provider_call_id": "provider-123",
  "provider_event_id": "event-456",
  "status": "completed",
  "duration_seconds": 75,
  "outcome_summary": "Customer requested a site survey."
}
```

Supported statuses are `queued`, `ringing`, `answered`, `completed`, `no_answer`,
`busy`, `failed`, `cancelled` and `opt_out`.

## Activation

Keep `TELEPHONY_ENABLED=false` until the gateway, SIP number, callback HTTPS endpoint
and disclosure text are reviewed in staging. Put token and webhook secret only in the
server secret store or untracked `.env`; never paste them into Telegram, a task, an
issue or a commit. Configure conservative hours and limits first, then verify one
owner-approved callback request end to end before increasing capacity.
