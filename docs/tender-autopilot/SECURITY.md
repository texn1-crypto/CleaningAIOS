# Security and threat model

| Threat | Boundary / control | Remaining work |
|---|---|---|
| Prompt injection in tender docs | Documents are untrusted evidence; no tool instructions | Add adversarial golden corpus |
| Document substitution | Exact document ID + SHA-256 binding; snapshot hash | Signed object manifest |
| Approval replay | Versioned, expiring Approval Engine; snapshot hash in task | Per-action short-lived execution token |
| Stop-price bypass | Backend deterministic calculation, no LLM arithmetic | Enforce again inside future bid adapter |
| SSRF | Safe URL validation and redirect recheck | Provider-specific allowlists |
| Malware/ZIP bomb/path traversal | Storage boundary and size limit exist | Sandbox, AV, archive limits and corpus |
| Secret/PII leakage to AI | Provider scopes and redaction policy | Document-level `LOCAL_ONLY` enforcement |
| Account/session theft | No autonomous ETP session today | Isolated worker, vault, MFA human takeover |
| Supplier/invoice fraud | Evidence-bound quote plus immutable masked company-profile fingerprint | Automated registry verification and bank-detail change hold |
| Duplicate external action | Outbox/consumer receipts | Provider idempotency + external receipt registry |
| Unauthorized submit/sign/pay | Separate owner approvals, no executor connected | Separation of duties and production adapter gates |

Fail-closed is mandatory: adapter unavailable, evidence conflict, unknown legal
rule, stale quote, stale document or expired approval stops execution and produces
an actionable exception. Read-only monitoring may continue under a kill switch;
write actions may not.

## Global external-actions kill switch

The database-backed `global_external_actions` control is the final policy gate for
every protected action, including submission, signing/contract, payments, bulk
outreach, social publication and final HR decisions. A manager can inspect it at
`GET /api/safety/external-actions-kill-switch`; only the owner can change it with
`PUT /api/safety/external-actions-kill-switch`. Activation requires a reason.

When active, the Decision Engine blocks the task before creating or accepting an
approval. The block is persisted in task transitions, the audit log and the
transactional event bus as `policy.execution_blocked`. Read-only tasks continue.
Repeated writes of the same state are idempotent and do not create duplicate audit
or event records. Deactivating the switch never approves or resumes an old task:
the operator must create a fresh task, and all normal owner approvals still apply.

## Protected capability flags

`CapabilityFlag` adds a separate persisted gate for every action kind known to the
Approval Engine. A missing or disabled flag blocks Orchestrator dispatch before an
approval is created or accepted. The global kill switch is evaluated first and
remains the final emergency stop; an enabled capability still requires its normal,
resource-bound owner approval.

Managers can inspect the complete registry at `GET /api/safety/capability-flags`.
Only the owner may change a known flag with
`PUT /api/safety/capability-flags/{capability_key}` and a reason. Every real change
increments the version and writes audit/outbox evidence; identical retries do not.
Initial flags are explicitly persisted as enabled to preserve existing approval-only
workflows. New/unknown protected capabilities have no implicit allow state and must
be added to the code-owned registry before use. Coverage of protected actions that
bypass Task/Orchestrator must be verified before the feature-flag requirement can be
called complete.
