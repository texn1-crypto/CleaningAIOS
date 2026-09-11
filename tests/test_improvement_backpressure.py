from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.improvements import (
    compact_agent_coaching_backlog,
    record_agent_coaching_improvements,
)
from app.models import AuditLog, ImprovementRequest
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


def test_perplexity_coaching_compacts_semantic_duplicates_without_deleting_history():
    session_factory = _session_factory()
    with session_factory() as db:
        rows = [
            ImprovementRequest(
                dedup_key="routing-a",
                source_user="perplexity_agent_coach",
                source_channel="system",
                request_text="Классифицировать запросы до маршрутизации",
                suggested_function="Классифицировать запросы до маршрутизации",
                missing_capabilities=["request_analyst_quality_improvement"],
                intent={"agent_type": "request_analyst"},
                classification="agent_quality_gap",
                codex_prompt="Implement routing-a",
                status="queued",
                occurrence_count=2,
            ),
            ImprovementRequest(
                dedup_key="routing-b",
                source_user="perplexity_agent_coach",
                source_channel="system",
                request_text="Добавить категории и routing для каждого запроса",
                suggested_function="Добавить категории и routing для каждого запроса",
                missing_capabilities=["request_analyst_quality_improvement"],
                intent={"agent_type": "request_analyst"},
                classification="agent_quality_gap",
                codex_prompt="Implement routing-b",
                status="queued",
                occurrence_count=3,
            ),
            ImprovementRequest(
                dedup_key="routing-other-agent",
                source_user="perplexity_agent_coach",
                source_channel="system",
                request_text="Классифицировать маршруты meta brain",
                suggested_function="Классифицировать маршруты meta brain",
                missing_capabilities=["meta_brain_quality_improvement"],
                intent={"agent_type": "meta_brain"},
                classification="agent_quality_gap",
                codex_prompt="Implement routing-other-agent",
                status="queued",
            ),
            ImprovementRequest(
                dedup_key="owner-routing",
                source_user="owner",
                source_channel="telegram",
                request_text="Классифицировать отдельный запрос владельца",
                suggested_function="Классифицировать отдельный запрос владельца",
                codex_prompt="Implement owner-routing",
                status="queued",
            ),
        ]
        db.add_all(rows)
        db.flush()
        canonical_id = rows[0].id
        duplicate_id = rows[1].id
        other_agent_id = rows[2].id
        owner_id = rows[3].id

        result = compact_agent_coaching_backlog(db, actor="test-compactor")
        db.flush()

        assert result["reviewed_count"] == 3
        assert result["semantic_group_count"] == 2
        assert result["duplicates_superseded"] == 1
        assert result["canonical_ids"] == [canonical_id]
        canonical = db.get(ImprovementRequest, canonical_id)
        duplicate = db.get(ImprovementRequest, duplicate_id)
        assert canonical is not None
        assert duplicate is not None
        assert canonical.status == "queued"
        assert canonical.occurrence_count == 5
        assert canonical.intent["semantic_focus"] == "request_routing"
        assert canonical.intent["consolidated_duplicate_ids"] == [duplicate_id]
        assert duplicate.status == "rejected"
        assert duplicate.handoff_status == "not_needed"
        assert duplicate.intent["duplicate_of"] == canonical_id
        assert f"#{canonical_id}" in duplicate.implementation_summary
        assert db.get(ImprovementRequest, other_agent_id).status == "queued"
        assert db.get(ImprovementRequest, owner_id).status == "queued"
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "improvement.coaching_backlog_compacted"
            )
        )
        assert audit is not None
        assert audit.actor == "test-compactor"
        assert audit.details["duplicates_superseded"] == 1

        repeated = compact_agent_coaching_backlog(db, actor="test-compactor")
        assert repeated["duplicates_superseded"] == 0

        recorded = record_agent_coaching_improvements(
            db,
            {
                "status": "succeeded",
                "recommendations": [
                    {
                        "agent_type": "request_analyst",
                        "change": "Добавить категоризацию и маршрут запроса",
                        "expected_effect": "Стабильная маршрутизация",
                        "validation": "Проверить категории на тестовом наборе",
                    }
                ],
            },
            limit=1,
            queue_limit=30,
        )
        assert recorded == [
            {
                "id": canonical_id,
                "status": "queued",
                "agent_type": "request_analyst",
                "semantic_focus": "request_routing",
            }
        ]
        assert db.get(ImprovementRequest, canonical_id).occurrence_count == 6
        assert db.scalar(select(func.count(ImprovementRequest.id))) == 4


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
