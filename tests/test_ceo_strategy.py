from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import AGENTS
from app.db import Base
from app.models import Task
from app.operations import (
    CEO_DEVELOPMENT_BACKLOG,
    maintain_ceo_development_backlog,
    review_ceo_strategy_portfolio,
)
from app.orchestrator import dispatch
from app.task_state import transition_task


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_every_registered_agent_executes_an_evidence_backed_strategy_checkpoint():
    session_factory = _session_factory()
    with session_factory() as db:
        tasks = maintain_ceo_development_backlog(
            db,
            now=datetime(2026, 9, 9, 9, 0),
            cadence_hours=24,
        )
        assert {task.agent_type for task in tasks} == set(AGENTS)

        for task in tasks:
            result = dispatch(db, task)
            assert task.status == "done"
            assert result["external_actions_executed"] is False
            assert result["success_metric"]
            assert result["evidence"][0]["type"] == "ceo_strategy_checkpoint"
        db.commit()


def test_ceo_review_deduplicates_system_admin_handoff_for_failed_lane():
    session_factory = _session_factory()
    with session_factory() as db:
        tasks = maintain_ceo_development_backlog(
            db,
            now=datetime(2026, 9, 9, 9, 0),
            cadence_hours=24,
        )
        failed_task = next(task for task in tasks if task.agent_type == "finance")
        transition_task(
            db,
            failed_task,
            "running",
            actor="finance",
            reason="test_started",
        )
        transition_task(
            db,
            failed_task,
            "failed",
            actor="finance",
            reason="test_failure",
        )

        first = review_ceo_strategy_portfolio(db, cycle_key="2026-09-09T00:00:00")
        repeated = review_ceo_strategy_portfolio(db, cycle_key="2026-09-09T00:00:00")
        db.commit()

        assert first["status"] == "at_risk"
        assert first["covered_agent_types"] == sorted(AGENTS)
        assert first["missing_agent_types"] == []
        assert first["at_risk_agent_types"] == ["finance"]
        assert first["system_admin_task_id"] == repeated["system_admin_task_id"]
        handoffs = db.scalars(
            select(Task).where(Task.payload["source"].as_string() == "ceo_strategy_supervision")
        ).all()
        assert len(handoffs) == 1
        assert handoffs[0].agent_type == "system_admin"
        assert handoffs[0].priority == "critical"
        assert handoffs[0].payload["at_risk_agent_types"] == ["finance"]


def test_ceo_review_does_not_claim_queued_work_is_verified():
    session_factory = _session_factory()
    with session_factory() as db:
        maintain_ceo_development_backlog(
            db,
            now=datetime(2026, 9, 9, 9, 0),
            cadence_hours=24,
        )
        result = review_ceo_strategy_portfolio(db, cycle_key="2026-09-09T00:00:00")

        assert result["status"] == "pending"
        assert result["pending_agent_types"] == sorted(AGENTS)
        assert result["verified_completed_agent_types"] == []
        assert result["system_admin_task_id"] is None


def test_ceo_review_is_verified_only_after_evidence_backed_completion():
    session_factory = _session_factory()
    with session_factory() as db:
        tasks = maintain_ceo_development_backlog(
            db,
            now=datetime(2026, 9, 9, 9, 0),
            cadence_hours=24,
        )
        for task in tasks:
            dispatch(db, task)
        result = review_ceo_strategy_portfolio(db, cycle_key="2026-09-09T00:00:00")
        db.commit()

        assert result["status"] == "verified"
        assert result["at_risk_agent_types"] == []
        assert result["missing_agent_types"] == []
        assert result["verified_completed_agent_types"] == sorted(AGENTS)
        assert result["system_admin_task_id"] is None
        assert len(CEO_DEVELOPMENT_BACKLOG) >= len(AGENTS)
