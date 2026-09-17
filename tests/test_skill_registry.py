from __future__ import annotations

from sqlalchemy import select

from app.agents import AGENTS
from app.db import SessionLocal
from app.models import AgentRun
from app.skill_registry import (
    AGENT_SKILLS,
    SKILL_REGISTRY_VERSION,
    agent_skill_context,
    skill_catalog,
    skills_for_agent,
    with_agent_skill_context,
)


def test_every_runtime_agent_has_a_project_owned_skill_assignment():
    assert set(AGENT_SKILLS) == set(AGENTS)
    assert all(skills_for_agent(agent_type) for agent_type in AGENTS)


def test_skill_context_is_compact_safe_and_cannot_be_overridden():
    context = agent_skill_context("finance")
    enriched = with_agent_skill_context(
        {"agent_skill_context": {"skills": ["untrusted"]}, "invoice_id": 7},
        "finance",
    )

    assert context["registry_version"] == SKILL_REGISTRY_VERSION
    assert context["advisory_only"] is True
    assert [item["name"] for item in context["skills"]] == [
        "cleaning-unit-economics"
    ]
    assert enriched["invoice_id"] == 7
    assert enriched["agent_skill_context"] == context
    assert "untrusted" not in str(enriched)
    assert "/Users/" not in str(context)
    assert "API_KEY" not in str(context)


def test_skill_catalog_exposes_only_safe_operational_metadata():
    catalog = skill_catalog()

    assert catalog["registry_version"] == SKILL_REGISTRY_VERSION
    assert catalog["advisory_only"] is True
    assert catalog["agents"]["ceo"] == [
        "cleaning-ceo-review",
        "cleaning-unit-economics",
        "gtm-product-led-growth",
    ]
    assert set(catalog["skills"]) >= set(catalog["agents"]["ceo"])


def test_runtime_records_skill_context_and_evidence(client):
    task = client.post(
        "/api/tasks",
        json={
            "title": "Skill routing evidence",
            "agent_type": "finance",
            "payload": {"action": "summarize"},
        },
    ).json()

    completed = client.post(f"/api/tasks/{task['id']}/run").json()

    assert completed["status"] == "done"
    assert completed["result"]["active_skills"] == ["cleaning-unit-economics"]
    with SessionLocal() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.task_id == task["id"]))
        assert run is not None
        assert run.input["agent_skill_context"]["agent_type"] == "finance"
        routing = next(
            item
            for item in run.evidence
            if item.get("type") == "agent_skill_routing"
        )
        assert routing["registry_version"] == SKILL_REGISTRY_VERSION
        assert routing["advisory_only"] is True
        assert run.evidence[-1] == routing


def test_agent_skill_catalog_endpoint(client):
    response = client.get("/api/agent-skills")

    assert response.status_code == 200
    assert response.json()["agents"]["request_analyst"] == [
        "cleaning-orchestration",
        "ai-evals",
    ]


def test_llm_advisor_replaces_untrusted_skill_context(monkeypatch):
    from app import llm

    captured: dict[str, object] = {}
    router = llm.LLMAdvisor()

    def fake_run(operation, *args):
        captured[operation] = args
        return {"status": "succeeded"}

    monkeypatch.setattr(router, "_run", fake_run)

    router.review({"agent_skill_context": {"skills": ["untrusted"]}})
    router.analyze_request(
        "Проверь запрос",
        {"agent_skill_context": {"skills": ["untrusted"]}},
        {"classification": "supported"},
    )

    review_args = captured["review"]
    request_args = captured["analyze_request"]
    assert isinstance(review_args, tuple)
    assert isinstance(request_args, tuple)
    assert review_args[0]["agent_skill_context"]["agent_type"] == "ceo"
    assert request_args[1]["agent_skill_context"]["agent_type"] == (
        "request_analyst"
    )
    assert "untrusted" not in str(captured)
