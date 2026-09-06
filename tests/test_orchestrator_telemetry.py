from __future__ import annotations

from datetime import datetime, timedelta
import json

from sqlalchemy import func, select

from app.agents import MetaBrainAgent
from app.db import SessionLocal
from app.models import AuditLog, OrchestratorDecision, Task
from app.orchestrator_telemetry import (
    measure_routing_outcome,
    record_routing_decisions,
    routing_decision_outcome_summary,
)


def test_orchestrator_routing_is_audited_measured_and_pii_free(client, monkeypatch):
    secret_marker = "customer-private@example.test"
    source = client.post(
        "/api/tasks",
        json={
            "title": "Route two safe internal tasks",
            "agent_type": "orchestrator",
            "payload": {
                "correlation_id": "routing-regression-1",
                "message": f"Do not persist {secret_marker} in routing telemetry",
                "delegations": [
                    {
                        "agent_type": "finance",
                        "title": f"Private title {secret_marker}",
                        "payload": {"internal_note": secret_marker},
                    },
                    {
                        "agent_type": "research",
                        "title": "Approval-gated research",
                        "payload": {
                            "action": "collect_tenders",
                            "action_kind": "tender_submission",
                            "private_contact": secret_marker,
                        },
                    },
                ],
            },
        },
    ).json()

    completed = client.post(f"/api/tasks/{source['id']}/run")

    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "done"
    assert len(body["result"]["routing_decision_ids"]) == 2

    denied = client.get(
        f"/api/orchestrator-decisions?source_task_id={source['id']}",
        headers={"X-Role": "operator"},
    )
    assert denied.status_code == 403
    response = client.get(
        f"/api/orchestrator-decisions?source_task_id={source['id']}",
        headers={"X-Role": "manager"},
    )
    assert response.status_code == 200
    decisions = response.json()
    assert len(decisions) == 2
    assert secret_marker not in json.dumps(decisions)
    by_agent = {row["selected_agent"]: row for row in decisions}
    assert by_agent["finance"]["task_type"] == "finance"
    assert by_agent["finance"]["expectation_status"] == "success_expected"
    assert by_agent["finance"]["outcome_status"] == "pending"
    assert by_agent["research"]["task_type"] == "research"
    assert by_agent["research"]["expectation_status"] == "at_risk"

    child_id = by_agent["finance"]["delegated_task_id"]
    child = client.post(f"/api/tasks/{child_id}/run")
    assert child.status_code == 200
    assert child.json()["status"] == "done"

    measured = client.get(
        f"/api/orchestrator-decisions?source_task_id={source['id']}",
        headers={"X-Role": "manager"},
    ).json()
    finance = next(row for row in measured if row["selected_agent"] == "finance")
    assert finance["successful"] is True
    assert finance["outcome_status"] == "succeeded"
    assert finance["measured_at"] is not None

    with SessionLocal() as db:
        source_task = db.get(Task, source["id"])
        assert source_task is not None
        before = db.scalar(
            select(func.count(OrchestratorDecision.id)).where(
                OrchestratorDecision.source_task_id == source["id"]
            )
        )
        record_routing_decisions(db, source_task=source_task, result=source_task.result)
        db.commit()
        after = db.scalar(
            select(func.count(OrchestratorDecision.id)).where(
                OrchestratorDecision.source_task_id == source["id"]
            )
        )
        assert before == after == 2
        audits = db.scalars(
            select(AuditLog).where(
                AuditLog.resource_type == "orchestrator_decision",
                AuditLog.resource_id.in_([str(row["id"]) for row in measured]),
            )
        ).all()
        assert {
            row.action for row in audits
        } >= {
            "orchestrator.routing_decision_recorded",
            "orchestrator.routing_outcome_measured",
        }
        assert secret_marker not in json.dumps([row.details for row in audits])

    monkeypatch.setattr(
        "app.agents.llm_advisor.coach_agents",
        lambda _: {
            "status": "disabled",
            "provider": "none",
            "model": "none",
            "recommendations": [],
        },
    )
    with SessionLocal() as db:
        metrics = MetaBrainAgent().execute(db, {})
        assert metrics["orchestrator_decisions_recorded"] >= 2
        assert metrics["orchestrator_outcomes_measured"] >= 1
        assert metrics["decision_outcomes_measured"] >= 1
        assert metrics["decision_success_rate"] is not None


def test_routing_outcome_waits_for_retries_and_marks_unmet_integration():
    with SessionLocal() as db:
        source = Task(title="Telemetry retry source", agent_type="orchestrator")
        child = Task(
            title="Telemetry retry child",
            agent_type="research",
            status="failed",
            attempts=1,
            max_attempts=2,
            result={"status": "credentials_required"},
        )
        db.add_all([source, child])
        db.flush()
        decision = record_routing_decisions(
            db,
            source_task=source,
            result={
                "delegated_tasks": [
                    {"id": child.id, "agent_type": child.agent_type}
                ]
            },
        )[0]

        measure_routing_outcome(db, child)
        assert decision.successful is None
        assert decision.outcome_status == "pending"

        child.attempts = child.max_attempts
        measure_routing_outcome(db, child)
        assert decision.successful is False
        assert decision.outcome_status == "expectation_missed"
        assert decision.measured_at is not None
        db.rollback()


def test_routing_outcome_distinguishes_integration_gap_from_pending_approval():
    with SessionLocal() as db:
        source = Task(title="Telemetry blocked source", agent_type="orchestrator")
        integration_gap = Task(
            title="Telemetry integration gap",
            agent_type="research",
            status="blocked",
            result={
                "status": "credentials_required",
                "execution_gap": "Official adapter is not configured",
            },
        )
        pending_approval = Task(
            title="Telemetry approval wait",
            agent_type="sales",
            status="blocked",
            result={
                "blocked": True,
                "reason": "owner_approval_required",
                "approval_id": 42,
            },
        )
        db.add_all([source, integration_gap, pending_approval])
        db.flush()
        decisions = record_routing_decisions(
            db,
            source_task=source,
            result={
                "delegated_tasks": [
                    {"id": integration_gap.id, "agent_type": "research"},
                    {"id": pending_approval.id, "agent_type": "sales"},
                ]
            },
        )
        by_agent = {row.selected_agent: row for row in decisions}

        measure_routing_outcome(db, integration_gap)
        measure_routing_outcome(db, pending_approval)

        assert by_agent["research"].successful is False
        assert by_agent["research"].outcome_status == "expectation_missed"
        assert by_agent["research"].measured_at is not None
        assert by_agent["sales"].successful is None
        assert by_agent["sales"].outcome_status == "pending"
        assert by_agent["sales"].measured_at is None
        db.rollback()


def test_partial_routing_outcome_is_logged_with_required_dimensions():
    with SessionLocal() as db:
        source = Task(title="Telemetry partial source", agent_type="orchestrator")
        child = Task(
            title="Telemetry partial child",
            agent_type="marketing",
            status="done",
            result={"status": "partial"},
        )
        db.add_all([source, child])
        db.flush()
        decision = record_routing_decisions(
            db,
            source_task=source,
            result={
                "delegated_tasks": [
                    {"id": child.id, "agent_type": child.agent_type}
                ]
            },
        )[0]

        measure_routing_outcome(db, child)

        assert decision.decision_outcome == "partial"
        assert decision.successful is False
        audit = db.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "orchestrator.routing_outcome_measured",
                AuditLog.resource_id == str(decision.id),
            )
            .order_by(AuditLog.id.desc())
        )
        assert audit is not None
        assert audit.details == {
            "delegated_task_id": child.id,
            "selected_agent": "marketing",
            "agent_type": "marketing",
            "task_category": "marketing",
            "decision_outcome": "partial",
            "outcome_status": "expectation_missed",
            "successful": False,
            "correlation_id": f"task:{source.id}",
        }
        db.rollback()


def test_decision_outcome_summary_aggregates_24_outcomes_by_agent():
    now = datetime(2026, 9, 6, 12, 0, 0)
    with SessionLocal() as db:
        source = Task(title="Telemetry aggregate source", agent_type="orchestrator")
        db.add(source)
        db.flush()
        for index in range(24):
            agent_type = "research" if index < 18 else "finance"
            outcome = "success" if index < 12 else "fail" if index < 18 else "partial"
            task = Task(
                title=f"Telemetry aggregate child {index}",
                agent_type=agent_type,
                status="done" if outcome != "fail" else "failed",
                result={"decision_outcome": outcome},
            )
            db.add(task)
            db.flush()
            db.add(
                OrchestratorDecision(
                    decision_key=f"aggregate-test:{index}",
                    source_task_id=source.id,
                    delegated_task_id=task.id,
                    task_type=agent_type,
                    selected_agent=agent_type,
                    expected_result="Synthetic aggregate-only regression outcome.",
                    expectation_status="success_expected",
                    outcome_status=(
                        "succeeded" if outcome == "success" else "expectation_missed"
                    ),
                    decision_outcome=outcome,
                    successful=outcome == "success",
                    measured_at=now - timedelta(minutes=index),
                )
            )
        db.flush()

        summary = routing_decision_outcome_summary(db, window_hours=24, now=now)

        assert summary["measured"] == 24
        assert summary["success"] == 12
        assert summary["partial"] == 6
        assert summary["fail"] == 6
        assert summary["decision_success_rate_percent"] == 50.0
        by_agent = {row["agent_type"]: row for row in summary["per_agent"]}
        assert by_agent["research"]["decision_success_rate_percent"] == 66.67
        assert by_agent["finance"]["partial"] == 6
        assert summary["privacy"]["aggregate_only"] is True
        db.rollback()
