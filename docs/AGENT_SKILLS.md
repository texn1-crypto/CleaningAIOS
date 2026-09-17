# Agent skill routing

CleaningAIOS uses a versioned, project-owned registry to assign relevant skills
to every runtime agent. The registry is defined in `app/skill_registry.py` and is
independent of personal home-directory files, so production does not depend on a
developer workstation or load arbitrary instructions at runtime.

## Runtime contract

- `AgentRuntime` attaches a trusted `agent_skill_context` to each execution.
- Caller-supplied skill context is replaced, not trusted.
- Every successful `AgentRun` records `agent_skill_routing` evidence, the
  registry version, and the skill names applied.
- LLM advisory calls receive the matching compact skill context for CEO review,
  request analysis and LLM Council, agent coaching, evolution research, and lead
  discovery.
- Skill context never grants tools, credentials, approval authority, or external
  side effects. Existing policy, approval, audit, and idempotency controls remain
  authoritative.

The authenticated operational catalog is available at `GET /api/agent-skills`.

## Assignment summary

| Agent | Skills |
| --- | --- |
| orchestrator | cleaning-orchestration, ai-evals |
| research | research |
| tender | russian-cleaning-tenders, cleaning-unit-economics |
| sales | cleaning-lead-qualification, copywriting, prospecting |
| marketing | content-strategy, brand, copywriting |
| hr | recruiting-pipeline |
| finance | cleaning-unit-economics |
| ceo | cleaning-ceo-review, cleaning-unit-economics, gtm-product-led-growth |
| growth_officer | gtm-product-led-growth, content-strategy, cleaning-unit-economics |
| meta_brain | ai-evals, incident-response |
| evolution_researcher | research, ai-evals |
| lead_scout | prospecting, cleaning-lead-qualification, research |
| lead_coordinator | cleaning-orchestration, cleaning-lead-qualification |
| management_lead_scout | prospecting, cleaning-lead-qualification |
| commercial_lead_scout | prospecting, cleaning-lead-qualification |
| tender_lead_scout | russian-cleaning-tenders, prospecting, cleaning-lead-qualification |
| social_lead_scout | prospecting, cleaning-lead-qualification, content-strategy |
| system_admin | incident-response, ai-evals |
| request_analyst | cleaning-orchestration, ai-evals |
| copywriter | copywriting, brand |
| creative | brand, content-strategy |

Development-only skills such as codebase-memory and security scanners remain
tools for Codex/Claude review. They are intentionally not exposed to autonomous
runtime agents. The `to-spec` workflow remains explicit-only.
