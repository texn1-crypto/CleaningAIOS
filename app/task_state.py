from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Task, TaskTransition


TASK_STATES = frozenset({"open", "queued", "running", "blocked", "done", "failed"})
ALLOWED_TRANSITIONS = {
    "open": frozenset({"queued", "running", "blocked", "done", "failed"}),
    "queued": frozenset({"running", "blocked", "done", "failed"}),
    "running": frozenset({"queued", "blocked", "done", "failed"}),
    "blocked": frozenset({"queued", "failed"}),
    "failed": frozenset({"queued"}),
    "done": frozenset(),
}


class InvalidTaskTransition(ValueError):
    """Raised when a command violates the persisted task state machine."""


def task_execution_lock_query(task_id: int):
    """Build the row-locking query shared by competing task executors."""

    return select(Task).where(Task.id == task_id).with_for_update()


def record_task_created(
    db: Session,
    task: Task,
    *,
    actor: str,
    reason: str = "task_created",
    correlation_id: str = "",
) -> TaskTransition:
    if task.status not in TASK_STATES:
        raise InvalidTaskTransition(f"Unknown initial task status: {task.status}")
    db.add(task)
    db.flush()
    key = f"task:{task.id}:created"
    existing = db.scalar(select(TaskTransition).where(TaskTransition.transition_key == key))
    if existing:
        return existing
    row = TaskTransition(
        task_id=task.id,
        from_status="",
        to_status=task.status,
        actor=actor[:128] or "system",
        reason=reason[:255],
        correlation_id=correlation_id[:128],
        details={},
        transition_key=key,
    )
    db.add(row)
    db.flush()
    return row


def transition_task(
    db: Session,
    task: Task,
    to_status: str,
    *,
    actor: str,
    reason: str,
    correlation_id: str = "",
    details: dict[str, Any] | None = None,
    transition_key: str | None = None,
) -> TaskTransition | None:
    from_status = task.status
    if to_status not in TASK_STATES:
        raise InvalidTaskTransition(f"Unknown target task status: {to_status}")
    if from_status == to_status:
        return None
    if from_status not in ALLOWED_TRANSITIONS or to_status not in ALLOWED_TRANSITIONS[from_status]:
        raise InvalidTaskTransition(f"Task transition {from_status} -> {to_status} is not allowed")
    key = transition_key or f"task:{task.id}:{from_status}:{to_status}:{uuid4()}"
    existing = db.scalar(select(TaskTransition).where(TaskTransition.transition_key == key))
    if existing:
        if existing.task_id != task.id or existing.from_status != from_status or existing.to_status != to_status:
            raise InvalidTaskTransition("Transition idempotency key is already bound to another state change")
        return existing
    row = TaskTransition(
        task_id=task.id,
        from_status=from_status,
        to_status=to_status,
        actor=actor[:128] or "system",
        reason=reason[:255],
        correlation_id=correlation_id[:128],
        details=details or {},
        transition_key=key[:255],
    )
    task.status = to_status
    db.add(row)
    db.flush()
    return row
