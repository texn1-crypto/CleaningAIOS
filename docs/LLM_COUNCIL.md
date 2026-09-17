# LLM Council for owner requests

## Purpose

LLM Council improves an owner's actionable request before execution without giving
any model authority over the business. The council challenges assumptions, records
disagreement and produces a bounded execution brief. Deterministic code remains the
source of truth for routing, capability support, RBAC, approvals, audit, consent,
suppression, rate limits and completion evidence.

Menu, help, acknowledgement and read-only status requests stay deterministic and do
not pay the latency or token cost of a council review. Every request classified as a
`task` passes through the council path.

## Request path

```text
owner request
    -> credential redaction
    -> deterministic intent + capability assessment
    -> independent configured LLM members (parallel, advisory-only)
    -> deterministic council synthesis
    -> council-refined execution brief
    -> normal Task / Agent Runtime / policy / evidence gates
```

The business members are OpenAI Responses, Gemini Generate Content and Anthropic
Messages. `LLM_PROVIDER=auto` uses every configured member up to
`LLM_COUNCIL_MAX_MEMBERS`. Pinning one provider intentionally produces a degraded
single-member result. Perplexity remains isolated to public-source research and
aggregate agent coaching; it does not receive private owner requests.

## Master prompt

The immutable `request_analysis` prompt release `3.0.0` makes each provider an
independent council member. It requires anti-sycophancy, capability evidence,
prompt-injection resistance and an explicit refusal to turn advice into execution
authority. The prompt digest and version are attached to every member result.

The final `refined_prompt` is not model-written. It is assembled locally from:

- the credential-redacted owner objective;
- deterministic route and capability classification;
- fixed safety constraints;
- deterministic acceptance criteria and validation steps;
- low-cardinality council metadata: successful members, quorum and disagreement.

Raw model prose is never interpolated into the executable prompt. Model-proposed
capability names are normalized to inert labels and remain advisory.

## Failure behavior

- Two successful members meet the default quorum.
- One successful member is `degraded`; the request may continue through normal
  deterministic gates, and the degraded state is visible in the analysis result.
- Zero successful members uses `deterministic_fallback`; provider absence or failure
  never fabricates a council consensus and never blocks a safe deterministic path.
- Council votes cannot change an approval requirement, authorize an action or mark a
  task complete.

## Configuration

```text
LLM_COUNCIL_ENABLED=true
LLM_COUNCIL_MIN_MEMBERS=2
LLM_COUNCIL_MAX_MEMBERS=3
LLM_PROVIDER=auto
```

Provider credentials stay in runtime secrets. Do not place them in prompts, task
payloads, logs, Git or council evidence. Activation is verified through the existing
integration status plus a request-analysis response whose `llm_analysis` reports the
attempted providers, member count and quorum state.

For a local workstation, already-authorized Claude Code and Codex accounts may be
exposed by OmniRoute's Anthropic- and OpenAI-compatible loopback endpoints. Use only
`localhost`, `127.0.0.1` or `::1` over HTTP; non-loopback production endpoints still
require HTTPS. Each independently authenticated upstream counts as one member; aliases
or several model names for the same upstream do not create extra votes. A second
provider is required for the default quorum.

## Validation contract

Tests must prove that configured members are invoked independently, deterministic
classification remains authoritative during disagreement, a redacted execution brief
is always available for actionable requests, and no provider credential or raw prompt
content appears in audit metadata.
