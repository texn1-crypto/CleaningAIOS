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
from app.models import BusinessRecord, ContentItem, OwnerNotification, Task


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_daily_owner_pack_builds_four_verified_pdfs_and_deduplicates_delivery(
    monkeypatch,
    tmp_path,
):
    session_factory = _session_factory()
    monkeypatch.setattr(notifications.settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(notifications.settings, "owner_telegram_id", "123")
    monkeypatch.setattr(notifications.settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(notifications.settings, "daily_owner_pack_timezone", "Europe/Moscow")
    monkeypatch.setattr(notifications.settings, "public_base_url", "https://cleaning.example")
    monkeypatch.setattr(notifications.settings, "social_telegram_url", "https://t.me/cleaning_channel")

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
                ContentItem(
                    channel="telegram",
                    title="Опубликованный пост",
                    body="Текст публикации",
                    status="published",
                    published_at=datetime(2040, 1, 2, 12),
                    metrics={
                        "publication_status": "published",
                        "external_post_id": "812",
                        "public_post_url": "https://t.me/cleaning_channel/812",
                    },
                ),
                ContentItem(
                    channel="website",
                    title="Новость на сайте",
                    body="Текст новости",
                    status="published",
                    published_at=datetime(2040, 1, 2, 13),
                    metrics={},
                ),
                ContentItem(
                    channel="vk",
                    title="Ошибка ВКонтакте",
                    body="Не опубликовано",
                    status="publication_failed",
                    scheduled_at=datetime(2040, 1, 2, 14),
                    metrics={
                        "publication_status": "publication_failed",
                        "public_post_url": "javascript:alert(1)",
                    },
                ),
                ContentItem(
                    channel="odnoklassniki",
                    title="Ожидает согласования",
                    body="Черновик",
                    status="approval",
                    scheduled_at=datetime(2040, 1, 2, 14, 30),
                    metrics={
                        "public_post_url": "https://user:secret@ok.ru/group/1/topic/2",
                    },
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
        assert len(first["artifacts"]) == 4
        assert len(second["artifacts"]) == 4
        assert db.scalar(select(func.count(OwnerNotification.id))) == 4
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
        publication_text = extracted[3]
        assert "https://t.me/cleaning_channel/812" in publication_text
        assert "https://cleaning.example/#news" in publication_text
        assert "Ошибка публикации" in publication_text
        assert "Ожидает действия" in publication_text
        assert "javascript:" not in publication_text
        assert "user:secret" not in publication_text
        publication_reader = PdfReader(first["artifacts"][3]["path"])
        publication_links = {
            str(action["/URI"])
            for page in publication_reader.pages
            for annotation in page.get("/Annots", [])
            if (action := annotation.get_object().get("/A")) and action.get("/URI")
        }
        assert publication_links == {
            "https://t.me/cleaning_channel/812",
            "https://cleaning.example/#news",
        }

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
        assert len(sent) == 4
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
    assert len(completed["result"]["artifacts"]) == 4
    assert completed["result"]["notifications_created"] == 4


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
