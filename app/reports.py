from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import AgentRun, AgentState, ApprovalRequest, DomainEvent, ImprovementRequest, Task


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _count(db: Session, model: type, *criteria: Any) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def build_activity_report(db: Session, *, period_hours: int = 24) -> dict[str, Any]:
    """Build a verifiable read-only report from the shared operational database."""
    hours = max(1, min(int(period_hours), 168))
    generated_at = _utcnow()
    cutoff = generated_at - timedelta(hours=hours)

    running_reports = db.scalars(
        select(Task).where(Task.status == "running", Task.agent_type == "orchestrator")
    ).all()
    current_report_count = sum(
        1 for row in running_reports if (row.payload or {}).get("action") == "system_activity_report"
    )

    completed = db.scalars(
        select(Task)
        .where(Task.status == "done", Task.updated_at >= cutoff)
        .order_by(Task.updated_at.desc(), Task.id.desc())
    ).all()
    recent_completed = [row for row in completed if row.agent_type != "request_analyst"][:5]

    active_total = _count(db, Task, Task.status.in_(["open", "queued", "running"]))
    summary = {
        "tasks_completed": len(completed),
        "business_tasks_completed": sum(row.agent_type != "request_analyst" for row in completed),
        "tasks_active": max(0, active_total - current_report_count),
        "tasks_failed": _count(db, Task, Task.status == "failed"),
        "tasks_blocked": _count(db, Task, Task.status == "blocked"),
        "agent_runs_succeeded": _count(
            db, AgentRun, AgentRun.status == "succeeded", AgentRun.finished_at >= cutoff
        ),
        "agent_runs_failed": _count(
            db, AgentRun, AgentRun.status == "failed", AgentRun.finished_at >= cutoff
        ),
        "queued_improvements": _count(db, ImprovementRequest, ImprovementRequest.status == "queued"),
        "implemented_improvements": _count(
            db,
            ImprovementRequest,
            ImprovementRequest.status == "implemented",
            ImprovementRequest.updated_at >= cutoff,
        ),
        "pending_approvals": _count(db, ApprovalRequest, ApprovalRequest.status == "pending"),
        "events_pending": _count(db, DomainEvent, DomainEvent.status == "pending"),
        "events_dead_letter": _count(db, DomainEvent, DomainEvent.status == "dead_letter"),
    }

    agents = db.scalars(select(AgentState).order_by(AgentState.agent_type)).all()
    agent_statuses = [
        {
            "agent_type": row.agent_type,
            "status": row.status,
            "last_heartbeat_at": row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None,
            "last_error": row.last_error,
        }
        for row in agents
    ]
    blockers = []
    if summary["tasks_failed"]:
        blockers.append(f"Задач с ошибкой: {summary['tasks_failed']}")
    if summary["tasks_blocked"]:
        blockers.append(f"Заблокированных задач: {summary['tasks_blocked']}")
    if summary["events_dead_letter"]:
        blockers.append(f"Событий в dead-letter: {summary['events_dead_letter']}")
    if summary["pending_approvals"]:
        blockers.append(f"Подтверждений владельца ожидают: {summary['pending_approvals']}")

    task_evidence = [
        {
            "type": "completed_task",
            "task_id": row.id,
            "agent_type": row.agent_type,
            "status": row.status,
            "updated_at": row.updated_at.isoformat(),
        }
        for row in recent_completed
    ]
    return {
        "outcome": "completed",
        "report_kind": "system_activity",
        "period_hours": hours,
        "generated_at": generated_at.isoformat(),
        "summary": summary,
        "recent_completed_tasks": [
            {
                "id": row.id,
                "title": row.title,
                "agent_type": row.agent_type,
                "status": row.status,
                "updated_at": row.updated_at.isoformat(),
            }
            for row in recent_completed
        ],
        "agent_statuses": agent_statuses,
        "blockers": blockers,
        "evidence": [
            {
                "type": "database_snapshot",
                "generated_at": generated_at.isoformat(),
                "period_hours": hours,
                **summary,
            },
            *task_evidence,
        ],
    }

