from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.prompt_registry import (
    PromptDeployment,
    PromptRelease,
    deployment_catalog,
    select_prompt,
)


def _deployment() -> PromptDeployment:
    return PromptDeployment(
        stable=PromptRelease("test", "1.0.0", "stable content", "test_schema"),
        candidate=PromptRelease("test", "2.0.0", "candidate content", "test_schema"),
    )


def test_prompt_rollout_is_deterministic_and_immediately_rollbackable():
    deployment = _deployment()
    subject_a = {"request": "example", "facts": {"a": 1, "b": 2}}
    subject_b = {"facts": {"b": 2, "a": 1}, "request": "example"}

    candidate = select_prompt(
        deployment,
        subject=subject_a,
        candidate_rollout_percent=100,
        rollout_seed="test-seed",
    )
    same_candidate = select_prompt(
        deployment,
        subject=subject_b,
        candidate_rollout_percent=100,
        rollout_seed="test-seed",
    )
    rolled_back = select_prompt(
        deployment,
        subject=subject_a,
        candidate_rollout_percent=0,
        rollout_seed="test-seed",
    )

    assert candidate.variant == "candidate"
    assert same_candidate.rollout_bucket == candidate.rollout_bucket
    assert same_candidate.release.digest == candidate.release.digest
    assert rolled_back.variant == "stable"
    assert rolled_back.release.version == "1.0.0"


def test_prompt_metadata_is_immutable_and_never_exposes_content():
    deployment = _deployment()
    catalog = deployment_catalog({"test_operation": deployment}, candidate_rollout_percent=15)
    serialized = repr(catalog)

    assert catalog["candidate_rollout_percent"] == 15
    assert "stable content" not in serialized
    assert "candidate content" not in serialized
    assert catalog["deployments"][0]["candidate"]["version"] == "2.0.0"
    assert catalog["deployments"][0]["stable"]["digest"].startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        deployment.stable.version = "changed"  # type: ignore[misc]


def test_llm_adapters_report_selected_prompt_even_without_credentials(monkeypatch):
    from app import llm
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "perplexity_api_key", "")
    monkeypatch.setattr(settings, "prompt_candidate_rollout_percent", 0)

    results = [
        llm.OpenAIResponsesAdvisor().review({"business_health": 90}),
        llm.AnthropicMessagesAdvisor().analyze_request("test", {}, {}),
        llm.PerplexityAgentCoach().coach_agents({"runs": 3}),
        llm.PerplexityAgentCoach().research_evolution({"sources": 2}),
    ]

    assert [result["prompt"]["name"] for result in results] == [
        "business_review",
        "request_analysis",
        "agent_coaching",
        "evolution_research",
    ]
    for index, result in enumerate(results):
        assert result["prompt"]["variant"] == "stable"
        expected_version = "1.1.0" if index == 2 else "1.0.0"
        assert result["prompt"]["version"] == expected_version
        assert "content" not in result["prompt"]


def test_prompt_catalog_api_is_manager_only_and_content_free(client):
    denied = client.get("/api/ai/prompts", headers={"X-Role": "viewer"})
    assert denied.status_code == 403

    response = client.get("/api/ai/prompts", headers={"X-Role": "manager"})
    assert response.status_code == 200
    result = response.json()
    assert result["candidate_rollout_percent"] == 0
    assert {item["operation"] for item in result["deployments"]} == {
        "agent_coaching",
        "business_review",
        "evolution_research",
        "request_analysis",
    }
    assert "You are" not in response.text
    assert "content" not in response.text
