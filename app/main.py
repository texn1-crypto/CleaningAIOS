from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, SessionLocal, engine
from .models import AgentState, AuditLog, BusinessRecord, Decision, OutboundMessage, Suppression, Task
from .orchestrator import audit, dispatch
from .schemas import DecisionCreate, OutreachCreate, RecordCreate, SuppressionCreate, TaskCreate
from .security import Principal, principal, require_role


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.production:
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def as_task(row: Task) -> dict:
    return {"id": row.id, "title": row.title, "status": row.status, "priority": row.priority, "agent_type": row.agent_type, "payload": row.payload, "result": row.result, "run_after": row.run_after}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "app": settings.app_name, "version": "1.0.0", "database": "ok"}


@app.get("/ready")
def ready(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    open_tasks = db.scalar(select(func.count(Task.id)).where(Task.status.in_(["open", "queued", "running"]))) or 0
    pending = db.scalar(select(func.count(Decision.id)).where(Decision.status == "pending")) or 0
    failed = db.scalar(select(func.count(Task.id)).where(Task.status == "failed")) or 0
    agents = db.scalars(select(AgentState).order_by(AgentState.agent_type)).all()
    return {"company_health": max(0, 100 - failed * 10 - pending * 5), "open_tasks": open_tasks, "pending_decisions": pending, "failed_tasks": failed, "agents": [{"type": x.agent_type, "status": x.status, "last_heartbeat_at": x.last_heartbeat_at, "last_error": x.last_error} for x in agents]}


@app.get("/api/tasks")
def list_tasks(status: Optional[str] = None, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    query = select(Task).order_by(Task.id.desc())
    if status:
        query = query.where(Task.status == status)
    return [as_task(x) for x in db.scalars(query).all()]


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = Task(**payload.model_dump(exclude_none=True))
    db.add(row); db.flush(); audit(db, actor.subject, "task.created", "task", str(row.id), {"agent_type": row.agent_type}); db.commit(); db.refresh(row)
    return as_task(row)


@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(Task, task_id)
    if not row: raise HTTPException(404, "Task not found")
    dispatch(db, row); db.commit(); db.refresh(row)
    return as_task(row)


@app.post("/api/tasks/{task_id}/complete")
def complete_task(task_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(Task, task_id)
    if not row: raise HTTPException(404, "Task not found")
    row.status = "done"; audit(db, actor.subject, "task.completed_manually", "task", str(row.id)); db.commit()
    return {"ok": True}


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


@app.post("/api/records", status_code=201)
def create_record(payload: RecordCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = BusinessRecord(**payload.model_dump())
    db.add(row); db.flush(); audit(db, actor.subject, "record.created", payload.record_type, str(row.id)); db.commit(); db.refresh(row)
    return {"id": row.id, "record_type": row.record_type, "status": row.status}


@app.post("/api/outreach/suppress", status_code=201)
def suppress(payload: SuppressionCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = Suppression(address=str(payload.address).lower(), reason=payload.reason)
    db.merge(row); audit(db, actor.subject, "outreach.suppressed", "email", row.address, {"reason": row.reason}); db.commit()
    return {"address": row.address, "suppressed": True}


@app.get("/api/outreach/unsubscribe")
def unsubscribe(email: str, db: Session = Depends(get_db)):
    row = Suppression(address=email.lower(), reason="unsubscribe")
    db.merge(row); audit(db, email.lower(), "outreach.unsubscribed", "email", email.lower()); db.commit()
    return {"unsubscribed": True}


@app.post("/api/outreach/messages", status_code=201)
def queue_message(payload: OutreachCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    recipient = str(payload.recipient).lower()
    if db.get(Suppression, recipient): raise HTTPException(409, "Recipient is suppressed")
    row = OutboundMessage(campaign_key=payload.campaign_key, recipient=recipient, subject=payload.subject, body=payload.body, scheduled_at=payload.scheduled_at or datetime.now(timezone.utc).replace(tzinfo=None))
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
def index():
    return """<!doctype html><html lang='ru'><meta charset='utf-8'><title>CleaningAI OS</title><style>body{font:16px system-ui;background:#0f172a;color:#e2e8f0;max-width:960px;margin:40px auto}.card{background:#1e293b;padding:20px;border-radius:14px;margin:12px}code{color:#93c5fd}</style><h1>CleaningAI OS</h1><div class=card><h2>Mission Control API</h2><p>Health: <code>/health</code> · OpenAPI: <code>/docs</code></p><p>Домены: задачи, решения, CRM, тендеры, HR, finance, outreach, audit и агенты работают через общую БД и orchestrator.</p></div></html>"""
