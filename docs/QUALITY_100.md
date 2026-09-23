# CleaningAIOS: 100 acceptance criteria

Owner request: 2026-09-24. These are 100 executable acceptance contracts for the
existing application, not 100 new features or full L6 readiness. The catalog is
`app/data/quality_criteria_100.json`; managers can read it at
`GET /api/agents/quality-criteria`. Each criterion has an exact regression test.
Catalog availability is not a pass.

Coverage: public research, handoffs, sales qualification, agent execution/skills,
knowledge citations, provider failures, bounded autonomy, data security, tender
evidence, delivery, Telegram, operations, observability and CEO facts. This does
not replace the master 325-requirement matrix.

## Verification

CI evaluates the 100 contracts against cases actually executed in the full suite.
Every parameterized case for a selected contract must pass. Missing, failed and
skipped cases never count as a pass. JUnit and the per-criterion JSON report are
uploaded as `quality-evidence` on the exact commit's CI run. Evidence is labeled
isolated regression tests, not live provider or production verification.

```bash
pytest -q --junitxml=test-results.xml
python scripts/check_quality_criteria.py --junit test-results.xml
```

Runtime evidence remains in `/health`, `/ready`, `/api/observability/agents`,
Task/AgentRun/AgentToolCall history and delivered owner notifications. Simulated
provider tests and strategic checkpoints do not establish business completion.
`qualified_owner_handoffs` counts qualified organizations with delivered reports.

## Release changes

- All 21 agents have a common public-research action without an approval round
  trip or invocation of their default domain handler.
- Linked-page traversal replaces homepage-only scheduled lead verification.
  Per-page URLs, hashes, partial results and failures persist. Legacy tasks work.
- The real public scout name `lead_scout` is added to the legacy crawler allowlist;
  the historical `public_lead_scout` alias stays compatible.
- Redirect destinations and HTTP statuses are checked; error pages are not proof.
- Tasks support `/api/tasks?limit=100` followed by `before_id=<last-id>`.
  Unbounded legacy responses remain compatible; clients must opt into pagination.
- CI includes the 100-contract gate and strict typing of crawler and assessor.

## Remaining work

No CAPTCHA, MFA, login, robots, access-control, consent, approval, rate-limit,
kill-switch or financial safeguard is disabled. No prospect outreach is granted.
Page/depth/time/size budgets prevent loops and excessive load. Web text is
untrusted and the research route sends no content to external AI providers.
Perplexity authorization, Twenty credentials, process-level cancellation,
restore drills and missing procurement executors are not solved by this catalog.
Other limitations remain in `CAPABILITY_AUDIT.md`. No "all agents at maximum" or
"100% production complete" claim is made. Final runtime evidence in the release
PR must identify the deployed SHA and actual task receipts.

Local pre-release evidence: 554 tests, all targeted strict-mypy checks, Ruff and
32 agent intent evals passed. All 100 criterion contracts passed from that JUnit
run. Optional independent Claude review timed out after 100 seconds; it is not
recorded as approval. Full CI, migrations and runtime checks remain release gates.
