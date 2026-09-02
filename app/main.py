from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
import logging
from pathlib import Path
import re
import time
from typing import Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, SessionLocal, engine
from .domains import module_summary, validate_record
from .models import AgentRun, AgentState, ApprovalRequest, AuditLog, BusinessRecord, ContactEvent, ContentItem, Decision, DomainEvent, EventConsumerReceipt, MessageTemplate, OutboundMessage, SenderMailbox, Suppression, Task, TaskTransition
from .orchestrator import audit, dispatch
from .platform import company_brain, event_bus
from .schemas import ApprovalDecision, ContactEventCreate, DecisionCreate, KnowledgeCreate, LeadAutopilotCreate, OutreachCreate, RecordCreate, RecordUpdate, SuppressionCreate, TaskCreate
from .security import Principal, principal, require_role, valid_unsubscribe_token, validate_production_security
from .api_v2 import router as api_v2_router
from .marketing_api import router as marketing_router
from .public_api import router as public_router
from .mission_control import MISSION_CONTROL_HTML
from .public_site import PUBLIC_SITE_HTML, privacy_html
from .site_pages import about_html, contacts_html, journal_html, prices_html, service_html, services_html
from .agents import AGENTS
from .llm import llm_advisor, prompt_deployment_catalog
from .readiness import integration_status
from .task_state import InvalidTaskTransition, record_task_created, transition_task
from .observability import agent_observability_snapshot, prometheus_metrics
from .logging_config import configure_logging, request_correlation_id
from .approval_service import (
    ApprovalConflict,
    ApprovalError,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalStale,
    decide_approval as decide_approval_service,
)
from .agent_replay import (
    ReplayConflict,
    ReplayError,
    ReplayNotFound,
    request_agent_replay,
)
from .agent_tools import agent_tool_catalog


configure_logging("web")
log = logging.getLogger("cleaningai.web")


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_production_security()
    if not settings.production:
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title=settings.app_name, version="2.1.0", lifespan=lifespan)
app.include_router(api_v2_router)
app.include_router(marketing_router)
app.include_router(public_router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.middleware("http")
async def request_observability(request: Request, call_next):
    supplied = request.headers.get("X-Correlation-ID", "")
    correlation_id = (
        supplied
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied)
        else uuid4().hex
    )
    token = request_correlation_id.set(correlation_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        log.info(
            "HTTP request completed",
            extra={
                "event": "http.request.completed",
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        response.headers["X-Correlation-ID"] = correlation_id
        return response
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        log.exception(
            "HTTP request failed",
            extra={
                "event": "http.request.failed",
                "method": request.method,
                "path": request.url.path,
                "duration_ms": duration_ms,
            },
        )
        raise
    finally:
        request_correlation_id.reset(token)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def as_task(row: Task) -> dict:
    return {"id": row.id, "title": row.title, "status": row.status, "priority": row.priority, "agent_type": row.agent_type, "assigned_to": row.assigned_to, "payload": row.payload, "result": row.result, "attempts": row.attempts, "max_attempts": row.max_attempts, "timeout_seconds": row.timeout_seconds, "run_after": row.run_after, "due_at": row.due_at, "next_retry_at": row.next_retry_at}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "app": settings.app_name, "version": "2.1.0", "database": "ok"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.get("/api/integrations")
def integrations(_: Principal = Depends(principal)):
    return integration_status()


@app.post("/api/leads/autopilot", status_code=201)
def create_autopilot_lead(
    payload: LeadAutopilotCreate,
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
):
    require_role(actor, "operator")
    from .lead_autopilot import LeadRateLimitExceeded, intake_telegram_lead

    try:
        return intake_telegram_lead(db, payload)
    except LeadRateLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    open_tasks = db.scalar(select(func.count(Task.id)).where(Task.status.in_(["open", "queued", "running"]))) or 0
    pending = db.scalar(select(func.count(Decision.id)).where(Decision.status == "pending")) or 0
    failed = db.scalar(select(func.count(Task.id)).where(Task.status == "failed")) or 0
    agents = db.scalars(select(AgentState).order_by(AgentState.agent_type)).all()
    pending_approvals = db.scalar(select(func.count(ApprovalRequest.id)).where(ApprovalRequest.status == "pending")) or 0
    return {"company_health": max(0, 100 - failed * 10 - (pending + pending_approvals) * 5), "open_tasks": open_tasks, "pending_decisions": pending, "pending_approvals": pending_approvals, "failed_tasks": failed, "modules": module_summary(db), "agents": [{"type": x.agent_type, "status": x.status, "last_heartbeat_at": x.last_heartbeat_at, "last_error": x.last_error} for x in agents]}


@app.get("/api/tasks")
def list_tasks(status: Optional[str] = None, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    query = select(Task).order_by(Task.id.desc())
    if status:
        query = query.where(Task.status == status)
    return [as_task(x) for x in db.scalars(query).all()]


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    if payload.agent_type not in AGENTS: raise HTTPException(422, f"Unknown agent: {payload.agent_type}")
    row = Task(**payload.model_dump(exclude_none=True))
    db.add(row); db.flush(); record_task_created(db, row, actor=actor.subject); audit(db, actor.subject, "task.created", "task", str(row.id), {"agent_type": row.agent_type}); db.commit(); db.refresh(row)
    return as_task(row)


@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(Task, task_id)
    if not row: raise HTTPException(404, "Task not found")
    if row.status not in {"open", "queued"}:
        raise HTTPException(409, f"Task in status {row.status} cannot be run")
    dispatch(db, row); db.commit(); db.refresh(row)
    return as_task(row)


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(Task, task_id)
    if not row: raise HTTPException(404, "Task not found")
    try:
        transition_task(db, row, "done", actor=actor.subject, reason="manual_completion")
    except InvalidTaskTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    audit(db, actor.subject, "task.completed_manually", "task", str(row.id)); db.commit()
    return {"ok": True}


@app.get("/api/tasks/{task_id}/transitions")
def task_transitions(task_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    if not db.get(Task, task_id):
        raise HTTPException(404, "Task not found")
    rows = db.scalars(select(TaskTransition).where(TaskTransition.task_id == task_id).order_by(TaskTransition.id)).all()
    return [{
        "id": row.id,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "actor": row.actor,
        "reason": row.reason,
        "correlation_id": row.correlation_id,
        "details": row.details,
        "created_at": row.created_at,
    } for row in rows]


@app.get("/api/decisions")
def list_decisions(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    rows = db.scalars(select(Decision).order_by(Decision.id.desc())).all()
    return [{"id": x.id, "title": x.title, "rationale": x.rationale, "kind": x.kind, "status": x.status, "payload": x.payload} for x in rows]


@app.post("/api/decisions", status_code=201)
def create_decision(payload: DecisionCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = Decision(**payload.model_dump(), requested_by=actor.subject)
    db.add(row); db.flush(); audit(db, actor.subject, "decision.requested", "decision", str(row.id), {"kind": row.kind}); db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status}


@app.post("/api/decisions/{decision_id}/{action}")
def decide(decision_id: int, action: str, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "owner")
    if action not in {"approve", "reject", "defer"}: raise HTTPException(400, "Unsupported action")
    row = db.get(Decision, decision_id)
    if not row: raise HTTPException(404, "Decision not found")
    row.status = {"approve": "approved", "reject": "rejected", "defer": "deferred"}[action]
    row.decided_by = actor.subject; row.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    audit(db, actor.subject, f"decision.{row.status}", "decision", str(row.id)); db.commit()
    return {"ok": True, "status": row.status}


@app.get("/api/records")
def records(record_type: Optional[str] = Query(None), db: Session = Depends(get_db), _: Principal = Depends(principal)):
    query = select(BusinessRecord).order_by(BusinessRecord.id.desc())
    if record_type: query = query.where(BusinessRecord.record_type == record_type)
    return [{"id": x.id, "record_type": x.record_type, "title": x.title, "status": x.status, "score": x.score, "owner": x.owner, "data": x.data, "source": x.source, "deadline_at": x.deadline_at} for x in db.scalars(query).all()]


@app.get("/api/proposals/{proposal_id}/download")
def download_proposal(proposal_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(BusinessRecord, proposal_id)
    if not row or row.record_type != "proposal" or row.status not in {"ready", "approved", "sent"}:
        raise HTTPException(404, "Ready proposal not found")
    storage_root = Path(settings.document_storage_path).resolve()
    path = Path(str(row.data.get("storage_path", ""))).resolve()
    try:
        path.relative_to(storage_root)
    except ValueError:
        raise HTTPException(403, "Proposal path is outside document storage")
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise HTTPException(404, "Proposal PDF is unavailable")
    audit(db, actor.subject, "proposal.downloaded", "proposal", str(row.id), {"client_record_id": row.data.get("lead_id")})
    db.commit()
    return FileResponse(path, media_type="application/pdf", filename=str(row.data.get("filename") or path.name))


@app.get("/api/proposal-revisions/{revision_id}/files/{file_kind}")
def download_proposal_revision_file(
    revision_id: int,
    file_kind: str,
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
):
    require_role(actor, "operator")
    if file_kind not in {"docx", "pdf"}:
        raise HTTPException(404, "Proposal revision format not found")
    row = db.get(BusinessRecord, revision_id)
    if not row or row.record_type != "proposal_revision" or row.status not in {"ready", "approved"}:
        raise HTTPException(404, "Ready proposal revision not found")
    item = (row.data.get("files") or {}).get(file_kind) or {}
    storage_root = Path(settings.document_storage_path).resolve()
    path = Path(str(item.get("storage_path", ""))).resolve()
    try:
        path.relative_to(storage_root)
    except ValueError:
        raise HTTPException(403, "Proposal revision path is outside document storage")
    expected_suffix = f".{file_kind}"
    if not path.is_file() or path.suffix.lower() != expected_suffix:
        raise HTTPException(404, "Proposal revision file is unavailable")
    media_type = "application/pdf" if file_kind == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    audit(db, actor.subject, "proposal_revision.downloaded", "proposal_revision", str(row.id), {"format": file_kind})
    db.commit()
    return FileResponse(path, media_type=media_type, filename=str(item.get("filename") or path.name))


@app.post("/api/records", status_code=201)
def create_record(payload: RecordCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    validate_record(payload.record_type, payload.status, payload.data, payload.deadline_at)
    row = BusinessRecord(**payload.model_dump())
    db.add(row); db.flush(); audit(db, actor.subject, "record.created", payload.record_type, str(row.id))
    event_bus.publish(db, f"{payload.record_type}.created", payload.record_type, str(row.id), {"title": row.title, "status": row.status, "score": row.score}, idempotency_key=f"record:{payload.record_type}:{row.id}:created", metadata={"actor": actor.subject})
    db.commit(); db.refresh(row)
    return {"id": row.id, "record_type": row.record_type, "status": row.status}


@app.patch("/api/records/{record_id}")
def update_record(record_id: int, payload: RecordUpdate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(BusinessRecord, record_id)
    if not row:
        raise HTTPException(404, "Record not found")
    changes = payload.model_dump(exclude_unset=True)
    old_status = row.status
    candidate_data = {**row.data, **(changes.get("data") or {})}
    candidate_status = changes.get("status", row.status)
    candidate_deadline = changes.get("deadline_at", row.deadline_at)
    validate_record(row.record_type, candidate_status, candidate_data, candidate_deadline)
    for field, value in changes.items():
        setattr(row, field, candidate_data if field == "data" else value)
    event_type = f"{row.record_type}.status_changed" if row.status != old_status else f"{row.record_type}.updated"
    event = event_bus.publish(db, event_type, row.record_type, str(row.id), {"old_status": old_status, "status": row.status, "changes": sorted(changes)}, metadata={"actor": actor.subject})
    audit(db, actor.subject, event_type, row.record_type, str(row.id), event.payload)
    db.commit(); db.refresh(row)
    return {"id": row.id, "record_type": row.record_type, "status": row.status, "data": row.data, "event_id": event.id}


@app.get("/api/records/{record_id}/contacts")
def record_contacts(record_id: int, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    if not db.get(BusinessRecord, record_id):
        raise HTTPException(404, "Record not found")
    rows = db.scalars(select(ContactEvent).where(ContactEvent.record_id == record_id).order_by(ContactEvent.id.desc())).all()
    return [{"id": x.id, "channel": x.channel, "direction": x.direction, "subject": x.subject, "body": x.body, "outcome": x.outcome, "created_at": x.created_at} for x in rows]


@app.post("/api/records/{record_id}/contacts", status_code=201)
def add_record_contact(record_id: int, payload: ContactEventCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    record = db.get(BusinessRecord, record_id)
    if not record:
        raise HTTPException(404, "Record not found")
    row = ContactEvent(record_id=record_id, **payload.model_dump())
    db.add(row); db.flush()
    event_bus.publish(db, f"{record.record_type}.contact_recorded", record.record_type, str(record.id), {"contact_id": row.id, "channel": row.channel, "direction": row.direction, "outcome": row.outcome})
    audit(db, actor.subject, "contact.recorded", record.record_type, str(record.id), {"contact_id": row.id})
    db.commit(); db.refresh(row)
    return {"id": row.id, "record_id": row.record_id}


@app.get("/api/modules/summary")
def modules_summary(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    return module_summary(db)


@app.get("/api/events")
def list_events(status: Optional[str] = None, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    query = select(DomainEvent).order_by(DomainEvent.id.desc())
    if status:
        query = query.where(DomainEvent.status == status)
    rows = db.scalars(query.limit(200)).all()
    receipts = db.scalars(select(EventConsumerReceipt).where(EventConsumerReceipt.event_id.in_([x.id for x in rows]))).all() if rows else []
    by_event: dict[int, list[dict]] = {}
    for receipt in receipts:
        by_event.setdefault(receipt.event_id, []).append({
            "consumer": receipt.consumer,
            "status": receipt.status,
            "attempts": receipt.attempts,
            "result_ref": receipt.result_ref,
            "last_error": receipt.last_error,
            "processed_at": receipt.processed_at,
        })
    return [{
        "id": x.id,
        "event_id": x.event_id,
        "event_type": x.event_type,
        "schema_version": x.schema_version,
        "aggregate_type": x.aggregate_type,
        "aggregate_id": x.aggregate_id,
        "correlation_id": x.correlation_id,
        "causation_id": x.causation_id,
        "actor": x.actor,
        "status": x.status,
        "attempts": x.attempts,
        "occurred_at": x.occurred_at,
        "created_at": x.created_at,
        "deliveries": by_event.get(x.id, []),
    } for x in rows]


@app.get("/api/brain")
def brain(namespace: Optional[str] = None, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    return company_brain.snapshot(db, namespace)


@app.put("/api/brain", status_code=201)
def remember(payload: KnowledgeCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    row = company_brain.remember(db, **payload.model_dump())
    event_bus.publish(db, "knowledge.updated", "knowledge", str(row.id), {"namespace": row.namespace, "key": row.key, "version": row.version}, idempotency_key=f"knowledge:{row.id}:v{row.version}")
    audit(db, actor.subject, "knowledge.updated", "knowledge", str(row.id), {"namespace": row.namespace, "key": row.key, "version": row.version})
    db.commit()
    return {"id": row.id, "version": row.version}


@app.get("/api/agent-runs")
def agent_runs(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    rows = db.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(200)).all()
    return [{"id": x.id, "agent_type": x.agent_type, "task_id": x.task_id, "status": x.status, "output": x.output, "error": x.error, "started_at": x.started_at, "finished_at": x.finished_at} for x in rows]


@app.post("/api/agent-runs/{run_id}/replay", status_code=201)
def replay_agent_run(
    run_id: int,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
):
    require_role(actor, "manager")
    try:
        result = request_agent_replay(
            db,
            run_id=run_id,
            idempotency_key=idempotency_key,
            requested_by=actor.subject,
        )
    except ReplayNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ReplayConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ReplayError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return result


@app.get("/api/ai/prompts")
def ai_prompt_deployments(actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    return prompt_deployment_catalog()


@app.get("/api/agent-tools")
def read_only_agent_tools(actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    return agent_tool_catalog()


@app.get("/api/observability/agents")
def agent_observability(
    window_hours: int = Query(default=settings.agent_slo_window_hours, ge=1, le=7 * 24),
    db: Session = Depends(get_db),
    _: Principal = Depends(principal),
):
    return agent_observability_snapshot(db, window_hours=window_hours)


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    snapshot = agent_observability_snapshot(db)
    return PlainTextResponse(
        prometheus_metrics(snapshot),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/api/approvals")
def approvals(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    rows = db.scalars(select(ApprovalRequest).order_by(ApprovalRequest.id.desc())).all()
    return [{
        "id": x.id,
        "action_kind": x.action_kind,
        "resource_type": x.resource_type,
        "resource_id": x.resource_id,
        "status": x.status,
        "rationale": x.rationale,
        "payload": x.payload,
        "requested_by": x.requested_by,
        "decision_version": x.decision_version,
        "expires_at": x.expires_at,
        "created_at": x.created_at,
    } for x in rows]


@app.post("/api/approvals/{approval_id}/{action}")
def decide_approval(approval_id: int, action: str, payload: ApprovalDecision, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "owner")
    try:
        result = decide_approval_service(
            db,
            approval_id=approval_id,
            action=action,
            note=payload.note,
            actor=actor,
        )
    except ApprovalNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ApprovalExpired as exc:
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    except (ApprovalConflict, ApprovalStale) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return result


@app.post("/api/outreach/suppress", status_code=201)
def suppress(payload: SuppressionCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = Suppression(address=str(payload.address).lower(), reason=payload.reason)
    db.merge(row); audit(db, actor.subject, "outreach.suppressed", "email", row.address, {"reason": row.reason}); db.commit()
    return {"address": row.address, "suppressed": True}


@app.get("/api/outreach/unsubscribe")
def unsubscribe(email: str, token: str = "", db: Session = Depends(get_db)):
    if not valid_unsubscribe_token(email, token):
        raise HTTPException(403, "Invalid unsubscribe token")
    row = Suppression(address=email.lower(), reason="unsubscribe")
    db.merge(row); audit(db, email.lower(), "outreach.unsubscribed", "email", email.lower()); db.commit()
    return {"unsubscribed": True}


@app.post("/api/outreach/messages", status_code=201)
def queue_message(payload: OutreachCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    recipient = str(payload.recipient).lower()
    if db.get(Suppression, recipient): raise HTTPException(409, "Recipient is suppressed")
    if payload.mailbox_id and not db.get(SenderMailbox, payload.mailbox_id): raise HTTPException(404, "Mailbox not found")
    if payload.template_id and not db.get(MessageTemplate, payload.template_id): raise HTTPException(404, "Template not found")
    row = OutboundMessage(campaign_key=payload.campaign_key, recipient=recipient, subject=payload.subject, body=payload.body, scheduled_at=payload.scheduled_at or datetime.now(timezone.utc).replace(tzinfo=None))
    row.mailbox_id = payload.mailbox_id; row.template_id = payload.template_id; row.attachments = payload.attachments
    db.add(row)
    try: db.flush()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Duplicate campaign recipient")
    audit(db, actor.subject, "outreach.queued", "outbound_message", str(row.id), {"recipient": recipient}); db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status}


@app.get("/api/audit")
def audit_log(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{"id": x.id, "actor": x.actor, "action": x.action, "resource_type": x.resource_type, "resource_id": x.resource_id, "details": x.details, "created_at": x.created_at} for x in rows]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    base = str(request.base_url).rstrip("/")
    return (
        PUBLIC_SITE_HTML
        .replace("__OG_IMAGE_URL__", escape(base + "/static/og-cleaningaios.png", quote=True))
        .replace("__COMPANY_NAME__", escape(settings.company_name, quote=True))
    )


@app.get("/mission-control", response_class=HTMLResponse)
def mission_control():
    return MISSION_CONTROL_HTML


@app.get("/services", response_class=HTMLResponse)
def services_page():
    return services_html()


@app.get("/services/{slug}", response_class=HTMLResponse)
def service_page(slug: str):
    page = service_html(slug)
    if page is None:
        raise HTTPException(404, "Service page not found")
    return page


@app.get("/prices", response_class=HTMLResponse)
def prices_page():
    return prices_html()


@app.get("/about", response_class=HTMLResponse)
def about_page():
    return about_html()


@app.get("/contacts", response_class=HTMLResponse)
def contacts_page():
    return contacts_html()


@app.get("/journal", response_class=HTMLResponse)
def journal_page():
    return journal_html()


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return privacy_html()


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /docs\nDisallow: /mission-control\nSitemap: " + settings.public_base_url.rstrip("/") + "/sitemap.xml\n"


@app.get("/sitemap.xml")
def sitemap():
    base = settings.public_base_url.rstrip("/")
    from .site_pages import SERVICE_DETAILS

    paths = ["/", "/services", "/prices", "/about", "/contacts", "/journal", "/privacy"]
    paths.extend(f"/services/{slug}" for slug in SERVICE_DETAILS)
    urls = "".join(f"<url><loc>{base}{path}</loc></url>" for path in paths)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")
