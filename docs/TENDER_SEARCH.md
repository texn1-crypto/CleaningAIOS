# Keyword tender search and owner PDF

## Incident and delivered scope

Before this change the tender agent ranked stored records; `keywords` did not
fetch public pages. Production had no configured `TENDER_SOURCES`, no collected
tenders, and the separate Perplexity-backed tender scout was blocked by HTTP 401.
Generic PDF delivery worked, but no tender-discovery PDF had been produced.

`search_public_tenders` connects actual public reads to the existing application:
Telegram/scheduler → Task → catalog observations → PostgreSQL tender records → PDF
→ durable owner Telegram queue. No new project, LLM dependency or submission agent.

This is a **partial source layer**, not the whole internet and not Tender Autopilot
L6. Requirement 9 remains PARTIAL. Only these public catalogs are read:

- https://www.b2b-center.ru/search/sankt-peterburg/uborka-pomeshhenij/
- https://www.b2b-center.ru/search/leningradskaya-oblast/uborka-pomeshhenij/

Same-origin public detail cards provide additional evidence. No account, hidden
search API, official EIS API, CAPTCHA/MFA workaround or provider-key fallback is used.
Live availability of one catalog never proves completeness of all procurement.

## Entry points and scheduling

In the existing authorized Telegram conversation:

> Найди тендеры по словам «уборка» и «мойка окон» и пришли PDF

Quoted words become explicit keywords; without quotes the configured cleaning
vocabulary is used. Up to eight phrases, combined with OR; words within each phrase
must occur in the title, with limited deterministic Russian inflection matching.
This is filtering within the two catalogs, not arbitrary internet keyword search.
Mixed search/submission requests retain their existing protected-action route.

The existing task API can queue `agent_type=tender` or `research` with
`payload={"action":"search_public_tenders","keywords":["уборка"],"notify_owner":true}`.
Its normal RBAC and Task idempotency contract still applies. A preview can use
`notify_owner=false`; later delivery reuses the verified artifact.

`TENDER_SEARCH_ENABLED=true` enables a normal scheduler Task every four hours by
default. `TENDER_SEARCH_INTERVAL_MINUTES` is bounded to 60–1440.
`TENDER_SEARCH_KEYWORDS` uses `|` between phrases. The production application
defaults to enabled; `.env.example` disables it for isolated development/CI.
Do not start the intentionally stopped local scheduler/workers/bot while the server
uses the same Telegram identity. Rollback of scheduling: set the flag false and
restart only the active server scheduler; existing records/PDFs are retained.

## Evidence, filtering and delivery

- Public GETs respect robots rules, identified User-Agent, minimum one-second
  spacing, a 19-request budget, 12-second network timeouts and 2 MB response limits.
  Redirects, access challenges, 401/403/429 and unreadable layouts are unavailable,
  never simulated successful searches. If both catalogs fail, the Task is blocked
  without a fabricated empty report. One available catalog permits a partial PDF.
- Unknown/expired deadlines and explicit supplies-only titles are excluded. A
  catalog's region can be the buyer's address, not the place of work. Explicit
  outside-region delivery addresses/titles are excluded; absent delivery evidence
  is clearly marked in the PDF, never counted as a confirmed SPb/LO opportunity.
- Zero/absent placeholder prices remain unknown. EIS links are copied from observed
  catalog links with a verified host, notice path and registry number. Publication
  numbers or a future deadline are not proof of legal eligibility/current status.
- Tender identity is the observed EIS registration number, otherwise public-card
  identity. Source hashes, timestamps, outcomes, exclusions and facts are retained
  in `tender_search_run`; current cards use `BusinessRecord(record_type=tender)`.
  All remain `NEEDS_VERIFICATION`. No CRM handoff metric credit is created.
- One PostgreSQL advisory transaction lock serializes search publication across
  worker replicas. Same keyword/time-window replays reuse the run. Same-day
  unchanged material facts/source availability reuse the report/notification,
  even if unrelated page content or observation timestamps change.
- `tender_search_report` stores the PDF path, hash, record IDs and notification ID.
  The existing notification worker validates bytes before `sendDocument` and
  persists its delivery state. `queued` is not `sent`; inspect OwnerNotification
  for delivery evidence. Protected manager download:
  `GET /api/tender-search/reports/{report_id}/download` (401/403/409 fail closed).
- PDF snapshots show observation time, deadlines (Moscow), prices, customer,
  delivery address, source links and uncertainty. Recheck original documents before
  participation; no application, signature, bid, supplier order or payment occurs.

## Verification and evidence

`tests/test_tender_search.py` uses explicitly synthetic fixtures for parsing,
expiry/geography/price handling, robots/access/URL/size boundaries, unavailable
sources, persistence, PDF bytes/text, dispatch, delivery deduplication, preview
upgrade, scheduler isolation, protected mixed intent and authenticated download.
Telegram intent golden cases run through `scripts/run_agent_evals.py`.
These mocks are not production evidence.

The coordinator also verified actual catalog/detail pages on 2026-09-24. The
Murmansk work found inside a SPb buyer catalog motivated the geography regression.
The new module is included in strict mypy CI alongside the existing targets; full
pytest, Ruff, agent evals, dependency/container gates, Alembic/Compose/API smoke
must pass before release. Commit/CI, deployed SHA, actual run/report IDs and
Telegram `sent` evidence are recorded in the release PR after verification. Until
then this document describes the tested code contract, not a claimed deployment.

Pre-release checks on 2026-09-24: 637 pytest cases, 39/39 agent evals, Ruff and all
11 strict-mypy CI invocations passed. Compose configuration passed with
`--no-env-resolution` because the isolated local copy intentionally has no secrets;
CI must run full configuration and runtime smoke with its generated test environment.
The independent read-only Claude review timed out after 110 seconds without
changing the working tree; this is not a review approval. The coordinator reviewed
the diff and rendered/inspected the synthetic PDF; actual-source/runtime delivery
evidence is still required after CI and deployment.
