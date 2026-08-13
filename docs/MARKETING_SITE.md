# Public website and Marketing OS

## What is operational

- `/` is the public responsive website; `/services`, service detail pages, `/prices`, `/about`, `/contacts` and `/journal` form the public multi-page catalogue. `/mission-control` keeps the existing internal dashboard.
- The same-origin lead form validates consent and contact details, uses a honeypot and a database-backed hourly rate limit, and never accepts bank credentials.
- A valid submission creates or updates the shared CRM lead, records an inbound contact and inbox message, preserves UTM attribution and emits a domain event.
- Deterministic scoring marks urgent/high-value requests as qualified. A hot lead creates a high-priority Sales Agent task and an owner email notification. The notification waits visibly for SMTP credentials instead of pretending delivery.
- Published `website` content from the shared content plan appears in the News section. Content can be updated and published through `/api/marketing/content/{id}`.
- Media assets have a durable queue and evidence fields. Image jobs can be fulfilled by the Codex ImageGen workflow; video jobs report missing credentials or adapters. Static assets shipped with the site were generated specifically for CleaningAI and visually checked. The public catalogue uses separate original photographs for business centres, residential common areas, industrial/warehouse cleaning and exterior facade/territory work; optimized project copies live under `app/static/services/` and contain no customer marks or personal data.
- Daily social plans remain `visual_pending` until both image assets are ready, carry a SHA-256, and have explicit visual-review evidence. The bot then sends an album containing every final image, exact caption, target channel and schedule. The approval payload is bound to a digest of that immutable preview; editing any caption, schedule, URL or image hash invalidates approval.
- Marketing providers and experiments share the business-record model. Experiments have unique UTM keys and calculate attributed leads, qualified leads and cost metrics from CRM evidence.
- Campaign activation is deliberately manual: after owner approval, an operator records the real external campaign ID. The system does not claim an external campaign exists before that happens.
- Supplier invoices are bound to a marketing provider and a masked company-requisites profile. Every invoice creates a financial owner approval and a Telegram notification. Approval changes the invoice to `approved_for_manual_payment`; it never transfers funds.
- `/api/ai/providers` reports the real provider state and the minimum data scopes. No AI provider receives blanket access, raw secrets, banking credentials or unapproved authority.

## Owner data required before public launch

Set these values in `.env` on the production server:

```dotenv
COMPANY_NAME=
COMPANY_LEGAL_NAME=
COMPANY_INN=
COMPANY_PHONE=
COMPANY_EMAIL=
COMPANY_ADDRESS=
COMPANY_SERVICE_AREA=
PRIVACY_CONTACT_EMAIL=
OWNER_NOTIFICATION_EMAIL=
PUBLIC_BASE_URL=https://your-domain.ru
SOCIAL_TELEGRAM_URL=
SOCIAL_VK_URL=
SOCIAL_ODNOKLASSNIKI_URL=
SOCIAL_INSTAGRAM_URL=
```

In production the public lead form stays disabled until `COMPANY_LEGAL_NAME` and either `PRIVACY_CONTACT_EMAIL` or `COMPANY_EMAIL` are present. The owner or a Russian privacy lawyer should verify the final privacy text and whether the company must take any regulator-specific steps before launch.

For hot-lead email delivery, configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` and `OWNER_NOTIFICATION_EMAIL`. For invoice approvals, configure `TELEGRAM_BOT_TOKEN` and `OWNER_TELEGRAM_ID`.

## Russian marketing channels

The provider registry supports Yandex Direct/Business, VK Ads, 2GIS, Avito, Telegram Ads, agencies and manual channels. Creating providers and hypotheses works without platform credentials. Automatic external activation is intentionally disabled until a reviewed executor exists; record the platform campaign ID after manual launch.

- Yandex Direct: register an OAuth application, request API access and obtain an OAuth token. Official API guide: <https://yandex.ru/dev/direct/doc/ru/concepts/access>.
- Yandex Business: create and verify the company profile, then choose the advertising product: <https://yandex.ru/support/business-priority/ru/advertising>.
- VK Ads: create a business advertising account through the official VK business products page: <https://vk.company.ru/ru/company/business/>.
- 2GIS: create/manage the company card and contact the advertising team through <https://business.2gis.ru/> and <https://reklama.2gis.ru/>.
- Telegram Ads: create an advertiser account through <https://ads.telegram.org/getting-started> and verify every creative against <https://ads.telegram.org/guidelines>.
- Avito: create a business account for Services. If API access is available for the account, obtain client credentials from the account/API support and store them only as server secrets.

Credentials belong in the production secret store or `.env`, never in the database, Telegram messages, prompts or Git. A credential only changes integration status to `credentials_present_adapter_required`; it does not silently authorize spend.

## Core API flow

1. `POST /api/public/leads` — public website submission.
2. `POST /api/marketing/providers` — supplier/platform registry.
3. `POST /api/marketing/experiments` — hypothesis and UTM contract.
4. `GET /api/marketing/experiments/{id}/analytics` — evidence-backed result.
5. `POST /api/company/requisites` — owner-only legal/bank requisites (no bank login or payment token).
6. `POST /api/marketing/invoices` — invoice plus Telegram owner approval.
7. `POST /api/approvals/{id}/approve|reject` — decision only; payment remains manual.
8. `POST/PATCH /api/marketing/media-assets` — durable image/video workflow.
9. `GET /api/marketing/social-batches/{id}/preview` — exact owner-review payload for a social batch.

## Continuous improvement

The existing hourly CleaningAI OS heartbeat checks, in order: queued capability improvements, queued media assets and due `scheduled` website content. It can use ImageGen for original public assets, but it cannot access customer PII or secrets. It never publishes drafts/ideas and never performs protected commitments.
