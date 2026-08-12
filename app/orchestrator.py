from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from .models import AuditLog, Task
from .platform import agent_runtime, decision_engine, event_bus


def audit(db: Session, actor: str, action: str, resource_type: str, resource_id: str = "", details: dict | None = None) -> None:
    db.add(AuditLog(actor=actor, action=action, resource_type=resource_type, resource_id=resource_id, details=details or {}))


def dispatch(db: Session, task: Task) -> dict:
    policy = decision_engine.evaluate(db, task)
    if not policy["allowed"]:
        task.status = "blocked"
        result = {"blocked": True, **policy}
        task.result = result
        audit(db, "decision_engine", "task.blocked", "task", str(task.id), result)
        event_bus.publish(db, "approval.requested", "task", str(task.id), result, idempotency_key=f"task:{task.id}:approval:{policy['approval_id']}")
        return result
    try:
        result = agent_runtime.execute(db, task)
        audit(db, task.agent_type, "task.completed", "task", str(task.id), result)
        event_bus.publish(db, "task.completed", "task", str(task.id), result, idempotency_key=f"task:{task.id}:completed:{task.attempts}")
        return result
    except Exception as exc:
        audit(db, task.agent_type, "task.failed", "task", str(task.id), task.result)
        if task.attempts < task.max_attempts:
            task.status = "queued"
            task.next_retry_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=min(300, 2 ** task.attempts))
            task.run_after = task.next_retry_at
            event_bus.publish(db, "task.retry_scheduled", "task", str(task.id), {"attempt": task.attempts, "max_attempts": task.max_attempts, "next_retry_at": task.next_retry_at.isoformat()}, idempotency_key=f"task:{task.id}:retry:{task.attempts}")
        else:
            event_bus.publish(db, "task.failed", "task", str(task.id), task.result, idempotency_key=f"task:{task.id}:failed")
        return task.result


def run_next(db: Session) -> Task | None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    priority_order = case((Task.priority == "critical", 4), (Task.priority == "high", 3), (Task.priority == "normal", 2), (Task.priority == "low", 1), else_=0)
    task = db.scalar(select(Task).where(Task.status.in_(["open", "queued"]), Task.run_after <= now).order_by(priority_order.desc(), Task.id).with_for_update(skip_locked=True))
    if task:
        dispatch(db, task)
        db.commit()
    return task
