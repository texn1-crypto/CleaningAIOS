from __future__ import annotations

import hashlib
from datetime import datetime

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import evolution_research, llm, notifications
from app.db import Base
from app.models import BusinessRecord, ImprovementRequest, OwnerNotification


def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def repository(**overrides):
    value = {
        "full_name": "example/safe-agent-runtime",
        "url": "https://github.com/example/safe-agent-runtime",
        "description": "Observable multi-agent task runtime",
        "language": "Python",
        "topics": ["agents", "evaluation"],
        "stars": 2400,
        "forks": 120,
        "open_issues": 8,
        "updated_at": "2026-08-30T12:00:00Z",
        "license_spdx": "Apache-2.0",
        "license_policy": "pattern_review_allowed",
        "archived": False,
        "readme_excerpt": "Measure agent outcomes and keep an auditable event trail.",
        "readme_sha256": "a" * 64,
    }
    value.update(overrides)
    return value


def test_evolution_research_creates_grounded_improvement_and_daily_pdf(monkeypatch, tmp_path):
    factory = session_factory()
    source = repository()
    monkeypatch.setattr(evolution_research.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(evolution_research.settings, "evolution_research_queries", "agent evaluation stars:>=200")
    monkeypatch.setattr(evolution_research.settings, "owner_telegram_id", "123")
    monkeypatch.setattr(evolution_research.settings, "telegram_bot_token", "configured")
    monkeypatch.setattr(
        evolution_research,
        "collect_github_repositories",
        lambda query, page, limit: (
            [source],
            {
                "query": query,
                "page": page,
                "github_total_count": 100,
                "github_incomplete_results": False,
                "rate_limit_remaining": "50",
            },
        ),
    )
    monkeypatch.setattr(
        evolution_research.llm_advisor,
        "research_evolution",
        lambda snapshot: {
            "status": "succeeded",
            "provider": "perplexity_sonar",
            "model": "sonar-test",
            "summary": "Найдена применимая схема измерения результатов агентов.",
            "findings": [],
            "recommendations": [
                {
                    "title": "Добавить eval-набор маршрутизации",
                    "domain": "agent_learning",
                    "change": "Версионировать проверяемый набор запросов и ожидаемых маршрутов.",
                    "rationale": "Текущая телеметрия измеряет исходы, но не регрессии маршрутизации.",
                    "source_urls": [source["url"]],
                    "validation": "Зафиксировать 20 сценариев и требовать 100% прохождения в CI.",
                    "owner_action_required": False,
                    "owner_action": "",
                }
            ],
        },
    )

    with factory() as db:
        result = evolution_research.run_evolution_research(
            db,
            registered_agents=["orchestrator", "meta_brain", "evolution_researcher"],
            now=datetime(2026, 9, 2, 15, 0, 0),
            notify_owner=True,
        )
        db.commit()
        improvement = db.scalar(select(ImprovementRequest))
        persisted_source = db.scalar(
            select(BusinessRecord).where(BusinessRecord.record_type == "evolution_research_source")
        )
        notification = db.scalar(select(OwnerNotification))

    assert result["status"] == "completed"
    assert result["automatic_code_changes_applied"] is False
    assert result["sources_researched"] == 1
    assert improvement is not None
    assert improvement.source_user == "github_evolution_researcher"
    assert improvement.intent["source_urls"] == [source["url"]]
    assert persisted_source.external_id == "github:example/safe-agent-runtime"
    assert persisted_source.data["automatic_code_import"] is False
    report_path = tmp_path / "reports" / "evolution" / result["report"]["filename"]
    assert report_path.read_bytes().startswith(b"%PDF")
    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == result["report"]["checksum_sha256"]
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(str(report_path)).pages)
    assert "AI Evolution Researcher" in pdf_text
    assert "Добавить eval-набор маршрутизации" in pdf_text
    assert source["url"] in pdf_text
    assert notification.status == "queued"
    assert notification.data["document_sha256"] == result["report"]["checksum_sha256"]


def test_evolution_improvement_rejects_provider_citation_outside_reviewed_batch(monkeypatch, tmp_path):
    factory = session_factory()
    source = repository()
    monkeypatch.setattr(evolution_research.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(evolution_research.settings, "evolution_research_queries", "agent evaluation")
    monkeypatch.setattr(
        evolution_research,
        "collect_github_repositories",
        lambda query, page, limit: ([source], {"query": query, "page": page, "github_total_count": 1, "github_incomplete_results": False, "rate_limit_remaining": "49"}),
    )
    monkeypatch.setattr(
        evolution_research.llm_advisor,
        "research_evolution",
        lambda snapshot: {
            "status": "succeeded",
            "provider": "perplexity_sonar",
            "model": "sonar-test",
            "summary": "Untrusted citation test",
            "findings": [],
            "recommendations": [{
                "title": "Unsafe source",
                "domain": "programming",
                "change": "Copy unknown code",
                "rationale": "Not grounded",
                "source_urls": ["https://attacker.example/repository"],
                "validation": "Run it",
                "owner_action_required": False,
                "owner_action": "",
            }],
        },
    )
    with factory() as db:
        result = evolution_research.run_evolution_research(
            db,
            registered_agents=["evolution_researcher"],
            now=datetime(2026, 9, 2, 15, 0, 0),
        )
        assert result["improvements"] == []
        assert db.scalar(select(ImprovementRequest)) is None


def test_evolution_research_does_not_invent_analysis_without_sources(monkeypatch, tmp_path):
    factory = session_factory()
    monkeypatch.setattr(evolution_research.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(evolution_research.settings, "evolution_research_queries", "missing topic")
    monkeypatch.setattr(
        evolution_research,
        "collect_github_repositories",
        lambda query, page, limit: ([], {"query": query, "page": page, "github_total_count": 0, "github_incomplete_results": False, "rate_limit_remaining": "50"}),
    )

    def unexpected_provider_call(snapshot):
        raise AssertionError("Perplexity must not run without reviewed sources")

    monkeypatch.setattr(evolution_research.llm_advisor, "research_evolution", unexpected_provider_call)
    with factory() as db:
        result = evolution_research.run_evolution_research(
            db,
            registered_agents=["evolution_researcher"],
            now=datetime(2026, 9, 2, 15, 0, 0),
        )
    assert result["status"] == "no_sources"
    assert result["improvements"] == []
    assert (tmp_path / "reports" / "evolution" / result["report"]["filename"]).is_file()


def test_github_collection_is_bounded_and_records_license_without_exposing_token(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            request = httpx.Request("GET", url)
            if url.endswith("/search/repositories"):
                return httpx.Response(
                    200,
                    request=request,
                    headers={"x-ratelimit-remaining": "58"},
                    json={
                        "total_count": 1,
                        "incomplete_results": False,
                        "items": [{
                            "full_name": "example/runtime",
                            "html_url": "https://github.com/example/runtime",
                            "private": False,
                            "archived": False,
                            "description": "safe runtime",
                            "language": "Python",
                            "topics": ["agents"],
                            "stargazers_count": 1000,
                            "forks_count": 50,
                            "open_issues_count": 2,
                            "updated_at": "2026-09-01T00:00:00Z",
                            "license": {"spdx_id": "MIT"},
                        }],
                    },
                )
            return httpx.Response(200, request=request, content=b"# Runtime\nObserved agent execution.")

    monkeypatch.setattr(evolution_research.httpx, "Client", Client)
    monkeypatch.setattr(evolution_research.settings, "github_research_token", "top-secret-token")
    rows, evidence = evolution_research.collect_github_repositories("agents", 1, limit=200)
    assert len(rows) == 1
    assert rows[0]["license_policy"] == "pattern_review_allowed"
    assert calls[0][1]["params"]["per_page"] == 20
    assert "fork:false" in calls[0][1]["params"]["q"]
    assert "top-secret-token" not in str((rows, evidence))


def test_github_collection_wraps_requested_page_to_available_results(monkeypatch):
    search_pages = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            request = httpx.Request("GET", url)
            if url.endswith("/search/repositories"):
                page = kwargs["params"]["page"]
                search_pages.append(page)
                items = [] if page != 1 else [{
                    "full_name": "example/runtime",
                    "html_url": "https://github.com/example/runtime",
                    "private": False,
                    "archived": False,
                    "stargazers_count": 1000,
                    "license": {"spdx_id": "MIT"},
                }]
                return httpx.Response(
                    200,
                    request=request,
                    headers={"x-ratelimit-remaining": "50"},
                    json={"total_count": 4, "incomplete_results": False, "items": items},
                )
            return httpx.Response(200, request=request, content=b"# Runtime")

    monkeypatch.setattr(evolution_research.httpx, "Client", Client)
    monkeypatch.setattr(evolution_research.settings, "github_research_token", "")
    rows, evidence = evolution_research.collect_github_repositories("marketing automation", 5, limit=20)
    assert search_pages == [5, 1]
    assert len(rows) == 1
    assert evidence["requested_page"] == 5
    assert evidence["page"] == 1


def test_owner_pdf_notification_uses_send_document_and_rejects_outside_path(monkeypatch, tmp_path):
    report = tmp_path / "daily.pdf"
    report.write_bytes(b"%PDF-1.4\nreport")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    monkeypatch.setattr(notifications.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(notifications.settings, "telegram_bot_token", "redacted-token")
    sent = {}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            sent.update({"url": url, **kwargs})
            return httpx.Response(200, request=httpx.Request("POST", url), json={"ok": True})

    monkeypatch.setattr(notifications.httpx, "Client", Client)
    row = OwnerNotification(
        idempotency_key="pdf-test",
        channel="telegram",
        recipient="123",
        subject="Daily report",
        body="Attached",
        data={
            "document_path": str(report),
            "document_filename": "evolution.pdf",
            "document_sha256": digest,
        },
    )
    notifications._send_telegram(None, row)
    assert sent["url"].endswith("/sendDocument")
    assert sent["files"]["document"] == ("evolution.pdf", report.read_bytes(), "application/pdf")

    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(report.read_bytes())
    with pytest.raises(RuntimeError, match="outside document storage"):
        notifications._verified_document_attachment(
            {"document_path": str(outside), "document_sha256": digest}
        )


def test_perplexity_evolution_contract_is_structured_and_advisory(monkeypatch):
    captured = {}
    source_url = "https://github.com/example/runtime"

    class Client:
        def __init__(self, *args, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            content = {
                "summary": "Source-grounded review",
                "findings": [{
                    "source_url": source_url,
                    "observation": "Has eval fixtures",
                    "applicability": "Routing regression tests",
                    "risk": "License review",
                }],
                "recommendations": [{
                    "title": "Version eval fixtures",
                    "domain": "agent_learning",
                    "change": "Add a versioned evaluation dataset",
                    "rationale": "Make regressions measurable",
                    "source_urls": [source_url],
                    "validation": "Run deterministic CI eval",
                    "owner_action_required": False,
                    "owner_action": "",
                }],
            }
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "model": "sonar-test",
                    "choices": [{"message": {"content": __import__("json").dumps(content)}}],
                    "citations": [source_url],
                },
            )

    monkeypatch.setattr(llm.httpx, "Client", Client)
    monkeypatch.setattr(llm.settings, "perplexity_api_key", "secret")
    monkeypatch.setattr(llm.settings, "perplexity_model", "sonar-test")
    result = llm.PerplexityAgentCoach().research_evolution(
        {"github_sources": [{"url": source_url, "readme_excerpt": "untrusted"}]}
    )
    assert result["status"] == "succeeded"
    assert result["recommendations"][0]["source_urls"] == [source_url]
    assert captured["payload"]["response_format"]["type"] == "json_schema"
    system_prompt = captured["payload"]["messages"][0]["content"]
    assert "untrusted data" in system_prompt
    assert "Do not copy source" in system_prompt
