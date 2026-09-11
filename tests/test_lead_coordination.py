from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import lead_reports
from app.db import Base
from app.lead_coordination import LEAD_SCOUT_PROFILES, coordinate_lead_scouts
from app.lead_reports import LEAD_REPORT_RECORD_TYPE, build_instant_lead_report
from app.models import BusinessRecord, OutboundMessage, OwnerNotification, Task


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_coordinator_creates_one_idempotent_task_per_public_source_profile():
    session_factory = _session_factory()
    with session_factory() as db:
        first = coordinate_lead_scouts(
            db,
            {
                "wave_key": "2045-06-05T12:00:00",
                "regions": ["Санкт-Петербург", "Москва"],
                "max_results": 1000,
            },
        )
        second = coordinate_lead_scouts(
            db,
            {
                "wave_key": "2045-06-05T12:00:00",
                "regions": ["Санкт-Петербург", "Москва"],
                "max_results": 1000,
            },
        )

        rows = db.scalars(select(Task).order_by(Task.id)).all()
        assert len(first["tasks_created"]) == len(LEAD_SCOUT_PROFILES) == 4
        assert second["tasks_created"] == []
        assert second["tasks_reused"] == first["tasks_created"]
        assert {row.agent_type for row in rows} == set(LEAD_SCOUT_PROFILES)
        assert all(row.payload["max_results"] == 50 for row in rows)
        assert all(row.payload["automatic_outreach"] is False for row in rows)
        assert first["coverage"]["claim_of_full_internet_coverage"] is False


def test_lead_coordinator_runs_through_protected_task_api(client):
    task = client.post(
        "/api/tasks",
        json={
            "title": "Lead coordinator API regression",
            "agent_type": "lead_coordinator",
            "payload": {
                "action": "coordinate_specialized_lead_scouts",
                "wave_key": "lead-coordinator-api-regression",
                "regions": ["Санкт-Петербург", "Москва"],
                "max_results": 7,
            },
            "max_attempts": 1,
        },
    ).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()

    assert completed["status"] == "done"
    result = completed["result"]
    assert result["status"] == "coordinated"
    assert len(result["tasks_created"]) == 4
    assert result["external_messages_sent"] is False


def test_new_or_changed_lead_creates_one_verified_pdf_and_never_outreach(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(lead_reports.settings, "document_storage_path", str(tmp_path))
    session_factory = _session_factory()
    with session_factory() as db:
        lead = BusinessRecord(
            record_type="lead",
            external_id="instant-report-test",
            title="Бизнес-центр Проверка",
            status="researched",
            owner="sales",
            source="perplexity_public_business_search",
            data={
                "region": "Санкт-Петербург",
                "city": "Санкт-Петербург",
                "website": "https://lead-report.example/",
                "public_emails": ["info@lead-report.example"],
                "public_phones": ["+78120000000"],
                "source_urls": ["https://lead-report.example/contacts"],
                "outreach_consent": "not_verified",
                "automatic_outreach": False,
            },
        )
        db.add(lead)
        db.flush()

        first = build_instant_lead_report(
            db,
            lead_ids=[lead.id],
            scout_role="commercial_lead_scout",
            generated_at=datetime(2045, 6, 5, 12, tzinfo=timezone.utc),
        )
        unchanged = build_instant_lead_report(
            db,
            lead_ids=[lead.id],
            scout_role="commercial_lead_scout",
            generated_at=datetime(2045, 6, 5, 12, 5, tzinfo=timezone.utc),
        )

        assert first["status"] == "completed"
        assert first["lead_count"] == 1
        assert unchanged["status"] == "no_new_information"
        path = Path(first["artifact"]["storage_path"])
        assert path.read_bytes().startswith(b"%PDF")
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        assert "Бизнес-центр Проверка" in text
        assert "info@lead-report.example" in text
        assert "https://lead-report.example/contacts" in text
        assert db.scalar(
            select(func.count()).select_from(BusinessRecord).where(
                BusinessRecord.record_type == LEAD_REPORT_RECORD_TYPE
            )
        ) == 1
        assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0

        lead.data = {
            **lead.data,
            "public_emails": ["info@lead-report.example", "sales@lead-report.example"],
        }
        changed = build_instant_lead_report(
            db,
            lead_ids=[lead.id],
            scout_role="commercial_lead_scout",
            generated_at=datetime(2045, 6, 5, 12, 10, tzinfo=timezone.utc),
        )
        assert changed["status"] == "completed"
        assert changed["report_id"] != first["report_id"]
        assert db.scalar(
            select(func.count()).select_from(BusinessRecord).where(
                BusinessRecord.record_type == LEAD_REPORT_RECORD_TYPE
            )
        ) == 2
        assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 2
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
