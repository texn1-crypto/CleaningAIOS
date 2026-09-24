# Lead PDF preview-to-delivery repair — 2026-09-24

Production improvement: **953**. Affected baseline:
`952985b8d0f55c9a5ce0c56cdbfd1e51a5ddb263`.

## Confirmed defect

`build_instant_lead_report(notify_owner=False)` saved a valid PDF and stamped
each lead's material signature. A later ordinary scout request with
`notify_owner=True` skipped those unchanged leads as `no_new_information`.
The cached-report branch also returned before creating a missing notification.
Thus a successful Task or saved PDF did not guarantee an owner handoff.

Production Task 16968, lead 1881 and report 1882 illustrate the persisted preview
state. That task intentionally disabled notifications; this repair does **not**
retroactively send it or bulk-replay any tasks. A subsequent normal request must
explicitly enable owner notification. No prospect outreach is authorized.

## Repair and preserved boundaries

- A matching signature suppresses repeated delivery only when the linked report
  is no longer a preview. Legacy previews without `notification_id` are supported.
- Reuse the existing report/PDF after verifying storage confinement, PDF header
  and SHA-256. Missing or modified artifacts fail before creating an outbox entry.
- Use the existing `lead-report:<digest>:telegram` idempotency key and persisted
  owner outbox. Do not reset queued, sent, dead-letter or waiting-configuration
  notifications. Restore lead markers when reusing a cached report.
- Serialize report publication using a PostgreSQL transaction advisory lock.
  Keep SQLite tests compatible; no schema migration or new dependency is needed.
- Audit a new delivery queue entry. `external_messages_sent` remains false while
  queueing. Only an actual sent owner notification and current qualification
  evidence can contribute to `qualified_owner_handoffs`.

The existing lead scout and management-company verification Task paths call this
function; API/Telegram entry points and protection rules are unchanged. CI now
also applies strict mypy to `app/lead_reports.py`.

## Verification evidence and release status

Synthetic regressions in `tests/test_lead_report_delivery.py` exercise real PDF
generation, ORM persistence and the outbox. The Task/API regression simulates
only the external discovery provider; it is not live supplier verification.
Before the fix, the preview upgrade returned `no_new_information`. After it,
all 11 focused tests pass, including legacy previews, damage, changed leads,
existing delivery states, lost markers and the scout dispatcher path.

An isolated existing test PostgreSQL deployment ran two concurrent delivery
transactions after committing a synthetic preview: one reused the PDF and queued
one notification; the other returned `no_new_information`. Telegram credentials
were explicitly empty, the notification stayed `waiting_configuration`, and no
worker, scheduler or bot was started. No production data was used.

The final full run passed 661 tests, including the additional Task regression.
Ruff, all mandatory CI strict-mypy targets plus this module, 39/39 agent evals,
100/100 executed quality regressions, Compose configuration validation, deployment
shell syntax and dependency audit passed. These are test results, not a claim of
live delivery, all-provider availability, full requirement coverage or L6.
The isolated PostgreSQL Alembic upgrade/current/check passed at revision 0034
without schema drift; its database was stopped afterward, preserving test data.
The bounded independent Claude review timed out without findings or approval.
Its status guard also observed pytest removing its temporary SQLite journal;
the final Git status contains only the five intended change paths.

Release gates still require CI image/security and isolated
Compose/Alembic smoke, followed by the normal backed-up deployment and matching
runtime SHA, health/readiness and Telegram verification. The PR and improvement
953 must carry that final commit/test/runtime evidence. Requirement 138 remains
PARTIAL; no other requirement is promoted by this repair.
