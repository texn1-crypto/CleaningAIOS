from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import lead_research
from app.agents import FocusedLeadScoutAgent
from app.db import Base
from app.lead_research import (
    LEAD_RESEARCH_ACTION,
    research_public_lead_evidence,
    schedule_public_lead_research,
)
from app.models import AuditLog, BusinessRecord, OutboundMessage, Task


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _lead(title: str, *, outcome: str = "owner_review") -> BusinessRecord:
    return BusinessRecord(
        record_type="lead",
        external_id=f"research:{title}",
        title=title,
        status="owner_review",
        owner="sales",
        source="verified_management_company_website",
        data={
            "region": "Санкт-Петербург",
            "organization_type": "management_company",
            "website": "https://example.test/",
            "source_urls": ["https://example.test/contacts"],
            "qualification_fingerprint": f"qualification:{title}",
            "qualification": {
                "outcome": outcome,
                "score": 70,
                "responsible_scout": "management_lead_scout",
                "missing_facts": [
                    "public_evidence_of_need",
                    "cleaning_scope_and_area",
                    "procurement_timing",
                ],
            },
        },
    )


def test_ceo_research_scheduler_is_bounded_and_idempotent(monkeypatch):
    monkeypatch.setattr(lead_research.settings, "lead_research_batch_size", 2)
    monkeypatch.setattr(lead_research.settings, "perplexity_api_key", "configured")
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        db.add_all(
            [
                _lead("Первая"),
                _lead("Вторая", outcome="research"),
                _lead("Третья"),
                _lead("Отказ", outcome="reject"),
            ]
        )
        db.flush()

        first = schedule_public_lead_research(
            db,
            current=now,
            cycle_key="2045-06-05T12:00",
        )
        repeated = schedule_public_lead_research(
            db,
            current=now,
            cycle_key="2045-06-05T13:00",
        )

        tasks = db.scalars(
            select(Task).where(
                Task.payload["action"].as_string() == LEAD_RESEARCH_ACTION
            )
        ).all()
        assert first["eligible_leads"] == 3
        assert len(first["tasks_created"]) == 2
        assert repeated["tasks_created"] == []
        assert repeated["tasks_reused"] == first["tasks_created"]
        assert len(tasks) == 2
        assert all(task.agent_type == "management_lead_scout" for task in tasks)
        assert all(task.payload["automatic_outreach"] is False for task in tasks)
        for task in tasks:
            task.status = "done"
        next_batch = schedule_public_lead_research(
            db,
            current=now,
            cycle_key="2045-06-05T14:00",
        )
        assert len(next_batch["tasks_created"]) == 1
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_research_scheduler_does_not_queue_without_provider_credentials(monkeypatch):
    monkeypatch.setattr(lead_research.settings, "perplexity_api_key", "")
    session_factory = _session_factory()
    with session_factory() as db:
        db.add(_lead("Нет провайдера"))
        db.flush()

        result = schedule_public_lead_research(
            db,
            current=datetime(2045, 6, 5, 12, 0),
            cycle_key="2045-06-05T12:00",
        )

        assert result["status"] == "credentials_required"
        assert result["tasks_created"] == []
        assert db.scalar(select(func.count()).select_from(Task)) == 0


def test_research_scheduler_backs_off_after_provider_configuration_failure(monkeypatch):
    monkeypatch.setattr(lead_research.settings, "lead_research_batch_size", 2)
    monkeypatch.setattr(lead_research.settings, "perplexity_api_key", "configured")
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    with session_factory() as db:
        db.add_all([_lead("Первая"), _lead("Вторая"), _lead("Третья")])
        db.flush()
        first = schedule_public_lead_research(
            db,
            current=now,
            cycle_key="2045-06-05T12:00",
        )
        blocking_task = db.get(Task, first["tasks_created"][0])
        assert blocking_task is not None
        blocking_task.status = "blocked"
        blocking_task.updated_at = now
        blocking_task.result = {
            "status": "unavailable",
            "handoff_status": "credentials_required",
            "provider": "perplexity_sonar",
        }
        db.flush()

        repeated = schedule_public_lead_research(
            db,
            current=now,
            cycle_key="2045-06-05T13:00",
        )

        assert repeated["status"] == "credentials_required"
        assert repeated["tasks_created"] == []
        assert repeated["configuration_blocking_task_ids"] == [blocking_task.id]
        assert db.scalar(select(func.count()).select_from(Task)) == 2


def test_targeted_research_persists_only_exact_cited_facts(monkeypatch):
    session_factory = _session_factory()
    now = datetime(2045, 6, 5, 12, 0)
    cited = "https://procurement.example.test/notice/15"
    monkeypatch.setattr(
        lead_research.llm_advisor,
        "research_public_lead_evidence",
        lambda brief: {
            "status": "succeeded",
            "provider": "perplexity_sonar",
            "organization_name": brief["organization_name"],
            "citations": [cited],
            "facts": [
                {
                    "field": "public_need_evidence",
                    "value": "Опубликован запрос предложений на ежедневную уборку",
                    "source_url": cited,
                },
                {
                    "field": "budget",
                    "value": "500000",
                    "source_url": "https://uncited.example.test/claim",
                },
            ],
        },
    )
    with session_factory() as db:
        lead = _lead("УК Публичный Спрос")
        db.add(lead)
        db.flush()

        result = FocusedLeadScoutAgent("management_lead_scout").execute(
            db,
            {"action": LEAD_RESEARCH_ACTION, "record_id": lead.id},
        )

        assert result["status"] == "completed"
        assert result["changed"] is True
        assert result["accepted_fact_count"] == 1
        assert result["rejected_fact_count"] == 1
        assert lead.data["public_need_evidence"].startswith("Опубликован")
        assert "budget" not in lead.data
        assert "qualification_fingerprint" not in lead.data
        assert lead.data["lead_research_evidence"][0]["source_url"] == cited
        assert result["external_messages_sent"] is False
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "lead.public_evidence_researched"
            )
        )
        assert audit is not None
        assert audit.details["accepted_fields"] == ["public_need_evidence"]
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_targeted_research_rejects_another_organization(monkeypatch):
    session_factory = _session_factory()
    monkeypatch.setattr(
        lead_research.llm_advisor,
        "research_public_lead_evidence",
        lambda brief: {
            "status": "succeeded",
            "provider": "perplexity_sonar",
            "organization_name": "Другая организация",
            "citations": ["https://example.test/notice"],
            "facts": [
                {
                    "field": "public_need_evidence",
                    "value": "Нужен клининг",
                    "source_url": "https://example.test/notice",
                }
            ],
        },
    )
    with session_factory() as db:
        lead = _lead("Точная организация")
        db.add(lead)
        db.flush()

        result = research_public_lead_evidence(
            db,
            {"record_id": lead.id},
        )

        assert result["status"] == "not_verified"
        assert result["reason"] == "organization_name_mismatch"
        assert "public_need_evidence" not in lead.data
        assert lead.data["qualification_fingerprint"]
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
