"""Synthetic previews exercise the real PDF, database and owner-outbox boundary."""

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import lead_reports
from app.db import Base
from app.models import BusinessRecord, OutboundMessage, OwnerNotification


@pytest.fixture
def report_db(monkeypatch, tmp_path):
    monkeypatch.setattr(lead_reports.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(lead_reports.settings, "owner_telegram_id", "synthetic-owner")
    monkeypatch.setattr(lead_reports.settings, "telegram_bot_token", "synthetic-token")
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False)() as db:
        db.add(BusinessRecord(
            record_type="lead", external_id="preview-regression", title="Склад: тестовый лид",
            status="owner_review", source="verified_management_company_website",
            data={"region": "Санкт-Петербург", "website": "https://example.test/",
                  "source_urls": ["https://example.test/contacts"],
                  "public_emails": ["office@example.test"], "public_phones": []},
        ))
        db.flush()
        yield db
    engine.dispose()


def _build(db, *, notify_owner):
    lead = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == "lead"))
    return lead_reports.build_instant_lead_report(
        db, lead_ids=[lead.id], scout_role="management_lead_scout",
        generated_at=datetime(2045, 6, 5, 12), notify_owner=notify_owner,
    )


def test_lead_preview_can_be_delivered_without_regenerating_or_resending(report_db):
    db = report_db
    preview = _build(db, notify_owner=False)
    path = Path(preview["artifact"]["storage_path"])
    original = path.read_bytes()
    delivered = _build(db, notify_owner=True)

    assert delivered["status"] == "completed"
    assert delivered["reused"] is True
    assert delivered["report_id"] == preview["report_id"]
    note = db.get(OwnerNotification, delivered["notification_id"])
    assert note.status == "queued"
    assert note.data["document_sha256"] == preview["artifact"]["sha256"]
    assert path.read_bytes() == original
    assert _build(db, notify_owner=True)["status"] == "no_new_information"
    assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1
    assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
    assert db.scalar(select(func.count()).select_from(BusinessRecord).where(
        BusinessRecord.record_type == lead_reports.LEAD_REPORT_RECORD_TYPE,
    )) == 1


def test_legacy_preview_signature_does_not_suppress_delivery(report_db):
    db = report_db
    preview = _build(db, notify_owner=False)
    report = db.get(BusinessRecord, preview["report_id"])
    report.data = {k: v for k, v in report.data.items() if k != "notification_id"}
    lead = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == "lead"))
    lead.data = {**lead.data,
                 "instant_lead_report_signature": lead_reports._snapshot_signature(
                     lead_reports._material_snapshot(lead)),
                 "instant_lead_report_id": preview["report_id"],
                 "instant_lead_reported_at": "2045-06-05T12:00:00"}
    db.flush()

    result = _build(db, notify_owner=True)

    assert result["report_id"] == preview["report_id"]
    assert result["notification_id"] is not None
    assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1


def test_cached_report_with_lost_lead_marker_keeps_existing_notification(report_db):
    db = report_db
    first = _build(db, notify_owner=True)
    note = db.get(OwnerNotification, first["notification_id"])
    note.status = "sent"
    lead = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == "lead"))
    lead.data = {k: v for k, v in lead.data.items() if not k.startswith("instant_lead_report")}
    db.flush()

    result = _build(db, notify_owner=True)

    assert result["report_id"] == first["report_id"]
    assert result["notification_id"] == note.id
    assert note.status == "sent"
    assert _build(db, notify_owner=True)["status"] == "no_new_information"
    assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1


@pytest.mark.parametrize("damage", ["missing", "checksum"])
def test_preview_upgrade_rejects_missing_or_modified_pdf(report_db, damage):
    db = report_db
    preview = _build(db, notify_owner=False)
    path = Path(preview["artifact"]["storage_path"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"synthetic-tamper")

    with pytest.raises((FileNotFoundError, RuntimeError)):
        _build(db, notify_owner=True)
    assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 0


@pytest.mark.parametrize("status", ["queued", "sent", "dead_letter", "waiting_configuration"])
def test_existing_delivery_is_never_requeued_by_preview_upgrade(report_db, status):
    db = report_db
    delivered = _build(db, notify_owner=True)
    note = db.get(OwnerNotification, delivered["notification_id"])
    note.status = status
    db.flush()

    result = _build(db, notify_owner=True)

    assert result["status"] == "no_new_information"
    assert note.status == status
    assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1


def test_changed_lead_does_not_deliver_stale_preview(report_db):
    db = report_db
    preview = _build(db, notify_owner=False)
    lead = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == "lead"))
    lead.data = {**lead.data, "website": "https://new.example.test/"}
    db.flush()

    result = _build(db, notify_owner=True)

    assert result["report_id"] != preview["report_id"]
    assert db.get(BusinessRecord, preview["report_id"]).data["notification_id"] is None
    assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1


def test_preview_upgrade_through_scout_task_workflow(client, monkeypatch, tmp_path):
    from app import lead_scout
    from app.db import SessionLocal

    monkeypatch.setattr(lead_reports.settings, "document_storage_path", str(tmp_path))
    cited = "https://preview-workflow.example.test/contacts"
    # Only the external discovery provider is simulated; dispatch, PDF and outbox are real.
    monkeypatch.setattr(lead_scout.llm_advisor, "discover_public_business_leads", lambda brief: {
        "status": "succeeded", "provider": "synthetic_regression", "citations": [cited],
        "leads": [{"organization_name": "Склад Preview Workflow", "region": "Санкт-Петербург",
                   "email": "info@preview-workflow.example.test", "website": cited,
                   "source_url": cited, "contact_scope": "organization",
                   "contact_person_named": False}],
    })

    def run(notify_owner):
        response = client.post("/api/tasks", json={
            "title": "Synthetic preview delivery regression", "agent_type": "lead_scout",
            "payload": {"regions": ["Санкт-Петербург"], "max_results": 1,
                        "notify_owner": notify_owner, "automatic_outreach": False},
        })
        assert response.status_code == 201
        result = client.post(f"/api/tasks/{response.json()['id']}/run").json()
        assert result["status"] == "done"
        return result["result"]["instant_lead_report"]

    preview = run(False)
    delivered = run(True)
    assert delivered["report_id"] == preview["report_id"]
    assert delivered["notification_id"] is not None
    assert run(True)["status"] == "no_new_information"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(OwnerNotification).where(
            OwnerNotification.resource_type == lead_reports.LEAD_REPORT_RECORD_TYPE,
            OwnerNotification.resource_id == str(preview["report_id"]),
        )) == 1
