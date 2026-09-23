from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import func, select

from app import agent_tools, crawl4ai_client, lead_reports
from app.agents import AGENTS
from app.config import settings
from app.db import SessionLocal
from app.models import AgentToolCall, ApprovalRequest, OutboundMessage
from tests.test_crawl4ai import _Response, _configure


def _site(monkeypatch, pages):
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "crawl4ai_timeout_seconds", 30)
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json):
            target = json["urls"][0]
            calls.append(target)
            assert json["crawler_config"]["params"]["check_robots_txt"] is True
            return _Response({"success": True, "results": [pages[target]]})

    monkeypatch.setattr(crawl4ai_client.httpx, "Client", Client)
    return calls


def _page(url, links=(), **values):
    return {"url": url, "status_code": 200, "success": True,
            "markdown": "Public company evidence", "links": {"internal": [{"href": x} for x in links]}, **values}


def test_research_follows_real_links_and_keeps_per_page_hashes(monkeypatch):
    calls = _site(monkeypatch, {
        "https://example.com/": _page("https://example.com/", ["/about", "/about#repeat"]),
        "https://example.com/about": _page("https://example.com/about", ["/contacts", "/"]),
        "https://example.com/contacts": _page("https://example.com/contacts"),
    })
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert calls == ["https://example.com/", "https://example.com/about", "https://example.com/contacts"]
    assert [page["depth"] for page in result["pages"]] == [0, 1, 2]
    for page in result["pages"]:
        assert page["content_sha256"] == "sha256:" + hashlib.sha256(page["markdown"].encode()).hexdigest()
        assert page["untrusted_external_data"] is True
        assert page["automatic_action_allowed"] is False
    assert result["pages_succeeded"] == 3


def test_research_skips_external_query_active_and_download_links(monkeypatch):
    calls = _site(monkeypatch, {"https://example.com/": _page("https://example.com/", [
        "https://evil.example/x", "https://user:secret@example.com/x", "/?token=secret", "/logout",
        "javascript:alert(1)", "/checkout", "/archive.zip", "http://example.com/x", "https://127.0.0.1/",
    ])})
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert calls == ["https://example.com/"]
    assert result["stop_reason"] == "frontier_exhausted"


def test_research_rechecks_dns_for_every_page(monkeypatch):
    calls = _site(monkeypatch, {
        "https://example.com/": _page("https://example.com/", ["/about"]),
    })
    answers = iter([{"93.184.216.34"}] * 3 + [{"10.0.0.1"}])
    monkeypatch.setattr(crawl4ai_client, "_resolved_public_addresses", lambda *args: next(answers))
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert len(calls) == 1
    assert result["pages"][1]["failure_category"] == "target_denied"
    assert result["partial"] is True


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_research_stops_on_root_access_failure_without_retry(monkeypatch, status):
    calls = _site(monkeypatch, {"https://example.com/": _page("https://example.com/", ["/about"], status_code=status)})
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert calls == ["https://example.com/"]
    assert result["success"] is False
    assert result["pages"][0]["markdown"] == ""


def test_research_rejects_cross_origin_redirect_content(monkeypatch):
    _site(monkeypatch, {"https://example.com/": _page("https://example.com/", redirected_url="https://other.example/", markdown="not evidence")})
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert result["success"] is False
    assert "not evidence" not in json.dumps(result)


def test_research_checks_redirected_url_not_just_requested_url(monkeypatch):
    _site(monkeypatch, {"https://example.com/": _page("https://example.com/", redirected_url="https://internal.local/", markdown="private")})
    with pytest.raises(crawl4ai_client.Crawl4AIPolicyDenied):
        crawl4ai_client.crawl_public_page({"url": "https://example.com/"})


def test_research_stops_at_page_budget(monkeypatch):
    calls = _site(monkeypatch, {"https://example.com/": _page("https://example.com/", ["/about"])})
    result = crawl4ai_client.research_public_site({"url": "https://example.com/", "max_pages": 1})
    assert len(calls) == 1
    assert result["stop_reason"] == "page_budget"
    assert result["partial"] is True


def test_research_stops_at_depth_budget(monkeypatch):
    calls = _site(monkeypatch, {
        "https://example.com/": _page("https://example.com/", ["/about"]),
        "https://example.com/about": _page("https://example.com/about", ["/contacts"]),
    })
    result = crawl4ai_client.research_public_site({"url": "https://example.com/", "max_depth": 1})
    assert len(calls) == 2
    assert result["pages_succeeded"] == 2
    assert result["stop_reason"] == "depth_budget"
    assert result["partial"] is True


@pytest.mark.parametrize("extra", [
    {"max_pages": True}, {"max_pages": 0}, {"max_pages": 6}, {"max_depth": 3},
    {"max_depth": "2"}, {"max_chars": 999}, {"max_chars": 3001}, {"js_code": "x"},
    {"url": "https://example.com/?token=secret"}, {"cookies": []}, {"proxy": "x"},
])
def test_research_validates_budgets_and_inputs_before_fetch(monkeypatch, extra):
    calls = _site(monkeypatch, {})
    with pytest.raises(crawl4ai_client.Crawl4AIPolicyDenied):
        crawl4ai_client.research_public_site({"url": "https://example.com/", **extra})
    assert calls == []


def test_research_bounds_each_page_and_prioritizes_evidence_links(monkeypatch):
    calls = _site(monkeypatch, {
        "https://example.com/": _page("https://example.com/", ["/blog", "/contacts"], markdown="x" * 5000),
        "https://example.com/contacts": _page("https://example.com/contacts", markdown="x" * 5000),
    })
    result = crawl4ai_client.research_public_site({"url": "https://example.com/", "max_pages": 2, "max_chars": 1000})
    assert calls[-1].endswith("/contacts")
    assert all(page["content_chars"] == 1000 and page["truncated"] for page in result["pages"])


def test_research_preserves_partial_result_after_time_budget(monkeypatch):
    calls = _site(monkeypatch, {"https://example.com/": _page("https://example.com/", ["/about"])})
    ticks = iter([0, 0, 29])
    monkeypatch.setattr(crawl4ai_client.time, "monotonic", lambda: next(ticks))
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert len(calls) == 1
    assert result["pages_succeeded"] == 1
    assert result["stop_reason"] == "time_budget"


@pytest.mark.parametrize("agent", sorted(AGENTS))
def test_every_registered_agent_can_research_without_approval_or_domain_side_effects(client, monkeypatch, agent):
    monkeypatch.setattr(agent_tools, "research_public_site", lambda args: {
        "success": True, "partial": False, "pages_succeeded": 1,
        "pages": [{"resolved_url": args["url"], "content_sha256": "sha256:test"}],
    })
    monkeypatch.setattr(AGENTS[agent], "execute", lambda *args: pytest.fail("Read-only research reached domain writer"))
    with SessionLocal() as db:
        approvals = db.scalar(select(func.count()).select_from(ApprovalRequest))
        outbound = db.scalar(select(func.count()).select_from(OutboundMessage))
    task = client.post("/api/tasks", json={"title": "Public research " + agent, "agent_type": agent,
        "max_attempts": 1, "payload": {"action": "public_web_research", "autonomy_action": "public_web_research",
        "research": {"url": "https://example.com/"}}}).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    assert completed["status"] == "done"
    assert completed["result"]["pages_succeeded"] == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ApprovalRequest)) == approvals
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == outbound
        receipt = db.scalar(select(AgentToolCall).where(AgentToolCall.task_id == task["id"]))
        assert receipt.status == "succeeded"
        assert receipt.input_digest.startswith("sha256:")


def test_research_unavailable_is_not_a_completed_task(client, monkeypatch):
    monkeypatch.setattr(agent_tools, "research_public_site", lambda args: {"success": False, "pages_succeeded": 0})
    task = client.post("/api/tasks", json={"title": "Unavailable public research", "agent_type": "research",
        "max_attempts": 1, "payload": {"action": "public_web_research", "research": {"url": "https://example.com/"}}}).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()
    assert result["status"] == "blocked"
    assert result["result"]["status"] == "unavailable"


def test_research_tool_allowlist_exactly_matches_registered_roles():
    assert agent_tools.READ_ONLY_TOOLS["web.public_research"].allowed_agents == set(AGENTS)


def test_public_research_does_not_override_a_protected_action(client, monkeypatch):
    monkeypatch.setattr(agent_tools, "research_public_site", lambda args: pytest.fail("protected task reached network"))
    task = client.post("/api/tasks", json={"title": "Protected action stays protected", "agent_type": "finance",
        "payload": {"action": "public_web_research", "action_kind": "financial",
        "research": {"url": "https://example.com/"}}}).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()
    assert result["status"] == "blocked"


def test_www_redirect_is_validated_but_other_subdomains_are_not_followed(monkeypatch):
    calls = _site(monkeypatch, {
        "https://example.com/": _page("https://example.com/", ["https://other.example.com/"], redirected_url="https://www.example.com/"),
    })
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert result["success"] is True
    assert len(calls) == 1


def test_malformed_link_does_not_discard_valid_page(monkeypatch):
    calls = _site(monkeypatch, {"https://example.com/": _page("https://example.com/", ["https://[invalid/x"])})
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert result["success"] is True
    assert len(calls) == 1


def test_empty_page_is_not_successful_research(monkeypatch):
    _site(monkeypatch, {"https://example.com/": _page("https://example.com/", markdown="")})
    result = crawl4ai_client.research_public_site({"url": "https://example.com/"})
    assert result["success"] is False
    assert result["pages"][0]["failure_category"] == "empty_content"


def test_task_listing_keyset_pages_are_bounded_and_do_not_overlap(client):
    for i in range(5):
        client.post("/api/tasks", json={"title": f"Pagination {i}", "agent_type": "research"})
    first = client.get("/api/tasks?limit=2").json()
    second = client.get(f"/api/tasks?limit=2&before_id={first[-1]['id']}").json()
    assert len(first) == len(second) == 2
    assert max(row["id"] for row in second) < min(row["id"] for row in first)
    assert client.get("/api/tasks?limit=501").status_code == 422
    assert client.get("/api/tasks?before_id=0").status_code == 422


def test_deep_candidate_verification_uses_matching_child_page_provenance(monkeypatch, tmp_path):
    from app.lead_outcomes import verify_existing_management_company_candidate
    from tests.test_lead_outcomes import _management_company, _session_factory

    monkeypatch.setattr(lead_reports.settings, "document_storage_path", str(tmp_path))
    with _session_factory()() as db:
        company = _management_company("УК Северный Дом", "https://example.com/", "")
        db.add(company)
        db.flush()
        result = verify_existing_management_company_candidate(db, {
            "record_id": company.id, "candidate_url": "https://example.com/", "notify_owner": False,
            "read_only_tool_results": [{"name": "web.public_research", "result": {
                "requested_url": "https://example.com/", "pages": [
                    {"success": True, "resolved_url": "https://example.com/", "markdown": "Welcome"},
                    {"success": True, "resolved_url": "https://example.com/about", "markdown": "УК Северный Дом", "content_sha256": "sha256:child"},
                ],
            }}],
        })
        assert result["status"] == "owner_review"
        assert company.data["provenance"][-1]["source_url"] == "https://example.com/about"
        assert company.data["provenance"][-1]["content_sha256"] == "sha256:child"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
