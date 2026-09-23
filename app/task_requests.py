from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import DomainEvent, Task
from .orchestrator import audit
from .platform import event_bus
from .schemas import TaskCreate
from .task_state import record_task_created


class TaskRequestConflict(ValueError):
    pass


class _ConcurrentReplay(Exception):
    pass


def create_requested_task(
    db: Session, payload: TaskCreate, *, actor: str, idempotency_key: str | None = None,
) -> Task:
    def new_task() -> Task:
        row = Task(**payload.model_dump(exclude_none=True))
        db.add(row)
        db.flush()
        record_task_created(db, row, actor=actor)
        audit(db, actor, "task.created", "task", str(row.id), {"agent_type": row.agent_type})
        return row

    if idempotency_key is None:
        return new_task()
    digest = hashlib.sha256(json.dumps(payload.model_dump(mode="json", exclude_none=True),
                                      sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    key = "task-request:" + hashlib.sha256(json.dumps([actor, idempotency_key]).encode()).hexdigest()

    def existing_task() -> Task | None:
        if idempotency_key is None:
            return None
        event = db.scalar(select(DomainEvent).where(DomainEvent.idempotency_key == key))
        if event is None:
            return None
        if event.payload.get("request_digest") != digest:
            raise TaskRequestConflict("Idempotency key was already used for a different task")
        row = db.get(Task, int(event.aggregate_id))
        if row is None:
            raise TaskRequestConflict("Original task is unavailable")
        return row

    prior = existing_task()
    if prior is not None:
        return prior
    try:
        with db.begin_nested():
            row = new_task()
            event = event_bus.publish(
                db, "task.requested", "task", str(row.id), {"request_digest": digest},
                idempotency_key=key, actor=actor,
            )
            if event.aggregate_id != str(row.id):
                raise _ConcurrentReplay()
        return row
    except (IntegrityError, _ConcurrentReplay):
        prior = existing_task()
        if prior is None:
            raise
        return prior
