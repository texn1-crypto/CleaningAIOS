from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pypdf import PdfReader
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import notifications, scheduler
from app.daily_owner_pack import PUBLIC_LEAD_SOURCE, run_daily_owner_pack
from app.db import Base
from app.models import BusinessRecord, OwnerNotification, Task


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_daily_owner_pack_builds_three_verified_pdfs_and_deduplicates_delivery(
    monkeypatch,
    tmp_path,
):
    session_factory = _session_factory()
    monkeypatch.setattr(notifications.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(notifications.settings, "owner_telegram_id", "123")
    monkeypatch.setattr(notifications.settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(notifications.settings, "daily_owner_pack_timezone", "Europe/Moscow")

    with session_factory() as db:
        db.add_all(
            [
                BusinessRecord(
                    record_type="lead",
                    external_id="public-1",
                    title="Публичный бизнес-центр",
                    status="researched",
                    source=PUBLIC_LEAD_SOURCE,
                    data={
                        "region": "Санкт-Петербург",
                        "website": "https://business.example/",
                        "public_emails": ["info@business.example"],
                        "public_phones": ["+78120000000"],
                        "source_urls": ["https://business.example/contacts"],
                        "last_verified_at": "2040-01-02T12:00:00",
                        "outreach_consent": "not_verified",
                    },
                ),
                BusinessRecord(
                    record_type="lead",
                    external_id="crm-1",
                    title="Клиент из CRM",
                    status="qualified",
                    source="public_website",
                    score=82,
                    data={
                        "company": "ООО Чистый Тест",
                        "email": "owner@example.test",
                        "phone": "+79990000000",
                        "service": "business_center",
                        "region": "Москва",
                    },
                ),
                Task(
                    title="Проверенная операция",
                    agent_type="orchestrator",
                    status="done",
                    result={"evidence": [{"type": "test"}]},
                ),
            ]
        )
        db.commit()

        first = run_daily_owner_pack(
            db,
            report_day="2040-01-02",
            now=datetime(2040, 1, 2, 15, tzinfo=timezone.utc),
        )
        db.commit()
        second = run_daily_owner_pack(
            db,
            report_day="2040-01-02",
            now=datetime(2040, 1, 2, 16, tzinfo=timezone.utc),
        )
        db.commit()

        assert first["status"] == "completed"
        assert len(first["artifacts"]) == 3
        assert len(second["artifacts"]) == 3
        assert db.scalar(select(func.count(OwnerNotification.id))) == 3
        rows = db.scalars(select(OwnerNotification).order_by(OwnerNotification.id)).all()

        extracted = []
        for artifact in first["artifacts"]:
            path = Path(artifact["path"])
            assert path.is_relative_to(tmp_path)
            raw = path.read_bytes()
            assert raw.startswith(b"%PDF")
            assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
            extracted.append("\n".join(page.extract_text() or "" for page in PdfReader(path).pages))
        assert "Публичный бизнес-центр" in extracted[0]
        assert "Клиент из CRM" in extracted[1]
        assert "Ежедневный операционный отчёт" in extracted[2]

        sent = []

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, **kwargs):
                sent.append((url, kwargs))
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"ok": True},
                )

        monkeypatch.setattr(notifications.httpx, "Client", Client)
        for row in rows:
            notifications._send_telegram(db, row)
        assert len(sent) == 3
        assert all(url.endswith("/sendDocument") for url, _ in sent)
        assert {
            kwargs["files"]["document"][0] for _, kwargs in sent
        } == {artifact["filename"] for artifact in first["artifacts"]}


def test_daily_owner_pack_runs_through_task_api(client, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "owner_telegram_id", "123")
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    task = client.post(
        "/api/tasks",
        json={
            "title": "Daily owner PDF pack API test",
            "agent_type": "orchestrator",
            "payload": {
                "action": "daily_owner_pdf_pack",
                "scheduled_local_day": "2041-03-04",
                "notify_owner": True,
            },
            "max_attempts": 1,
        },
    ).json()

    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    assert completed["status"] == "done"
    assert completed["result"]["report_kind"] == "daily_owner_pdf_pack"
    assert len(completed["result"]["artifacts"]) == 3
    assert completed["result"]["notifications_created"] == 3


def test_scheduler_creates_only_one_daily_owner_pack_task(monkeypatch):
    session_factory = _session_factory()
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler.settings, "daily_owner_pack_timezone", "UTC")
    monkeypatch.setattr(scheduler.settings, "daily_owner_pack_hour", 0)
    monkeypatch.setattr(scheduler.settings, "tender_sources", "")
    monkeypatch.setattr(scheduler.settings, "perplexity_api_key", "")
    monkeypatch.setattr(scheduler.settings, "evolution_research_queries", "")

    scheduler.schedule_cycle()
    scheduler.schedule_cycle()

    with session_factory() as db:
        rows = db.scalars(
            select(Task).where(Task.title.like("Daily owner PDF pack · %"))
        ).all()
        assert len(rows) == 1
        assert rows[0].agent_type == "orchestrator"
        assert rows[0].payload["action"] == "daily_owner_pdf_pack"
        assert rows[0].payload["notify_owner"] is True
