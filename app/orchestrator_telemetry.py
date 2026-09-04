from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, OrchestratorDecision, Task


_SAFE_TASK_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MISSED_EXPECTATION_STATUSES = frozenset(
    {
        "adapter_required",
        "configuration_required",
        "credentials_required",
        "failed",
        "not_found",
        "stale_evaluation",
        "tender_closed",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _task_type(task: Task) -> str:
    # Agent type is registry-controlled. Free-form action/title/payload values are
    # deliberately excluded because they can contain customer data or credentials.
    return task.agent_type if _SAFE_TASK_TYPE.fullmatch(task.agent_type) else "delegated_task"


def _expectation_status(task: Task, now: datetime) -> str:
    payload = task.payload or {}
    deadline_is_tight = bool(
        task.due_at
        and task.due_at <= now + timedelta(seconds=max(1, task.timeout_seconds))
    )
    approval_is_required = bool(payload.get("action_kind"))
    retry_budget_is_low = task.max_attempts <= 1
    return "at_risk" if deadline_is_tight or approval_is_required or retry_budget_is_low else "success_expected"


def record_routing_decisions(
    db: Session,
    *,
    source_task: Task,
    result: dict[str, Any],
) -> list[OrchestratorDecision]:
    """Persist only deterministic routing metadata; never copy titles or task payloads."""

    raw_delegations = result.get("delegated_tasks")
    if not isinstance(raw_delegations, list):
        return []
    now = _now()
    correlation_id = f"task:{source_task.id}"
    recorded: list[OrchestratorDecision] = []
    seen_task_ids: set[int] = set()
    for item in raw_delegations:
        if not isinstance(item, dict):
            continue
        raw_task_id = item.get("id")
        if raw_task_id is None:
            continue
        try:
            delegated_task_id = int(raw_task_id)
        except (TypeError, ValueError):
            continue
        if delegated_task_id <= 0 or delegated_task_id in seen_task_ids:
            continue
        seen_task_ids.add(delegated_task_id)
        child = db.get(Task, delegated_task_id)
        if not child or child.id == source_task.id:
            continue
        claimed_agent = str(item.get("agent_type") or "")
        if claimed_agent and claimed_agent != child.agent_type:
            continue
        decision_key = f"orchestrator-route:{source_task.id}:{child.id}"
        existing = db.scalar(
            select(OrchestratorDecision).where(
                OrchestratorDecision.decision_key == decision_key
            )
        )
        if existing:
            recorded.append(existing)
            continue
        normalized_agent = _task_type(child)
        row = OrchestratorDecision(
            decision_key=decision_key,
            source_task_id=source_task.id,
            delegated_task_id=child.id,
            task_type=normalized_agent,
            selected_agent=normalized_agent,
            expected_result=(
                f"Agent {normalized_agent} reaches done without an error or unmet integration requirement."
            ),
            expectation_status=_expectation_status(child, now),
            correlation_id=correlation_id,
            created_at=now,
        )
        db.add(row)
        db.flush()
        db.add(
            AuditLog(
                actor="orchestrator",
                action="orchestrator.routing_decision_recorded",
                resource_type="orchestrator_decision",
                resource_id=str(row.id),
                details={
                    "source_task_id": source_task.id,
                    "delegated_task_id": child.id,
                    "task_type": row.task_type,
                    "selected_agent": row.selected_agent,
                    "expectation_status": row.expectation_status,
                    "correlation_id": correlation_id,
                },
            )
        )
        recorded.append(row)
    return recorded


def measure_routing_outcome(db: Session, task: Task) -> OrchestratorDecision | None:
    row = db.scalar(
        select(OrchestratorDecision).where(
            OrchestratorDecision.delegated_task_id == task.id
        )
    )
    if not row or row.successful is not None:
        return row
    result = task.result or {}
    result_status = str(result.get("status") or "").lower()
    approval_is_pending = bool(
        task.status == "blocked"
        and (
            result.get("reason") == "owner_approval_required"
            or (result.get("blocked") is True and result.get("approval_id"))
        )
    )
    integration_is_unmet = bool(
        task.status == "blocked"
        and not approval_is_pending
        and (
            result.get("execution_gap")
            or result.get("error")
            or result_status in _MISSED_EXPECTATION_STATUSES
        )
    )
    if task.status not in {"done", "failed"} and not integration_is_unmet:
        return row
    if task.status == "failed" and task.attempts < task.max_attempts:
        return row
    successful = bool(
        task.status == "done"
        and not result.get("error")
        and result_status not in _MISSED_EXPECTATION_STATUSES
    )
    row.successful = successful
    row.outcome_status = "succeeded" if successful else "expectation_missed"
    row.measured_at = _now()
    db.add(
        AuditLog(
            actor="orchestrator",
            action="orchestrator.routing_outcome_measured",
            resource_type="orchestrator_decision",
            resource_id=str(row.id),
            details={
                "delegated_task_id": task.id,
                "selected_agent": row.selected_agent,
                "outcome_status": row.outcome_status,
                "successful": successful,
                "correlation_id": row.correlation_id,
            },
        )
    )
    db.flush()
    return row


def sync_routing_outcomes(db: Session) -> int:
    rows = db.scalars(
        select(OrchestratorDecision).where(OrchestratorDecision.successful.is_(None))
    ).all()
    measured = 0
    for row in rows:
        task = db.get(Task, row.delegated_task_id)
        if not task:
            continue
        was_pending = row.successful is None
        measure_routing_outcome(db, task)
        if was_pending and row.successful is not None:
            measured += 1
    return measured


def routing_decision_view(row: OrchestratorDecision) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_task_id": row.source_task_id,
        "delegated_task_id": row.delegated_task_id,
        "task_type": row.task_type,
        "selected_agent": row.selected_agent,
        "expected_result": row.expected_result,
        "expectation_status": row.expectation_status,
        "outcome_status": row.outcome_status,
        "successful": row.successful,
        "correlation_id": row.correlation_id,
        "created_at": row.created_at,
        "measured_at": row.measured_at,
    }
