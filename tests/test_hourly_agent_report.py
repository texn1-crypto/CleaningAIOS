from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import AGENTS, MarketingAgent
from app.db import Base
from app.models import AgentRun, ContentItem, MediaAsset, Task
from app.reports import build_activity_report, format_activity_report


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_hourly_report_accounts_for_every_agent_and_social_blockers():
    session_factory = _session_factory()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as db:
        task = Task(
            title="Проверить CRM-воронку",
            agent_type="sales",
            status="done",
        )
        db.add(task)
        db.flush()
        db.add(
            AgentRun(
                agent_type="sales",
                task_id=task.id,
                status="succeeded",
                started_at=now,
                finished_at=now,
            )
        )
        item = ContentItem(
            channel="telegram",
            title="Пост",
            status="visual_pending",
            scheduled_at=now,
        )
        db.add(item)
        db.flush()
        db.add(
            MediaAsset(
                content_item_id=item.id,
                kind="image",
                title="Визуал",
                provider="openai_images",
                status="credentials_required",
            )
        )
        db.commit()

        report = build_activity_report(db, period_minutes=60)
        activity = {row["agent_type"]: row for row in report["agent_activity"]}
        assert set(AGENTS).issubset(activity)
        assert {"social_image", "social_publisher"}.issubset(activity)
        assert activity["sales"]["did_work"] is True
        assert activity["sales"]["succeeded"] == 1
        assert activity["sales"]["last_task_title"] == "Проверить CRM-воронку"
        assert activity["copywriter"]["did_work"] is False
        assert "не назначались задачи" in activity["copywriter"]["inactivity_reason"]
        assert "ожидают готовых изображений" in activity["social_publisher"]["inactivity_reason"]
        assert "IMAGE_GENERATION_API_KEY" in activity["social_image"]["inactivity_reason"]

        formatted = format_activity_report(report)
        assert "Работа каждого ИИ-агента" in formatted
        assert "sales: 1/1 успешно" in formatted
        assert "copywriter: не работал" in formatted
        assert "social_publisher: не работал" in formatted
        assert len(formatted) < 4096


def test_marketing_agent_uses_daily_evergreen_fallback_when_news_is_unavailable(
    monkeypatch,
):
    from app import social_marketing

    session_factory = _session_factory()
    monkeypatch.setattr(social_marketing, "fetch_cleaning_news", lambda **kwargs: [])
    payload = {
        "action": "prepare_daily_cleaning_news_plan",
        "day": "2042-02-04T07:00:00+00:00",
    }
    with session_factory() as db:
        first = MarketingAgent().execute(db, payload)
        db.commit()
        assert first["content_source"] == "evergreen_fallback"
        assert first["news_status"] == "news_unavailable"
        assert first["created"] == 10
        assert any(
            row.get("type") == "evergreen_social_fallback"
            for row in first["evidence"]
        )
        assert db.scalar(select(func.count(ContentItem.id))) == 10

        repeated = MarketingAgent().execute(db, payload)
        db.commit()
        assert repeated["created"] == 0
        assert db.scalar(select(func.count(ContentItem.id))) == 10
