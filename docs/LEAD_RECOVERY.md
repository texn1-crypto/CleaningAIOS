# Lead pipeline recovery — 2026-09-23

The handoff goal used to count a delivered report's CRM `owner_review` labels
without checking qualification. A production batch contained 26 rejected cards
alongside 36 owner-review candidates, while all 62 counted toward the goal.

The reconciliation now re-evaluates the existing deterministic qualification
rules and requires service-area fit, property fit, organization identity, public
organization contacts and fresh source evidence. Repeated reports and duplicate
organizations (tax ID, or matching normalized name and website) count once.
Rejected, unverified and stale cards do not count. CRM lifecycle history is not
rewritten. Owner review does not mean sales ready or authorize outreach.

The daily CEO plan has at most four priorities. When the search provider is
unavailable, provider recovery replaces blocked search as the first priority;
fallback verification, qualification and evidence-based measurement stay visible.

Validation: 498 tests passed; Ruff, all CI strict-mypy targets and 32 agent evals
passed. New regressions cover rejected/out-of-area and missing/stale evidence,
duplicate organizations/reports, unchanged CRM status and both provider states.
An isolated Compose deployment passed PostgreSQL Alembic upgrade/current/check
at revision `0034`, `/health`, `/ready`, and healthy web/worker/scheduler checks.
Its containers were stopped after validation; no production data was used.
The optional independent Claude review did not finish within its bounded budget
and is not recorded as an approval.

Runtime configuration was recovered separately on release `181e21b`: Crawl4AI's
existing secret bootstrapper enabled its private token and app configuration.
The pinned image passed public-page, private-address rejection and PostgreSQL
network-isolation checks; the application's guarded client also fetched a public
page successfully. Perplexity still returns HTTP 401 and needs account access;
that is not a successful provider integration. No prospect messages were sent
by the recovery verification.

A read-only production reconciliation of 10 delivered discovery reports found
62 unique lead cards but only 12 unique organizations meeting the corrected
qualification gate. Missing property-type fit affected 42 cards and out-of-area
fit affected 26 (these groups overlap). The corrected monthly progress at this
pre-release check is 12/20; production must remeasure it after deployment.

One owner-requested bounded recovery task (`16968`, one attempt, notifications
and outreach disabled) verified existing management company `841` using the
audited public-crawl tool and created CRM lead `1881` in `owner_review`. The task
completed with a successful tool receipt and `external_messages_sent=false`.
This new undelivered card is not included in the delivered-handoff count.

Local runtime notes: Docker Desktop was stopped. Starting it restored the local
web/database, Crawl4AI and Twenty containers. Twenty completed its existing
startup migrations and became healthy. The local and cloud databases have
different task histories but the same Telegram delivery identity. To avoid two
independent notification producers, only the newly resumed local scheduler and
four local workers were stopped again; local data/UI and the four healthy cloud
workers remain intact. The existing local bot stayed stopped. No data was deleted.

OmniRoute was registered as the user's loopback-only launchd service
`com.cleaningaios.omniroute`; its direct loopback monitoring endpoint returned
`healthy` with `setupComplete=true`. Twenty requires its owner's dedicated API
key from Settings → API & Webhooks before the CleaningAIOS projection can run.
Local readiness is not yet consistently fast: repeated OmniRoute/Jarvis checks
also timed out during severe host memory pressure (8 GB physical RAM, roughly
6.6 GB used swap; an OmniRoute process sample showed garbage collection).
A single healthy response is not sufficient evidence of stable 24/7 operation.

Code changes are confined to `app/lead_outcomes.py`, `app/lead_qualification.py`,
`tests/test_lead_outcomes.py` and this report in the isolated recovery worktree.
The two original worktrees retain their pre-existing 73 and 9 dirty paths.
On 2026-09-24 the owner explicitly authorized autonomous technical repairs,
commits, PRs, merges after successful checks and production deployment. This
authorization does not waive the application's exact approvals for financial,
legal, tender, signing, payment, bulk outreach or final hiring actions, nor allow
overwriting others' changes, deleting data, bypassing CI, MFA or authorization.
Future technical work remains bounded to the existing system, isolated from the
dirty worktrees and validated before release. Runtime completion must be checked
against the deployed release SHA, not inferred from this document.

This is a narrow recovery, not completion of Tender Autopilot L6 or the 325-item
master specification. Existing capability limitations remain in the capability audit.
