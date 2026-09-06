from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.improvements import record_agent_coaching_improvements
from app.models import ImprovementRequest
from app.reports import build_activity_report, format_activity_report


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _coaching(*changes: str) -> dict:
    return {
        "status": "succeeded",
        "recommendations": [
            {
                "agent_type": "system_admin",
                "change": change,
                "expected_effect": "Очередь остаётся управляемой",
                "validation": f"Проверить рекомендацию: {change}",
            }
            for change in changes
        ],
    }


def _improvement(
    dedup_key: str,
    *,
    source_user: str,
    source_channel: str,
) -> ImprovementRequest:
    return ImprovementRequest(
        dedup_key=dedup_key,
        source_user=source_user,
        source_channel=source_channel,
        request_text=f"Improvement {dedup_key}",
        codex_prompt=f"Implement {dedup_key}",
        status="queued",
    )


def test_perplexity_coaching_stops_creating_rows_at_backlog_limit():
    session_factory = _session_factory()
    with session_factory() as db:
        first = record_agent_coaching_improvements(
            db,
            _coaching("change-a", "change-b", "change-c"),
            limit=3,
            queue_limit=2,
        )
        assert len(first) == 2
        assert db.scalar(
            select(func.count(ImprovementRequest.id)).where(
                ImprovementRequest.source_user == "perplexity_agent_coach",
                ImprovementRequest.status == "queued",
            )
        ) == 2

        repeated = record_agent_coaching_improvements(
            db,
            _coaching("change-a", "change-d"),
            limit=2,
            queue_limit=2,
        )
        assert len(repeated) == 1
        assert repeated[0]["id"] == first[0]["id"]
        assert db.get(ImprovementRequest, first[0]["id"]).occurrence_count == 2
        assert db.scalar(select(func.count(ImprovementRequest.id))) == 2


def test_activity_report_explains_improvement_queue_sources():
    session_factory = _session_factory()
    with session_factory() as db:
        db.add_all(
            [
                _improvement(
                    "perplexity",
                    source_user="perplexity_agent_coach",
                    source_channel="system",
                ),
                _improvement(
                    "research",
                    source_user="github_evolution_researcher",
                    source_channel="system",
                ),
                _improvement(
                    "telegram",
                    source_user="owner",
                    source_channel="telegram",
                ),
                _improvement(
                    "other",
                    source_user="system_admin",
                    source_channel="system",
                ),
            ]
        )
        db.commit()

        report = build_activity_report(db, period_minutes=120)
        summary = report["summary"]
        assert summary["queued_improvements"] == 4
        assert summary["queued_improvements_perplexity"] == 1
        assert summary["queued_improvements_research"] == 1
        assert summary["queued_improvements_telegram"] == 1
        assert summary["queued_improvements_other"] == 1
        formatted = format_activity_report(report)
        assert "Perplexity: 1" in formatted
        assert "GitHub research: 1" in formatted
        assert "Telegram: 1" in formatted
        assert "прочие: 1" in formatted
