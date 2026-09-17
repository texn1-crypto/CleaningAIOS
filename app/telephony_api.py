from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime
from collections.abc import Iterator
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import BusinessRecord, Task
from .orchestrator import audit
from .platform import event_bus
from .security import Principal, principal, require_role
from .task_state import record_task_created
from .telephony import CALL_STATUS_RANK, apply_call_callback


router = APIRouter(prefix="/api/telephony", tags=["telephony"])


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SalesCallCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: int = Field(gt=0)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.:-]+$",
    )
    purpose: str = Field(min_length=3, max_length=500)
    script: str = Field(min_length=3, max_length=4_000)
    scheduled_at: Optional[datetime] = None


class CallCallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_attempt_id: str = Field(min_length=8, max_length=128)
    provider_call_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    provider_event_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    status: str
    duration_seconds: int = Field(default=0, ge=0, le=86_400)
    outcome_summary: str = Field(default="", max_length=2_000)


@router.post("/calls", status_code=202)
def request_sales_call(
    payload: SalesCallCreate,
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
) -> dict[str, Any]:
    require_role(actor, "operator")
    lead = db.get(BusinessRecord, payload.lead_id)
    if lead is None or lead.record_type != "lead":
        raise HTTPException(404, "CRM lead not found")
    title = f"Approved sales call · {payload.idempotency_key}"
    existing = db.scalar(
        select(Task).where(Task.agent_type == "sales", Task.title == title)
    )
    if existing is not None:
        return {
            "task_id": existing.id,
            "status": existing.status,
            "duplicate": True,
            "approval_id": (existing.payload or {}).get("approval_id"),
        }
    task = Task(
        title=title,
        agent_type="sales",
        priority="high",
        payload={
            "action": "queue_consented_sales_call",
            "lead_id": payload.lead_id,
            "call_idempotency_key": payload.idempotency_key,
            "purpose": payload.purpose,
            "script": payload.script,
            "scheduled_at": payload.scheduled_at.isoformat() if payload.scheduled_at else None,
            "recording_enabled": False,
            "source": "telephony_api",
        },
        max_attempts=1,
    )
    db.add(task)
    db.flush()
    record_task_created(
        db,
        task,
        actor=actor.subject,
        reason="consented_sales_call_requested",
    )
    audit(
        db,
        actor.subject,
        "telephony.call_requested",
        "task",
        str(task.id),
        {"lead_id": lead.id, "recording_enabled": False},
    )
    db.commit()
    return {"task_id": task.id, "status": task.status, "duplicate": False}


@router.get("/calls")
def list_sales_calls(
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
) -> list[dict[str, Any]]:
    require_role(actor, "viewer")
    rows = db.scalars(
        select(BusinessRecord)
        .where(BusinessRecord.record_type == "sales_call")
        .order_by(BusinessRecord.id.desc())
        .limit(200)
    ).all()
    return [
        {
            "id": row.id,
            "lead_id": int((row.data or {}).get("lead_id") or 0),
            "status": row.status,
            "purpose": str((row.data or {}).get("purpose") or ""),
            "phone_last4": str((row.data or {}).get("phone_last4") or ""),
            "scheduled_at": (row.data or {}).get("scheduled_at"),
            "duration_seconds": int((row.data or {}).get("duration_seconds") or 0),
            "outcome_summary": str((row.data or {}).get("outcome_summary") or ""),
            "recording_enabled": False,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        for row in rows
    ]


@router.post("/callback")
async def telephony_callback(
    request: Request,
    x_telephony_signature: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    secret = settings.telephony_webhook_secret.encode()
    if not secret:
        raise HTTPException(503, "Telephony webhook is not configured")
    raw = await request.body()
    expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    supplied = x_telephony_signature.removeprefix("sha256=")
    if not re.fullmatch(r"[0-9a-f]{64}", supplied) or not hmac.compare_digest(
        supplied, expected
    ):
        raise HTTPException(403, "Invalid telephony signature")
    try:
        decoded: Any = json.loads(raw)
        payload = CallCallback.model_validate(decoded)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(422, "Invalid telephony callback") from exc
    if payload.status not in CALL_STATUS_RANK:
        raise HTTPException(422, "Unsupported telephony callback status")
    try:
        row, duplicate = apply_call_callback(db, **payload.model_dump())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    event_bus.publish(
        db,
        "telephony.call_status_received",
        "sales_call",
        str(row.id),
        {"status": row.status, "duplicate": duplicate},
        idempotency_key=f"telephony-callback:{payload.provider_event_id}",
        actor="telephony_gateway",
        correlation_id=f"sales-call:{row.id}",
    )
    db.commit()
    return {"call_id": row.id, "status": row.status, "duplicate": duplicate}
