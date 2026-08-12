from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agents import AGENTS, heartbeat
from .models import AgentRun, ApprovalRequest, CompanyKnowledge, DomainEvent, Task


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EventBus:
    """Transactional outbox. Events are stored in the same transaction as domain data."""

    def publish(
        self,
        db: Session,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DomainEvent:
        key = idempotency_key or f"{event_type}:{aggregate_type}:{aggregate_id}:{uuid4()}"
        existing = db.scalar(select(DomainEvent).where(DomainEvent.idempotency_key == key))
        if existing:
            return existing
        row = DomainEvent(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload or {},
            metadata_json=metadata or {},
            idempotency_key=key,
        )
        db.add(row)
        db.flush()
        return row

    def next(self, db: Session) -> DomainEvent | None:
        return db.scalar(
            select(DomainEvent)
            .where(DomainEvent.status == "pending", DomainEvent.available_at <= now_utc())
            .order_by(DomainEvent.id)
            .with_for_update(skip_locked=True)
        )

    def complete(self, event: DomainEvent) -> None:
        event.status = "published"
        event.published_at = now_utc()
        event.last_error = ""

    def fail(self, event: DomainEvent, error: str) -> None:
        event.attempts += 1
        event.last_error = error
        event.status = "dead_letter" if event.attempts >= 5 else "pending"
        event.available_at = now_utc() + timedelta(seconds=min(300, 2 ** event.attempts))


class CompanyBrain:
    def remember(
        self,
        db: Session,
        namespace: str,
        key: str,
        value: dict[str, Any],
        *,
        source: str = "system",
        confidence: float = 1.0,
        valid_until: datetime | None = None,
    ) -> CompanyKnowledge:
        row = db.scalar(select(CompanyKnowledge).where(CompanyKnowledge.namespace == namespace, CompanyKnowledge.key == key))
        if row:
            row.value = value
            row.source = source
            row.confidence = max(0.0, min(1.0, confidence))
            row.valid_until = valid_until
            row.version += 1
        else:
            row = CompanyKnowledge(namespace=namespace, key=key, value=value, source=source, confidence=max(0.0, min(1.0, confidence)), valid_until=valid_until)
            db.add(row)
        db.flush()
        return row

    def snapshot(self, db: Session, namespace: str | None = None) -> dict[str, dict[str, Any]]:
        query = select(CompanyKnowledge).where(
            (CompanyKnowledge.valid_until.is_(None)) | (CompanyKnowledge.valid_until > now_utc())
        )
        if namespace:
            query = query.where(CompanyKnowledge.namespace == namespace)
        rows = db.scalars(query.order_by(CompanyKnowledge.namespace, CompanyKnowledge.key)).all()
        return {f"{row.namespace}.{row.key}": {"value": row.value, "confidence": row.confidence, "version": row.version, "source": row.source} for row in rows}


class ApprovalEngine:
    protected_actions = {"financial", "legal", "contract", "hr_final", "tender_submission", "bulk_outreach"}

    def request(self, db: Session, action_kind: str, resource_type: str, resource_id: str, requested_by: str, payload: dict[str, Any], rationale: str = "") -> ApprovalRequest:
        existing = db.scalar(select(ApprovalRequest).where(
            ApprovalRequest.action_kind == action_kind,
            ApprovalRequest.resource_type == resource_type,
            ApprovalRequest.resource_id == resource_id,
            ApprovalRequest.status == "pending",
        ))
        if existing:
            return existing
        row = ApprovalRequest(action_kind=action_kind, resource_type=resource_type, resource_id=resource_id, requested_by=requested_by, payload=payload, rationale=rationale)
        db.add(row)
        db.flush()
        return row

    def authorized(self, db: Session, action_kind: str | None, approval_id: int | None, resource_type: str | None = None, resource_id: str | None = None, expected_payload: dict[str, Any] | None = None) -> bool:
        if action_kind not in self.protected_actions:
            return True
        row = db.get(ApprovalRequest, approval_id) if approval_id else None
        return bool(row and row.action_kind == action_kind and row.status == "approved" and (resource_type is None or row.resource_type == resource_type) and (resource_id is None or row.resource_id == resource_id) and (expected_payload is None or row.payload == expected_payload))


class DecisionEngine:
    """Deterministic policy layer; an LLM may propose, but policy decides execution."""

    def evaluate(self, db: Session, task: Task) -> dict[str, Any]:
        action_kind = task.payload.get("action_kind")
        if action_kind in approval_engine.protected_actions:
            supplied_id = task.payload.get("approval_id")
            if approval_engine.authorized(db, action_kind, supplied_id, "task", str(task.id)):
                return {"allowed": True, "reason": "owner_approved", "approval_id": supplied_id}
            approval = approval_engine.request(db, action_kind, "task", str(task.id), task.agent_type, task.payload, "Protected business action")
            task.payload = {**task.payload, "approval_id": approval.id}
            if approval.status != "approved":
                return {"allowed": False, "reason": "owner_approval_required", "approval_id": approval.id}
        return {"allowed": True, "reason": "policy_passed"}


class AgentRuntime:
    def execute(self, db: Session, task: Task) -> dict[str, Any]:
        task.status = "running"
        task.attempts += 1
        agent = AGENTS.get(task.agent_type)
        if not agent:
            raise ValueError(f"Unknown agent: {task.agent_type}")
        run = AgentRun(agent_type=task.agent_type, task_id=task.id, correlation_id=str(task.payload.get("correlation_id", "")), input=task.payload)
        db.add(run)
        heartbeat(db, task.agent_type, "running")
        db.flush()
        try:
            result = agent.execute(db, task.payload)
            run.output = result
            run.evidence = result.get("evidence", []) if isinstance(result.get("evidence", []), list) else []
            run.cost = float(result.get("cost", 0) or 0)
            run.status = "succeeded"
            task.result = result
            task.status = "done"
            heartbeat(db, task.agent_type, "idle", metrics=result)
            return result
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            task.status = "failed"
            task.result = {"error": str(exc)}
            heartbeat(db, task.agent_type, "error", str(exc))
            raise
        finally:
            run.finished_at = now_utc()


event_bus = EventBus()
company_brain = CompanyBrain()
approval_engine = ApprovalEngine()
decision_engine = DecisionEngine()
agent_runtime = AgentRuntime()


DOMAIN_AGENT = {"lead": "sales", "tender": "tender", "candidate": "hr", "cashflow": "finance", "expense": "finance", "payment": "finance", "campaign": "marketing"}


def route_event(db: Session, event: DomainEvent) -> Task | None:
    agent_type = DOMAIN_AGENT.get(event.aggregate_type)
    if not agent_type:
        return None
    task = Task(
        title=f"Process {event.event_type} #{event.aggregate_id}",
        agent_type=agent_type,
        status="queued",
        payload={"event_id": event.id, "event_type": event.event_type, "record_id": event.aggregate_id, "correlation_id": event.metadata_json.get("correlation_id", "")},
    )
    db.add(task)
    db.flush()
    return task


def process_next_event(db: Session) -> DomainEvent | None:
    event = event_bus.next(db)
    if not event:
        return None
    try:
        route_event(db, event)
        event_bus.complete(event)
        db.commit()
    except Exception as exc:
        db.rollback()
        event = db.get(DomainEvent, event.id)
        if event:
            event_bus.fail(event, str(exc))
            db.commit()
        raise
    return event
