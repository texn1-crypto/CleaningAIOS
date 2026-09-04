# Prompt releases and agent replay

## Versioned prompt deployments

Every OpenAI, Anthropic and Perplexity advisory operation selects an immutable
prompt release from `app/llm.py`. A release has a semantic version, response-schema
name and SHA-256 digest. Provider results and therefore persisted `AgentRun.output`
include only this metadata; the prompt text is intentionally absent from the
management API and logs.

`GET /api/ai/prompts` is manager-only and reports stable/candidate metadata plus
the configured candidate percentage. Selection is deterministic for the canonical
input and `PROMPT_ROLLOUT_SEED`, so a retry remains in the same cohort.

The production default is:

```text
PROMPT_CANDIDATE_ROLLOUT_PERCENT=0
```

Increase it only after the candidate passes the fixed agent eval dataset. Use a
small cohort first. To roll back, set the value to `0` and restart the services;
the immutable stable release remains available and no database rewrite is needed.

## Safe replay

`POST /api/agent-runs/{run_id}/replay` requires a manager identity and an
`Idempotency-Key` header. It accepts only terminal runs. The server recursively
removes stored credential/token/password fields and all old approval artifacts,
creates a new one-attempt task with a new correlation ID, then blocks it behind a
fresh `agent_replay` owner approval. Requesting replay never executes the agent.

Approving the exact replay only moves the task to `queued`; normal worker policy,
audit and idempotency controls still apply. The old approval is never reused. A
hashed idempotency key is stored in `agent_replay_requests`, preventing duplicate
replay tasks without persisting the caller's raw key. Replaying a running run or a
replay-of-replay is rejected.
