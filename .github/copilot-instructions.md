# CleaningAIOS repository instructions

Follow the root `AGENTS.md` and the architecture documents it names. Keep changes
small, production-connected and backward compatible. Never bypass the deterministic
policy engine, owner approvals, RBAC, audit log, idempotency, consent, suppression,
unsubscribe or rate limits.

When reviewing a change, report only concrete defects. Give special attention to
duplicate side effects, stale approvals, fabricated completion, secret/PII leakage,
unsafe paths or URLs, missing timeouts, retry storms and incomplete regression tests.
Require `pytest`, Ruff, the local agent eval runner and relevant Docker/API smoke
checks before considering a production change complete.
