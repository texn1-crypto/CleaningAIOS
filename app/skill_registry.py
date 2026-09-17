from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SKILL_REGISTRY_VERSION = "1.0.0"


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    purpose: str
    guardrails: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "guardrails": list(self.guardrails),
        }


_COMMON_GUARDRAILS = (
    "advisory_only",
    "preserve_owner_approval",
    "use_verified_evidence",
    "no_secrets_or_personal_data",
)


SKILLS: dict[str, SkillDefinition] = {
    "ai-evals": SkillDefinition(
        name="ai-evals",
        purpose="Measure agent quality with reproducible tests and outcome evidence.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "brand": SkillDefinition(
        name="brand",
        purpose="Keep customer-facing language and creative work consistent with the brand.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "cleaning-ceo-review": SkillDefinition(
        name="cleaning-ceo-review",
        purpose="Review CleaningAIOS profitability, growth, operations, cash, and agent performance.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "cleaning-lead-qualification": SkillDefinition(
        name="cleaning-lead-qualification",
        purpose="Qualify cleaning leads using fit, demand, timing, economics, consent, and safety.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "cleaning-orchestration": SkillDefinition(
        name="cleaning-orchestration",
        purpose="Route work while preserving approvals, audit evidence, and idempotency.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "cleaning-unit-economics": SkillDefinition(
        name="cleaning-unit-economics",
        purpose="Evaluate cleaning-site pricing, margin, budget, and cash-flow risk.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "content-strategy": SkillDefinition(
        name="content-strategy",
        purpose="Plan content pillars, funnel coverage, production, distribution, and refresh cycles.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "copywriting": SkillDefinition(
        name="copywriting",
        purpose="Create persuasive, accurate customer-facing copy with clear calls to action.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "gtm-product-led-growth": SkillDefinition(
        name="gtm-product-led-growth",
        purpose="Evaluate acquisition, activation, retention, and expansion growth motions.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "incident-response": SkillDefinition(
        name="incident-response",
        purpose="Triage incidents, preserve evidence, communicate impact, and plan recovery.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "prospecting": SkillDefinition(
        name="prospecting",
        purpose="Find and prioritize suitable prospects using verifiable public business evidence.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "recruiting-pipeline": SkillDefinition(
        name="recruiting-pipeline",
        purpose="Manage candidate sourcing, screening, interviews, and hiring pipeline status.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "research": SkillDefinition(
        name="research",
        purpose="Research questions with high-trust sources and explicit evidence provenance.",
        guardrails=_COMMON_GUARDRAILS,
    ),
    "russian-cleaning-tenders": SkillDefinition(
        name="russian-cleaning-tenders",
        purpose="Analyze Russian cleaning tenders, bid fitness, risks, and owner review needs.",
        guardrails=_COMMON_GUARDRAILS,
    ),
}


AGENT_SKILLS: dict[str, tuple[str, ...]] = {
    "orchestrator": ("cleaning-orchestration", "ai-evals"),
    "research": ("research",),
    "tender": ("russian-cleaning-tenders", "cleaning-unit-economics"),
    "sales": ("cleaning-lead-qualification", "copywriting", "prospecting"),
    "marketing": ("content-strategy", "brand", "copywriting"),
    "hr": ("recruiting-pipeline",),
    "finance": ("cleaning-unit-economics",),
    "ceo": (
        "cleaning-ceo-review",
        "cleaning-unit-economics",
        "gtm-product-led-growth",
    ),
    "growth_officer": (
        "gtm-product-led-growth",
        "content-strategy",
        "cleaning-unit-economics",
    ),
    "meta_brain": ("ai-evals", "incident-response"),
    "evolution_researcher": ("research", "ai-evals"),
    "lead_scout": ("prospecting", "cleaning-lead-qualification", "research"),
    "lead_coordinator": ("cleaning-orchestration", "cleaning-lead-qualification"),
    "management_lead_scout": ("prospecting", "cleaning-lead-qualification"),
    "commercial_lead_scout": ("prospecting", "cleaning-lead-qualification"),
    "tender_lead_scout": (
        "russian-cleaning-tenders",
        "prospecting",
        "cleaning-lead-qualification",
    ),
    "social_lead_scout": (
        "prospecting",
        "cleaning-lead-qualification",
        "content-strategy",
    ),
    "system_admin": ("incident-response", "ai-evals"),
    "request_analyst": ("cleaning-orchestration", "ai-evals"),
    "copywriter": ("copywriting", "brand"),
    "creative": ("brand", "content-strategy"),
}


def skills_for_agent(agent_type: str) -> tuple[SkillDefinition, ...]:
    """Return the immutable, project-owned skill assignment for an agent."""

    return tuple(SKILLS[name] for name in AGENT_SKILLS.get(agent_type, ()))


def agent_skill_context(agent_type: str) -> dict[str, Any]:
    """Build compact JSON-safe guidance without loading user files or secrets."""

    return {
        "registry_version": SKILL_REGISTRY_VERSION,
        "source": "cleaningaios_skill_registry",
        "agent_type": agent_type,
        "advisory_only": True,
        "skills": [skill.as_dict() for skill in skills_for_agent(agent_type)],
    }


def with_agent_skill_context(
    subject: Mapping[str, Any],
    agent_type: str,
) -> dict[str, Any]:
    """Attach trusted registry context, replacing any caller-supplied value."""

    return {
        **dict(subject),
        "agent_skill_context": agent_skill_context(agent_type),
    }


def skill_routing_evidence(agent_type: str) -> dict[str, Any]:
    return {
        "type": "agent_skill_routing",
        "registry_version": SKILL_REGISTRY_VERSION,
        "agent_type": agent_type,
        "skills": [skill.name for skill in skills_for_agent(agent_type)],
        "advisory_only": True,
    }


def skill_catalog() -> dict[str, Any]:
    """Return a safe operational view for dashboards and support checks."""

    return {
        "registry_version": SKILL_REGISTRY_VERSION,
        "advisory_only": True,
        "skills": {
            name: definition.as_dict()
            for name, definition in sorted(SKILLS.items())
        },
        "agents": {
            agent_type: list(skill_names)
            for agent_type, skill_names in sorted(AGENT_SKILLS.items())
        },
    }
