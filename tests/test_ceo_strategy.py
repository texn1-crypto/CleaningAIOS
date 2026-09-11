from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import AGENTS
from app.db import Base
from app.models import AuditLog, Task
from app.operations import (
    CEO_AGENT_GROWTH_STRATEGIES,
    CEO_DEVELOPMENT_BACKLOG,
    CEO_GROWTH_MISSION,
    CEO_SELF_IMPROVEMENT_PROTOCOL,
    CEO_STRATEGY_VERSION,
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
        assert set(CEO_AGENT_GROWTH_STRATEGIES) == set(AGENTS)

        for task in tasks:
            result = dispatch(db, task)
            assert task.status == "done"
            assert result["external_actions_executed"] is False
            assert result["business_mission"] == CEO_GROWTH_MISSION
            assert result["success_metric"]
            assert result["lead_stage"]
            assert result["profit_lever"]
            assert result["strategy_hypothesis"]
            assert result["strategy_decision"] == "establish_baseline"
            assert result["self_improvement"]["protocol"] == CEO_SELF_IMPROVEMENT_PROTOCOL
            assert result["self_improvement"]["next_safe_experiment"]
            assert result["evidence"][0]["type"] == "ceo_strategy_checkpoint"
        db.commit()


def test_legacy_future_strategy_task_is_upgraded_in_place_with_audit():
    session_factory = _session_factory()
    now = datetime(2026, 9, 9, 9, 0)
    template = CEO_DEVELOPMENT_BACKLOG[0]
    with session_factory() as db:
        legacy = Task(
            title=template["title"],
            agent_type=template["agent_type"],
            status="queued",
            run_after=datetime(2026, 9, 10, 9, 0),
            payload={
                "action": "website_growth_review",
                "origin": "ceo_continuous_backlog",
            },
        )
        db.add(legacy)
        db.flush()
        legacy_id = legacy.id

        maintained = maintain_ceo_development_backlog(db, now=now, cadence_hours=24)
        db.commit()

        upgraded = db.get(Task, legacy_id)
        assert upgraded is not None
        assert upgraded.payload["strategy_version"] == CEO_STRATEGY_VERSION
        assert upgraded.payload["action"] == "ceo_strategic_checkpoint"
        assert upgraded.payload["business_mission"] == CEO_GROWTH_MISSION
        assert upgraded.payload["lead_stage"]
        assert upgraded.payload["profit_lever"]
        assert upgraded.payload["strategy_hypothesis"]
        assert upgraded.run_after == now
        assert sum(task.title == template["title"] for task in db.scalars(select(Task)).all()) == 1
        assert upgraded in maintained
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "strategy.task_upgraded",
                AuditLog.resource_id == str(legacy_id),
            )
        )
        assert audit is not None
        assert audit.details == {
            "from_version": "legacy",
            "to_version": CEO_STRATEGY_VERSION,
        }


def test_strategy_checkpoint_revises_from_domain_outcomes_not_busywork():
    session_factory = _session_factory()
    now = datetime(2026, 9, 9, 9, 0)
    with session_factory() as db:
        successful = Task(
            title="Qualified lead analysis",
            agent_type="sales",
            status="done",
            result={"evidence": [{"type": "lead_pipeline"}]},
        )
        failed = Task(
            title="CRM normalization",
            agent_type="sales",
            status="failed",
            result={"error": "technical"},
        )
        old_checkpoint = Task(
            title="Old strategic checkpoint",
            agent_type="sales",
            status="done",
            payload={"origin": "ceo_continuous_backlog"},
            result={"strategy_decision": "keep_and_test_next_hypothesis"},
        )
        db.add_all([successful, failed, old_checkpoint])
        db.flush()
        checkpoint = next(
            task
            for task in maintain_ceo_development_backlog(db, now=now, cadence_hours=24)
            if task.agent_type == "sales"
        )

        result = dispatch(db, checkpoint)

        assert result["status"] == "at_risk"
        assert result["strategy_decision"] == "repair_before_next_experiment"
        assert result["previous_strategy_decision"] == "keep_and_test_next_hypothesis"
        assert result["measured_task_count"] == 2
        assert result["completion_rate_percent"] == 50.0
        assert old_checkpoint.id not in result["self_improvement"]["observation_task_ids"]
        assert set(result["self_improvement"]["observation_task_ids"]) == {
            successful.id,
            failed.id,
        }
        assert result["external_actions_executed"] is False


def test_strategy_checkpoint_preserves_owner_approval_as_a_guardrail():
    session_factory = _session_factory()
    now = datetime(2026, 9, 9, 9, 0)
    with session_factory() as db:
        approval_wait = Task(
            title="Send approved proposal",
            agent_type="sales",
            status="blocked",
            result={
                "reason": "owner_approval_required",
                "approval_id": 42,
            },
        )
        db.add(approval_wait)
        db.flush()
        checkpoint = next(
            task
            for task in maintain_ceo_development_backlog(db, now=now, cadence_hours=24)
            if task.agent_type == "sales"
        )

        result = dispatch(db, checkpoint)

        assert result["status"] == "waiting_owner_approval"
        assert result["strategy_decision"] == "hold_for_owner_approval"
        assert result["blocked_task_count"] == 0
        assert result["owner_approval_waiting_task_count"] == 1
        assert result["self_improvement"]["observation_task_ids"] == []
        assert result["self_improvement"]["owner_approval_waiting_task_ids"] == [
            approval_wait.id
        ]
        assert result["external_actions_executed"] is False


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
