# Twenty CRM projection

CleaningAIOS remains the system of record for leads, consent, suppression, tasks,
reports and audit evidence. Twenty is an operational sales view. The integration
projects only leads in `owner_review`, `qualified`, `sales_ready` or `won` status
that also carry organization scope, an HTTPS public website and a verification
timestamp. Consumer website-form and Telegram-wizard leads are never projected.
The connector never treats public contact data as outreach consent and never sends
a message.

## Configuration

1. In the owner's Twenty workspace open **Settings → API & Webhooks** and create a
   dedicated least-privilege API key.
2. Store the key only in the deployment environment as `TWENTY_API_KEY`; never add
   it to Git, a task payload, an agent prompt or chat.
3. Set `TWENTY_ENABLED=true`. For the local self-hosted service use
   `TWENTY_BASE_URL=http://host.docker.internal:3020`. Remote deployments must use
   HTTPS.
4. Restart the CleaningAIOS web, worker and scheduler containers.

Twenty documents API-key authentication and the per-workspace REST surface at
<https://docs.twenty.com/developers/extend/api>.

## Runtime contract

- The scheduler queues at most one `sync_twenty_verified_leads` Sales task per
  configured interval.
- One run processes at most 45 leads and defaults to 25, leaving room under
  Twenty's documented 100-request-per-minute limit for create reconciliation.
- A lead stores its last projection fingerprint and Twenty company ID inside the
  existing CRM record. An unchanged successful projection is skipped.
- Before creating a company, the adapter searches Twenty by exact organization
  name and verifies the public website host. This reconciles a create whose response
  was lost before the remote ID could be stored. Creates also carry a deterministic
  idempotency header; updates address the stored company ID.
- Only the organization name and verified public website are transmitted. Public
  email addresses and phone numbers stay in CleaningAIOS.
- Provider responses are size-bounded, redirects are not followed and errors are
  reduced to safe categories. Tokens and raw provider errors are never persisted.
- `401`/`403` becomes `credentials_required`; rate limits and transport failures
  become `unavailable`; schema rejection is held until the lead projection changes.
- Every successful create/update and every batch outcome is written to the audit
  trail. A daily deduplicated owner notification reports configuration failures.

Configuration status is included in `GET /api/integrations`. Runtime status is
available to a manager at `GET /api/integrations/twenty`.
