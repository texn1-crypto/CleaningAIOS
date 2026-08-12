from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agents import AGENTS, heartbeat
from .models import ApprovalKind, AuditLog, Decision, Task


CRITICAL_ACTIONS = {item.value for item in ApprovalKind}


def audit(db: Session, actor: str, action: str, resource_type: str, resource_id: str = "", details: dict | None = None) -> None:
    db.add(AuditLog(actor=actor, action=action, resource_type=resource_type, resource_id=resource_id, details=details or {}))


def dispatch(db: Session, task: Task) -> dict:
    if task.payload.get("action_kind") in CRITICAL_ACTIONS:
        approval_id = task.payload.get("approval_id")
        approval = db.get(Decision, approval_id) if approval_id else None
        if not approval or approval.status != "approved":
            task.status = "blocked"
            result = {"blocked": True, "reason": "owner_approval_required", "approval_id": approval_id}
            task.result = result
            audit(db, "orchestrator", "task.blocked", "task", str(task.id), result)
            return result
    agent = AGENTS.get(task.agent_type)
    if not agent:
        raise ValueError(f"Unknown agent: {task.agent_type}")
    task.status = "running"
    task.attempts += 1
    heartbeat(db, task.agent_type, "running")
    db.flush()
    try:
        result = agent.execute(db, task.payload)
        task.result = result
        task.status = "done"
        heartbeat(db, task.agent_type, "idle", metrics=result)
        audit(db, task.agent_type, "task.completed", "task", str(task.id), result)
        return result
    except Exception as exc:
        task.status = "failed"
        task.result = {"error": str(exc)}
        heartbeat(db, task.agent_type, "error", str(exc))
        audit(db, task.agent_type, "task.failed", "task", str(task.id), task.result)
        raise


def run_next(db: Session) -> Task | None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    task = db.scalar(select(Task).where(Task.status.in_(["open", "queued"]), Task.run_after <= now).order_by(Task.priority.desc(), Task.id).with_for_update(skip_locked=True))
    if task:
        dispatch(db, task)
        db.commit()
    return task
