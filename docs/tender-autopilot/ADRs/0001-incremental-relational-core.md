# ADR-0001: Incremental relational core with immutable decision snapshots

- Status: accepted
- Date: 2026-09-06

## Context

CleaningAIOS already has PostgreSQL, SQLAlchemy, Alembic, Task workflow,
transactional outbox, agent runs, audit and protected approvals. Replacing these
with a second workflow/database would split truth and increase operational risk.
The legacy tender evaluator also stores a mutable JSON result and uses `float`, so
an approved decision cannot yet be reconstructed exactly.

## Decision

Keep the modular monolith and relational system of record. Add append-only
`TenderAssessmentSnapshot` rows containing canonical inputs, results, actor,
rules version and SHA-256 hashes. Use Decimal calculations and evidence references
bound to persisted tender-document checksums. Reuse the existing participation
task and Approval Engine, binding its payload to the snapshot hash.

Do not introduce Temporal, a vector database or a separate microservice before a
measured need. They remain possible adapters behind current boundaries.

## Consequences

- Identical requests are idempotent and auditable.
- Changed documents or inputs create a distinct decision and approval context.
- PostgreSQL prevents update/delete of snapshots.
- Legacy `BusinessRecord.data` remains a current-state projection for compatibility.
- Automatic extraction and external execution remain explicit future capabilities,
  not implied by the snapshot endpoint.
