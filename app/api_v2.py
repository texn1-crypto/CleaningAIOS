from __future__ import annotations

import base64
import binascii
import hashlib
import os
import smtplib
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import SessionLocal
from .config import settings
from .models import ApprovalRequest, BusinessGoal, BusinessRecord, ContentItem, Decision, DecisionOutcome, ImportJob, ImprovementRequest, InboxMessage, MailTransportState, MessageTemplate, OperatingEntity, OutboundMessage, OutreachConsent, OwnerNotification, SenderMailbox, Suppression, Task, TaskTransition, TenderDocument
from .integrations import collect_tenders, download_tender_document
from .improvements import retry_workspace_handoff
from .management_companies import enrich_management_company, import_management_companies
from .operations import business_graph, create_ceo_actions, entity_view, goal_progress, parse_lead_import, score_tender, simulate_site, site_economics, validate_entity
from .reports import build_ceo_brief
from .orchestrator import audit, dispatch
from .outreach import campaign_approval_payload, persist_campaign_attachments, queue_campaign, upsert_consent, validate_attachments, verified_recipients
from .platform import approval_engine, event_bus
from .schemas import CampaignLaunch, ContentItemCreate, CustomerRequestedCampaignDraft, DecisionOutcomeCreate, DeliveryEventCreate, GoalCreate, GoalProgressUpdate, ImportFile, ImprovementUpdate, InboxMessageCreate, InboxStatusUpdate, MailboxCreate, ManagementCompanyCampaignDraft, ManagementCompanyImport, OperatingEntityCreate, OperatingEntityUpdate, OutreachConsentUpsert, RequestAnalysisCreate, SimulationRequest, StructuredDecisionCreate, TemplateCreate, TenderDocumentCreate, TenderEvaluationRequest
from .security import Principal, principal, require_role
from .chat import redact_sensitive_text
from .approval_service import (
    ApprovalConflict,
    ApprovalError,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalStale,
    decide_approval,
)
from .telegram_control import (
    CallbackTokenError,
    CallbackTokenExpired,
    approval_card,
    audit_subject,
    authorize_identity,
    bind_identity,
    parse_alert_ack_token,
    parse_callback_token,
)
from .notifications import (
    NotificationNotDelivered,
    NotificationNotFound,
    acknowledge_owner_notification,
)
from .tender_intelligence import TERMINAL_TENDER_STATUSES, classify_tender_scope, ensure_participation_review_task, evaluate_tender_viability, merge_registered_document_risks, screening_record_status
from .schemas import TelegramAlertCallback, TelegramApprovalCallback, TelegramIdentityBind, TelegramIdentityRequest, TelegramTaskQuery

router = APIRouter(prefix="/api")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/telegram/control/authorize")
def authorize_telegram_control(
    payload: TelegramIdentityRequest,
    db: Session = Depends(get_db),
    channel: Principal = Depends(principal),
):
    require_role(channel, "owner")
    identity, reason = authorize_identity(
        db,
        user_id=payload.user_id,
        chat_id=payload.chat_id,
        minimum_role=payload.minimum_role,
    )
    db.commit()
    return {
        "authorized": identity is not None,
        "role": identity.role if identity else None,
        "subject": identity.subject if identity else None,
        "reason": reason,
    }


@router.put("/telegram/control/identities")
def bind_telegram_control_identity(
    payload: TelegramIdentityBind,
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
):
    require_role(actor, "owner")
    binding = bind_identity(
        db,
        user_id=payload.user_id,
        chat_id=payload.chat_id,
        role=payload.role,
    )
    audit(
        db,
        actor.subject,
        "telegram.identity_bound",
        "role_binding",
        audit_subject(payload.user_id, payload.chat_id),
        {"role": binding.role},
    )
    db.commit()
    return {"subject": binding.subject, "role": binding.role, "active": binding.active}


def _require_telegram_identity(
    db: Session,
    payload: TelegramIdentityRequest | TelegramApprovalCallback | TelegramAlertCallback | TelegramTaskQuery,
    minimum_role: str,
):
    identity, reason = authorize_identity(
        db,
        user_id=payload.user_id,
        chat_id=payload.chat_id,
        minimum_role=minimum_role,
    )
    if identity is None:
        db.commit()
        raise HTTPException(403, f"Telegram authorization failed: {reason}")
    return identity


def _require_telegram_owner(
    db: Session,
    payload: TelegramIdentityRequest | TelegramApprovalCallback | TelegramAlertCallback,
):
    return _require_telegram_identity(db, payload, "owner")


@router.post("/telegram/control/approvals")
def telegram_approval_cards(
    payload: TelegramIdentityRequest,
    db: Session = Depends(get_db),
    channel: Principal = Depends(principal),
):
    require_role(channel, "owner")
    identity = _require_telegram_owner(db, payload)
    current = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = db.scalars(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.status == "pending",
            or_(ApprovalRequest.expires_at.is_(None), ApprovalRequest.expires_at > current),
        )
        .order_by(ApprovalRequest.id.desc())
        .limit(20)
    ).all()
    cards = [approval_card(row) for row in rows]
    return {
        "role": identity.role,
        "items": [card for card in cards if card["callbacks"]],
    }


@router.post("/telegram/control/approvals/{approval_id}/card")
def telegram_approval_card(
    approval_id: int,
    payload: TelegramIdentityRequest,
    db: Session = Depends(get_db),
    channel: Principal = Depends(principal),
):
    require_role(channel, "owner")
    _require_telegram_owner(db, payload)
    row = db.get(ApprovalRequest, approval_id)
    if row is None:
        raise HTTPException(404, "Approval not found")
    return approval_card(row)


@router.post("/telegram/control/approval-decision")
def telegram_approval_decision(
    payload: TelegramApprovalCallback,
    db: Session = Depends(get_db),
    channel: Principal = Depends(principal),
):
    require_role(channel, "owner")
    identity = _require_telegram_owner(db, payload)
    try:
        parsed = parse_callback_token(payload.callback_token)
    except CallbackTokenExpired as exc:
        audit(
            db,
            identity.subject,
            "telegram.callback_rejected",
            "approval",
            "",
            {"reason": "expired"},
        )
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    except CallbackTokenError as exc:
        audit(
            db,
            identity.subject,
            "telegram.callback_rejected",
            "approval",
            "",
            {"reason": "invalid_signature_or_format"},
        )
        db.commit()
        raise HTTPException(403, str(exc)) from exc
    try:
        result = decide_approval(
            db,
            approval_id=int(parsed["approval_id"]),
            action=str(parsed["action"]),
            note=payload.note,
            actor=Principal(subject=identity.subject, role=identity.role),
            channel="telegram",
            expected_version=int(parsed["decision_version"]),
            idempotent=True,
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


@router.post("/telegram/control/alert-acknowledgement")
def telegram_alert_acknowledgement(
    payload: TelegramAlertCallback,
    db: Session = Depends(get_db),
    channel: Principal = Depends(principal),
):
    require_role(channel, "owner")
    identity = _require_telegram_owner(db, payload)
    try:
        parsed = parse_alert_ack_token(payload.callback_token)
    except CallbackTokenExpired as exc:
        audit(
            db,
            identity.subject,
            "telegram.alert_callback_rejected",
            "owner_notification",
            "",
            {"reason": "expired"},
        )
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    except CallbackTokenError as exc:
        audit(
            db,
            identity.subject,
            "telegram.alert_callback_rejected",
            "owner_notification",
            "",
            {"reason": "invalid_signature_or_format"},
        )
        db.commit()
        raise HTTPException(403, str(exc)) from exc
    try:
        result = acknowledge_owner_notification(
            db,
            notification_id=int(parsed["notification_id"]),
            actor=identity.subject,
        )
    except NotificationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except NotificationNotDelivered as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return result


@router.post("/telegram/control/tasks/query")
def telegram_task_query(
    payload: TelegramTaskQuery,
    db: Session = Depends(get_db),
    channel: Principal = Depends(principal),
):
    require_role(channel, "owner")
    identity = _require_telegram_identity(db, payload, "viewer")
    offset = (payload.page - 1) * payload.page_size
    if payload.view == "critical_events":
        criteria = OwnerNotification.severity.in_(["high", "critical"])
        total = int(
            db.scalar(
                select(func.count()).select_from(OwnerNotification).where(criteria)
            )
            or 0
        )
        rows = db.scalars(
            select(OwnerNotification)
            .where(criteria)
            .order_by(OwnerNotification.id.desc())
            .offset(offset)
            .limit(payload.page_size)
        ).all()
        items = [
            {
                "item_type": "critical_event",
                "id": row.id,
                "title": row.subject,
                "status": row.status,
                "priority": row.severity,
                "assigned_to": "owner",
                "due_at": None,
                "correlation_id": row.correlation_id,
                "source": {
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                },
                "acknowledged_at": row.acknowledged_at,
            }
            for row in rows
        ]
    else:
        criteria = []
        if payload.view == "mine":
            criteria.append(Task.assigned_to == identity.subject)
        elif payload.view == "overdue":
            criteria.extend(
                [
                    Task.due_at.is_not(None),
                    Task.due_at < datetime.now(timezone.utc).replace(tzinfo=None),
                    Task.status.in_(["open", "queued", "running", "blocked"]),
                ]
            )
        elif payload.view == "critical":
            criteria.append(
                or_(Task.priority == "critical", Task.status.in_(["failed", "blocked"]))
            )
        count_query = select(func.count()).select_from(Task)
        query = select(Task)
        if criteria:
            count_query = count_query.where(*criteria)
            query = query.where(*criteria)
        total = int(db.scalar(count_query) or 0)
        rows = db.scalars(
            query.order_by(Task.due_at.asc().nulls_last(), Task.id.desc())
            .offset(offset)
            .limit(payload.page_size)
        ).all()
        task_ids = [row.id for row in rows]
        transitions = (
            db.scalars(
                select(TaskTransition)
                .where(TaskTransition.task_id.in_(task_ids))
                .order_by(TaskTransition.task_id, TaskTransition.id.desc())
            ).all()
            if task_ids
            else []
        )
        latest_transition = {}
        for transition in transitions:
            latest_transition.setdefault(transition.task_id, transition)
        items = []
        for row in rows:
            transition = latest_transition.get(row.id)
            items.append(
                {
                    "item_type": "task",
                    "id": row.id,
                    "title": row.title,
                    "status": row.status,
                    "priority": row.priority,
                    "agent_type": row.agent_type,
                    "assigned_to": row.assigned_to,
                    "due_at": row.due_at,
                    "correlation_id": transition.correlation_id if transition else "",
                    "last_transition": {
                        "id": transition.id,
                        "from_status": transition.from_status,
                        "to_status": transition.to_status,
                        "reason": transition.reason,
                    }
                    if transition
                    else None,
                }
            )
    total_pages = max(1, (total + payload.page_size - 1) // payload.page_size)
    return {
        "view": payload.view,
        "page": payload.page,
        "page_size": payload.page_size,
        "total": total,
        "total_pages": total_pages,
        "has_previous": payload.page > 1,
        "has_next": payload.page < total_pages,
        "items": items,
    }


def _previous_request_text(db: Session, *, source_channel: str, source_user: str, current_message: str) -> str:
    rows = db.scalars(
        select(Task)
        .where(Task.agent_type == "request_analyst")
        .order_by(Task.id.desc())
        .limit(100)
    ).all()
    normalized_current = " ".join(current_message.lower().split())
    for row in rows:
        stored = row.payload if isinstance(row.payload, dict) else {}
        if stored.get("source_channel") != source_channel or str(stored.get("source_user")) != source_user:
            continue
        candidate = redact_sensitive_text(str(stored.get("message") or "")).strip()[:4000]
        if candidate and " ".join(candidate.lower().split()) != normalized_current:
            return candidate
    return ""


def improvement_view(row: ImprovementRequest) -> dict:
    return {
        "id": row.id,
        "source_channel": row.source_channel,
        "source_user": row.source_user,
        "request_text": row.request_text,
        "intent": row.intent,
        "capability_score": row.capability_score,
        "classification": row.classification,
        "reason": row.reason,
        "missing_capabilities": row.missing_capabilities,
        "suggested_function": row.suggested_function,
        "codex_prompt": row.codex_prompt,
        "acceptance_criteria": row.acceptance_criteria,
        "test_plan": row.test_plan,
        "status": row.status,
        "occurrence_count": row.occurrence_count,
        "handoff_status": row.handoff_status,
        "workspace_conversation_url": row.workspace_conversation_url,
        "workspace_run_id": row.workspace_run_id,
        "implementation_summary": row.implementation_summary,
        "test_evidence": row.test_evidence,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.post("/request-analysis")
def analyze_request(payload: RequestAnalysisCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    safe_message = redact_sensitive_text(payload.message.strip())[:4000]
    resolved_intent = dict(payload.intent)
    resolved_payload = dict(resolved_intent.get("payload") or {})
    context_found = None
    if resolved_payload.get("action") == "review_previous_text":
        if not resolved_payload.get("referenced_text"):
            resolved_payload["referenced_text"] = _previous_request_text(
                db,
                source_channel=payload.source_channel,
                source_user=payload.source_user,
                current_message=safe_message,
            )
        context_found = bool(resolved_payload.get("referenced_text"))
        resolved_intent["payload"] = resolved_payload
    task_payload = payload.model_dump()
    task_payload["message"] = safe_message
    task_payload["intent"] = resolved_intent
    task = Task(
        title=f"Analyze {payload.source_channel} request",
        agent_type="request_analyst",
        priority="high",
        payload=task_payload,
    )
    db.add(task); db.flush()
    result = dispatch(db, task)
    audit(db, actor.subject, "request.analyzed", "task", str(task.id), {"classification": result.get("classification"), "improvement_id": result.get("improvement_id")})
    db.commit()
    return {
        "analysis_task_id": task.id,
        **result,
        "resolved_intent": resolved_intent,
        "context_found": context_found,
    }


@router.get("/improvements")
def improvements(status: Optional[str] = None, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    query = select(ImprovementRequest).order_by(ImprovementRequest.id)
    if status:
        query = query.where(ImprovementRequest.status == status)
    return [improvement_view(row) for row in db.scalars(query.limit(500)).all()]


@router.patch("/improvements/{improvement_id}")
def update_improvement(improvement_id: int, payload: ImprovementUpdate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    row = db.get(ImprovementRequest, improvement_id)
    if not row:
        raise HTTPException(404, "Improvement request not found")
    row.status = payload.status
    if payload.implementation_summary:
        row.implementation_summary = payload.implementation_summary
    if payload.test_evidence:
        row.test_evidence = payload.test_evidence
    audit(db, actor.subject, "improvement.updated", "improvement_request", str(row.id), {"status": row.status})
    db.commit(); db.refresh(row)
    return improvement_view(row)


@router.post("/improvements/{improvement_id}/handoff")
def handoff_improvement(improvement_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    row = db.get(ImprovementRequest, improvement_id)
    if not row:
        raise HTTPException(404, "Improvement request not found")
    result = retry_workspace_handoff(row)
    audit(db, actor.subject, "improvement.handoff", "improvement_request", str(row.id), {"status": result["status"]})
    db.commit(); db.refresh(row)
    return {**improvement_view(row), "handoff": result}


@router.get("/entities")
def entities(entity_type: Optional[str] = None, parent_id: Optional[int] = None, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    query = select(OperatingEntity).order_by(OperatingEntity.id.desc())
    if entity_type:
        query = query.where(OperatingEntity.entity_type == entity_type)
    if parent_id is not None:
        query = query.where(OperatingEntity.parent_id == parent_id)
    return [entity_view(x) for x in db.scalars(query).all()]


@router.post("/entities", status_code=201)
def create_entity(payload: OperatingEntityCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    validate_entity(db, payload.entity_type, payload.parent_id, payload.data)
    row = OperatingEntity(**payload.model_dump())
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, "Duplicate external entity")
    event_bus.publish(db, f"{row.entity_type}.created", row.entity_type, str(row.id), {"name": row.name, "status": row.status, "parent_id": row.parent_id})
    audit(db, actor.subject, f"{row.entity_type}.created", row.entity_type, str(row.id))
    db.commit(); db.refresh(row)
    return entity_view(row)


@router.patch("/entities/{entity_id}")
def update_entity(entity_id: int, payload: OperatingEntityUpdate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(OperatingEntity, entity_id)
    if not row:
        raise HTTPException(404, "Entity not found")
    changes = payload.model_dump(exclude_unset=True)
    data = {**row.data, **(changes.get("data") or {})}
    parent_id = changes.get("parent_id", row.parent_id)
    validate_entity(db, row.entity_type, parent_id, data)
    old_status = row.status
    for field, value in changes.items():
        setattr(row, field, data if field == "data" else value)
    event_type = f"{row.entity_type}.status_changed" if row.status != old_status else f"{row.entity_type}.updated"
    event_bus.publish(db, event_type, row.entity_type, str(row.id), {"old_status": old_status, "status": row.status, "changes": sorted(changes)})
    audit(db, actor.subject, event_type, row.entity_type, str(row.id))
    db.commit(); db.refresh(row)
    return entity_view(row)


@router.get("/company/graph")
def company_graph(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    return business_graph(db)


@router.get("/finance/site-economics")
def economics(site_id: Optional[int] = None, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    return site_economics(db, site_id)


@router.post("/simulations")
def simulate(payload: SimulationRequest, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    return simulate_site(db, **payload.model_dump())


@router.get("/goals")
def goals(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    return [goal_progress(x) for x in db.scalars(select(BusinessGoal).order_by(BusinessGoal.id.desc())).all()]


@router.post("/goals", status_code=201)
def create_goal(payload: GoalCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    row = BusinessGoal(**payload.model_dump())
    db.add(row); db.flush()
    event_bus.publish(db, "goal.created", "goal", str(row.id), goal_progress(row))
    audit(db, actor.subject, "goal.created", "goal", str(row.id))
    db.commit(); db.refresh(row)
    return goal_progress(row)


@router.patch("/goals/{goal_id}/progress")
def update_goal(goal_id: int, payload: GoalProgressUpdate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(BusinessGoal, goal_id)
    if not row:
        raise HTTPException(404, "Goal not found")
    row.current = payload.current
    reached = row.current >= row.target if row.target >= row.baseline else row.current <= row.target
    if reached:
        row.status = "completed"
    result = goal_progress(row)
    event_bus.publish(db, "goal.progress_updated", "goal", str(row.id), {**result, "note": payload.note})
    db.commit()
    return result


@router.post("/ceo/review")
def ceo_review(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    tasks = create_ceo_actions(db)
    event_bus.publish(db, "ceo.review_completed", "company", "1", {"tasks_created": [x.id for x in tasks]})
    audit(db, actor.subject, "ceo.review_completed", "company", "1", {"tasks_created": len(tasks)})
    db.commit()
    return {"tasks_created": [{"id": x.id, "title": x.title, "agent_type": x.agent_type} for x in tasks]}


@router.get("/ceo/brief")
def ceo_brief(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    return build_ceo_brief(db)


@router.post("/structured-decisions", status_code=201)
def create_structured_decision(payload: StructuredDecisionCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    data = payload.model_dump()
    row = Decision(title=payload.title, rationale=payload.problem, kind=payload.approval_kind or "operational", requested_by=actor.subject, payload=data)
    db.add(row); db.flush()
    approval = approval_engine.request(db, payload.approval_kind or "business_decision", "decision", str(row.id), actor.subject, data, payload.problem) if payload.requires_approval else None
    event_bus.publish(db, "decision.proposed", "decision", str(row.id), {"risk": payload.risk, "confidence": payload.confidence, "requires_approval": payload.requires_approval})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status, "approval_id": approval.id if approval else None, **data}


@router.put("/decisions/{decision_id}/outcome")
def measure_decision(decision_id: int, payload: DecisionOutcomeCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    if not db.get(Decision, decision_id):
        raise HTTPException(404, "Decision not found")
    row = db.scalar(select(DecisionOutcome).where(DecisionOutcome.decision_id == decision_id))
    if row:
        for field, value in payload.model_dump().items(): setattr(row, field, value)
        row.measured_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        row = DecisionOutcome(decision_id=decision_id, **payload.model_dump()); db.add(row)
    db.flush()
    event_bus.publish(db, "decision.outcome_measured", "decision", str(decision_id), payload.model_dump())
    db.commit()
    return {"decision_id": decision_id, **payload.model_dump()}


@router.post("/tenders/{record_id}/documents", status_code=201)
def add_tender_document(record_id: int, payload: TenderDocumentCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    tender = db.get(BusinessRecord, record_id)
    if not tender or tender.record_type != "tender":
        raise HTTPException(404, "Tender not found")
    row = TenderDocument(record_id=record_id, **payload.model_dump())
    if row.analysis:
        row.status = "analyzed"; row.analyzed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(row)
    try: db.flush()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Tender document already registered")
    event_bus.publish(db, "tender.document_registered", "tender", str(record_id), {"document_id": row.id, "status": row.status})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status, "analysis": row.analysis}


@router.post("/tenders/{record_id}/score")
def calculate_tender_score(record_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    tender = db.get(BusinessRecord, record_id)
    if not tender or tender.record_type != "tender": raise HTTPException(404, "Tender not found")
    result = score_tender(tender.data)
    tender.score = result["score"]; tender.data = {**tender.data, "score_breakdown": result["breakdown"], "recommendation": result["recommendation"]}
    event_bus.publish(db, "tender.scored", "tender", str(tender.id), result)
    db.commit()
    return result


@router.post("/tenders/{record_id}/evaluate")
def evaluate_tender(
    record_id: int,
    payload: TenderEvaluationRequest,
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
):
    require_role(actor, "operator")
    tender = db.get(BusinessRecord, record_id)
    if not tender or tender.record_type != "tender":
        raise HTTPException(404, "Tender not found")
    if tender.status in TERMINAL_TENDER_STATUSES:
        raise HTTPException(409, f"Tender is in terminal status: {tender.status}")

    values = payload.model_dump(
        exclude={"queue_participation_review"},
        exclude_none=True,
        exclude_unset=True,
    )
    if "legal_risk_flags" in values:
        values["source_legal_risk_flags"] = list(values["legal_risk_flags"])
    tender.data = {**(tender.data or {}), **values, "title": tender.title, "external_id": tender.external_id or ""}
    if tender.deadline_at:
        tender.data = {**tender.data, "deadline_at": tender.deadline_at.isoformat()}
    tender.data = {**tender.data, "scope_assessment": classify_tender_scope(tender.title, tender.data)}
    tender.data = merge_registered_document_risks(db, tender, tender.data)
    evaluation = evaluate_tender_viability(tender.data)
    tender.score = evaluation.get("score")
    tender.status = screening_record_status(evaluation["status"])
    tender.data = {
        **tender.data,
        "viability_evaluation": evaluation,
        "score_breakdown": evaluation.get("score_breakdown", {}),
        "recommendation": evaluation["decision"],
    }

    participation_task = None
    if evaluation.get("participation_review_available") and payload.queue_participation_review:
        participation_task = ensure_participation_review_task(
            db,
            tender,
            evaluation,
            actor=actor.subject,
        )

    event_bus.publish(
        db,
        "tender.viability_evaluated",
        "tender",
        str(tender.id),
        {
            "status": evaluation["status"],
            "decision": evaluation["decision"],
            "score": evaluation.get("score"),
            "hard_stops": evaluation.get("hard_stops", []),
            "participation_review_task_id": participation_task.id if participation_task else None,
        },
        idempotency_key=f"tender:{tender.id}:evaluation:{evaluation['fingerprint']}",
        actor=actor.subject,
    )
    audit(
        db,
        actor.subject,
        "tender.viability_evaluated",
        "tender",
        str(tender.id),
        {"status": evaluation["status"], "fingerprint": evaluation["fingerprint"]},
    )
    db.commit()
    return {
        **evaluation,
        "participation_review_task_id": participation_task.id if participation_task else None,
        "participation_review_task_status": participation_task.status if participation_task else None,
    }


@router.post("/tender-sources/collect")
def run_tender_collection(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    result = collect_tenders(db)
    audit(db, actor.subject, "tenders.collected", "tender_feed", "", result)
    db.commit()
    return result


@router.post("/tender-documents/{document_id}/download")
def download_document(document_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    document = db.get(TenderDocument, document_id)
    if not document: raise HTTPException(404, "Tender document not found")
    result = download_tender_document(db, document)
    audit(db, actor.subject, "tender.document_downloaded", "tender_document", str(document.id), {"checksum": document.checksum})
    db.commit()
    return result


@router.post("/outreach/mailboxes", status_code=201)
def create_mailbox(payload: MailboxCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "owner")
    row = SenderMailbox(**payload.model_dump())
    db.add(row)
    try: db.commit()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Mailbox already exists")
    db.refresh(row)
    return {"id": row.id, "name": row.name, "address": row.address, "active": row.active, "secret_configured": bool(row.secret_ref and os.environ.get(row.secret_ref)), "inbound_enabled": row.inbound_enabled, "inbound_secret_configured": bool(row.imap_secret_ref and os.environ.get(row.imap_secret_ref))}


@router.get("/outreach/mailboxes")
def mailboxes(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    rows = db.scalars(select(SenderMailbox).order_by(SenderMailbox.id)).all()
    states = {state.mailbox_key: state for state in db.scalars(select(MailTransportState)).all()}
    return [{"id": x.id, "name": x.name, "address": x.address, "active": x.active, "per_minute": x.per_minute, "per_day": x.per_day, "sent_today": x.sent_today, "last_sent_at": x.last_sent_at, "secret_configured": bool(x.secret_ref and os.environ.get(x.secret_ref)), "inbound_enabled": x.inbound_enabled, "inbound_secret_configured": bool(x.imap_secret_ref and os.environ.get(x.imap_secret_ref)), "delivery_status": states[str(x.id)].status if str(x.id) in states else "ready", "retry_after": states[str(x.id)].retry_after if str(x.id) in states else None} for x in rows]


@router.post("/outreach/mailboxes/{mailbox_key}/resume")
def resume_mailbox_transport(
    mailbox_key: str,
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
):
    """Authenticate without sending, then recover a quarantined SMTP queue."""
    require_role(actor, "owner")
    mailbox_id: int | None
    if mailbox_key == "default":
        mailbox_id = None
        smtp_host = settings.smtp_host
        smtp_port = settings.smtp_port
        smtp_username = settings.smtp_username
        smtp_password = settings.smtp_password
        configured = bool(smtp_host and smtp_username and smtp_password and settings.smtp_from_email)
    elif mailbox_key.isdigit() and str(int(mailbox_key)) == mailbox_key:
        mailbox_id = int(mailbox_key)
        mailbox = db.get(SenderMailbox, mailbox_id)
        if mailbox is None:
            raise HTTPException(404, "Mailbox not found")
        smtp_host = mailbox.smtp_host
        smtp_port = mailbox.smtp_port
        smtp_username = mailbox.username or mailbox.address
        smtp_password = os.environ.get(mailbox.secret_ref, "") if mailbox.secret_ref else ""
        configured = bool(mailbox.active and smtp_host and smtp_username and smtp_password)
    else:
        raise HTTPException(404, "Mailbox not found")
    if not configured:
        raise HTTPException(409, "SMTP credentials must be configured before recovery")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    state = db.get(MailTransportState, mailbox_key)
    previous_status = state.status if state else "ready"

    try:
        smtp_factory = smtplib.SMTP_SSL if smtp_port == 465 else smtplib.SMTP
        with smtp_factory(smtp_host, smtp_port, timeout=20) as smtp:
            if smtp_port != 465:
                smtp.starttls()
            smtp.login(smtp_username, smtp_password)
    except smtplib.SMTPAuthenticationError:
        status = "credentials_required"
        reason = "SMTP authentication failed; verify the mailbox application password"
    except smtplib.SMTPResponseException:
        status = "provider_blocked"
        reason = "SMTP provider rejected the authentication preflight"
    except (OSError, TimeoutError, smtplib.SMTPException):
        status = "unavailable"
        reason = "SMTP authentication preflight is temporarily unavailable"
    else:
        status = "ready"
        reason = ""

    mailbox_scope = (
        OutboundMessage.mailbox_id == mailbox_id
        if mailbox_id is not None
        else OutboundMessage.mailbox_id.is_(None)
    )
    if status != "ready":
        if state is None:
            state = MailTransportState(mailbox_key=mailbox_key)
            db.add(state)
        state.status = status
        state.reason = reason
        state.consecutive_failures = (state.consecutive_failures or 0) + 1
        state.blocked_at = now
        state.retry_after = None
        state.updated_at = now
        paused = db.execute(
            update(OutboundMessage)
            .where(mailbox_scope, OutboundMessage.status.in_(["queued", "retry"]))
            .values(status="waiting_configuration", error=reason)
        )
        audit(
            db,
            actor.subject,
            "outreach.smtp_preflight_failed",
            "sender_mailbox",
            mailbox_key,
            {
                "status": status,
                "previous_status": previous_status,
                "paused": int(paused.rowcount or 0),
            },
        )
        db.commit()
        return {
            "mailbox_key": mailbox_key,
            "status": status,
            "authenticated": False,
            "previous_status": previous_status,
            "requeued": 0,
            "reason": reason,
        }

    if state is None:
        state = MailTransportState(mailbox_key=mailbox_key)
        db.add(state)
    state.status = "ready"
    state.reason = ""
    state.consecutive_failures = 0
    state.blocked_at = None
    state.retry_after = None
    state.updated_at = now
    result = db.execute(
        update(OutboundMessage)
        .where(mailbox_scope, OutboundMessage.status == "waiting_configuration")
        .values(status="queued", scheduled_at=now, error="")
    )
    requeued = int(result.rowcount or 0)
    audit(
        db,
        actor.subject,
        "outreach.smtp_transport_resumed",
        "sender_mailbox",
        mailbox_key,
        {"previous_status": previous_status, "requeued": requeued},
    )
    db.commit()
    return {
        "mailbox_key": mailbox_key,
        "status": "ready",
        "authenticated": True,
        "previous_status": previous_status,
        "requeued": requeued,
    }


@router.put("/outreach/consents")
def record_outreach_consent(payload: OutreachConsentUpsert, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "owner")
    if payload.record_id:
        record = db.get(BusinessRecord, payload.record_id)
        if not record:
            raise HTTPException(404, "Business record not found")
    row = upsert_consent(
        db,
        address=str(payload.address),
        record_id=payload.record_id,
        status=payload.status,
        purpose=payload.purpose,
        source_url=payload.source_url,
        evidence=payload.evidence,
        actor=actor.subject,
    )
    event_bus.publish(
        db,
        f"outreach.consent_{row.status}",
        "email",
        row.address,
        {"record_id": row.record_id, "purpose": row.purpose, "evidence_hash": row.evidence_hash},
        actor=actor.subject,
    )
    audit(db, actor.subject, f"outreach.consent_{row.status}", "email", row.address, {"record_id": row.record_id, "evidence_hash": row.evidence_hash})
    db.commit()
    return {"address": row.address, "status": row.status, "record_id": row.record_id, "purpose": row.purpose, "evidence_hash": row.evidence_hash}


@router.get("/outreach/consents")
def outreach_consents(status: Optional[str] = None, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    query = select(OutreachConsent).order_by(OutreachConsent.address)
    if status:
        query = query.where(OutreachConsent.status == status)
    rows = db.scalars(query.limit(5000)).all()
    return [{"address": row.address, "status": row.status, "record_id": row.record_id, "purpose": row.purpose, "source_url": row.source_url, "evidence_hash": row.evidence_hash, "verified_at": row.verified_at} for row in rows]


@router.post("/outreach/templates", status_code=201)
def create_template(payload: TemplateCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    row = MessageTemplate(**payload.model_dump()); db.add(row)
    try: db.commit()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Template already exists")
    db.refresh(row)
    return {"id": row.id, "name": row.name, "subject": row.subject, "body": row.body, "variables": row.variables}


@router.get("/outreach/templates")
def templates(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    rows = db.scalars(select(MessageTemplate).where(MessageTemplate.active.is_(True)).order_by(MessageTemplate.name)).all()
    return [{"id": x.id, "name": x.name, "subject": x.subject, "body": x.body, "variables": x.variables} for x in rows]


@router.get("/outreach/messages")
def delivery_log(status: Optional[str] = None, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    query = select(OutboundMessage).order_by(OutboundMessage.id.desc())
    if status: query = query.where(OutboundMessage.status == status)
    rows = db.scalars(query.limit(1000)).all()
    return [{"id": x.id, "campaign_key": x.campaign_key, "recipient": x.recipient, "mailbox_id": x.mailbox_id, "status": x.status, "scheduled_at": x.scheduled_at, "sent_at": x.sent_at, "error": x.error, "attachment_count": len(x.attachments or [])} for x in rows]


@router.get("/outreach/summary")
def outreach_summary(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    """Return an owner-safe operational view without exposing recipient addresses."""
    require_role(actor, "manager")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    mailboxes = db.scalars(select(SenderMailbox).order_by(SenderMailbox.id)).all()
    transport_states = db.scalars(select(MailTransportState).order_by(MailTransportState.mailbox_key)).all()
    state_by_key = {row.mailbox_key: row for row in transport_states}

    def transport_available(key: str) -> bool:
        state = state_by_key.get(key)
        if state is None or state.status == "ready":
            return True
        return bool(
            state.status == "rate_limited"
            and state.retry_after
            and state.retry_after <= now
        )

    ready_mailboxes = sum(
        1
        for row in mailboxes
        if row.active
        and row.smtp_host
        and (row.username or row.address)
        and row.secret_ref
        and bool(os.environ.get(row.secret_ref))
        and transport_available(str(row.id))
    )
    default_sender_ready = bool(
        settings.smtp_host
        and settings.smtp_username
        and settings.smtp_password
        and settings.smtp_from_email
        and transport_available("default")
    )
    unavailable_transports = [
        row
        for row in transport_states
        if not transport_available(row.mailbox_key)
    ]
    inbound_enabled = [row for row in mailboxes if row.active and row.inbound_enabled]
    inbound_receiving_ready = sum(
        1
        for row in inbound_enabled
        if row.imap_host
        and (row.imap_username or row.username or row.address)
        and row.imap_secret_ref
        and bool(os.environ.get(row.imap_secret_ref))
    )
    inbound_forwarding_ready = sum(
        1
        for row in inbound_enabled
        if row.imap_host
        and (row.imap_username or row.username or row.address)
        and row.imap_secret_ref
        and bool(os.environ.get(row.imap_secret_ref))
        and (
            default_sender_ready
            or (
                row.smtp_host
                and (row.username or row.address)
                and row.secret_ref
                and bool(os.environ.get(row.secret_ref))
            )
        )
        and settings.owner_notification_email
        and settings.owner_notification_email.strip().lower() != row.address.lower()
    )
    status_counts = {
        str(status): int(count)
        for status, count in db.execute(
            select(OutboundMessage.status, func.count(OutboundMessage.id)).group_by(OutboundMessage.status)
        ).all()
    }
    recent_campaign_keys = db.scalars(
        select(OutboundMessage.campaign_key)
        .group_by(OutboundMessage.campaign_key)
        .order_by(func.max(OutboundMessage.id).desc())
        .limit(5)
    ).all()
    campaigns: list[dict] = []
    for campaign_key in recent_campaign_keys:
        latest = db.scalar(
            select(OutboundMessage)
            .where(OutboundMessage.campaign_key == campaign_key)
            .order_by(OutboundMessage.id.desc())
            .limit(1)
        )
        campaign_statuses = {
            str(status): int(count)
            for status, count in db.execute(
                select(OutboundMessage.status, func.count(OutboundMessage.id))
                .where(OutboundMessage.campaign_key == campaign_key)
                .group_by(OutboundMessage.status)
            ).all()
        }
        campaigns.append({
            "campaign_key": campaign_key,
            "subject": latest.subject if latest else "",
            "message_count": sum(campaign_statuses.values()),
            "statuses": campaign_statuses,
            "last_scheduled_at": latest.scheduled_at if latest else None,
        })
    verified_consents = db.scalar(
        select(func.count(OutreachConsent.address)).where(
            OutreachConsent.status == "verified",
            OutreachConsent.purpose == "commercial_outreach",
        )
    ) or 0
    revoked_consents = db.scalar(
        select(func.count(OutreachConsent.address)).where(OutreachConsent.status == "revoked")
    ) or 0
    pending_approvals = db.scalar(
        select(func.count(ApprovalRequest.id)).where(
            ApprovalRequest.action_kind == "bulk_outreach",
            ApprovalRequest.status == "pending",
        )
    ) or 0
    return {
        "delivery_ready": bool(default_sender_ready or ready_mailboxes),
        "mailboxes": {
            "total": len(mailboxes),
            "active": sum(1 for row in mailboxes if row.active),
            "ready": ready_mailboxes,
            "default_sender_ready": default_sender_ready,
            "quarantined": len(unavailable_transports),
        },
        "transports": {
            "unavailable": len(unavailable_transports),
            "items": [
                {
                    "mailbox_key": row.mailbox_key,
                    "status": row.status,
                    "reason": row.reason,
                    "retry_after": row.retry_after,
                }
                for row in unavailable_transports
            ],
        },
        "inbound": {
            "enabled": len(inbound_enabled),
            "receiving_ready": inbound_receiving_ready,
            "forwarding_ready": inbound_forwarding_ready,
            "owner_destination_ready": bool(settings.owner_notification_email),
        },
        "consents": {"verified": int(verified_consents), "revoked": int(revoked_consents)},
        "suppressed": int(db.scalar(select(func.count(Suppression.address))) or 0),
        "messages": {
            "total": int(sum(status_counts.values())),
            "statuses": status_counts,
        },
        "campaigns": {
            "total": int(db.scalar(select(func.count(func.distinct(OutboundMessage.campaign_key)))) or 0),
            "recent": campaigns,
        },
        "pending_approvals": int(pending_approvals),
        "limits": {
            "per_minute": settings.outreach_per_minute,
            "per_day": settings.outreach_per_day,
        },
        "safety": {
            "verified_consent_required": True,
            "owner_approval_required": True,
            "suppression_enforced": True,
            "transport_circuit_breaker": True,
        },
    }


@router.post("/outreach/delivery-events", status_code=201)
def delivery_event(payload: DeliveryEventCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    recipient = str(payload.recipient).lower()
    message = db.get(OutboundMessage, payload.message_id) if payload.message_id else db.scalar(select(OutboundMessage).where(OutboundMessage.recipient == recipient).order_by(OutboundMessage.id.desc()))
    if message and message.recipient != recipient: raise HTTPException(409, "Recipient does not match message")
    if message:
        message.status = {"delivered": "delivered", "bounce": "bounced", "complaint": "complained", "unsubscribe": "unsubscribed"}[payload.event_type]
        if payload.reason: message.error = payload.reason
    if payload.event_type in {"bounce", "complaint", "unsubscribe"}:
        db.merge(Suppression(address=recipient, reason=payload.event_type))
    event_bus.publish(db, f"outreach.{payload.event_type}", "outbound_message", str(message.id if message else payload.message_id or ""), {"recipient": recipient, "reason": payload.reason, "data": payload.data})
    audit(db, actor.subject, f"outreach.{payload.event_type}", "outbound_message", str(message.id if message else ""), {"recipient": recipient})
    db.commit()
    return {"accepted": True, "message_id": message.id if message else None, "recipient": recipient, "suppressed": payload.event_type in {"bounce", "complaint", "unsubscribe"}}


@router.post("/outreach/campaigns/launch")
def launch_campaign(payload: CampaignLaunch, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    recipients = sorted(set(str(address).lower() for address in payload.recipients))
    _, without_consent = verified_recipients(db, recipients)
    if without_consent:
        raise HTTPException(422, {"message": "Verified commercial-outreach consent is required", "addresses_without_consent": without_consent[:100], "count": len(without_consent)})
    try:
        approval_payload = campaign_approval_payload(
            recipients=recipients,
            subject=payload.subject,
            body=payload.body,
            mailbox_id=payload.mailbox_id,
            template_id=payload.template_id,
            scheduled_at=payload.scheduled_at,
            attachments=payload.attachments,
            auto_balance_mailboxes=payload.auto_balance_mailboxes,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not approval_engine.authorized(db, "bulk_outreach", payload.approval_id, "campaign", payload.campaign_key, approval_payload):
        request = approval_engine.request(db, "bulk_outreach", "campaign", payload.campaign_key, actor.subject, approval_payload, "Bulk outreach requires owner approval")
        event_bus.publish(db, "approval.requested", "campaign", payload.campaign_key, {"approval_id": request.id, "recipient_count": len(payload.recipients)}, idempotency_key=f"campaign:{payload.campaign_key}:approval:{request.id}")
        db.commit()
        return {"status": "waiting_approval", "approval_id": request.id, "recipient_count": len(payload.recipients)}
    try:
        result = queue_campaign(
            db,
            campaign_key=payload.campaign_key,
            recipients=recipients,
            subject=payload.subject,
            body=payload.body,
            mailbox_id=payload.mailbox_id,
            template_id=payload.template_id,
            scheduled_at=payload.scheduled_at,
            attachments=payload.attachments,
            auto_balance_mailboxes=payload.auto_balance_mailboxes,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    event_bus.publish(db, "campaign.queued", "campaign", payload.campaign_key, result, idempotency_key=f"campaign:{payload.campaign_key}:queued")
    audit(db, actor.subject, "campaign.queued", "campaign", payload.campaign_key, result)
    db.commit()
    return result


@router.post("/outreach/campaigns/customer-requested/draft", status_code=201)
def draft_customer_requested_campaign(
    payload: CustomerRequestedCampaignDraft,
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
):
    """Record owner-attested opt-ins and create an approval-bound campaign task.

    The endpoint never sends mail. The deterministic sales agent can only queue the
    exact recipient/content payload after the owner approves the protected task.
    """
    require_role(actor, "owner")
    recipients = sorted(set(str(address).lower() for address in payload.recipients))
    batch_size = 100
    recipient_batches = [recipients[index:index + batch_size] for index in range(0, len(recipients), batch_size)]
    source_filename = os.path.basename(payload.source_filename or "")[:255] or None
    try:
        checked_attachments = validate_attachments(payload.attachments)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    attachment_digest = hashlib.sha256(
        "\n".join(item["sha256"] for item in checked_attachments).encode()
    ).hexdigest()
    campaign_identity = "\n".join(recipients) + "\n" + payload.subject + "\n" + payload.body
    if checked_attachments:
        campaign_identity += "\n" + attachment_digest
    campaign_digest = hashlib.sha256(campaign_identity.encode()).hexdigest()
    campaign_key = f"customer-requested-{campaign_digest[:32]}"
    try:
        attachments = persist_campaign_attachments(campaign_key, payload.attachments)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    candidates = db.scalars(
        select(Task)
        .where(Task.agent_type == "sales")
        .order_by(Task.id.desc())
        .limit(1000)
    ).all()
    existing = next(
        (row for row in candidates if (row.payload or {}).get("campaign_key") == campaign_key),
        None,
    )
    if existing:
        stored = existing.result or {}
        return {
            "status": existing.status,
            "task_id": existing.id,
            "approval_id": stored.get("approval_id") or (existing.payload or {}).get("approval_id"),
            "recipient_count": len(recipients),
            "batch_size": int((existing.payload or {}).get("batch_size") or batch_size),
            "batch_count": int((existing.payload or {}).get("batch_count") or len(recipient_batches)),
            "attachment_count": len((existing.payload or {}).get("attachments") or []),
            "campaign_key": campaign_key,
            "idempotent_replay": True,
        }

    for address in recipients:
        consent = upsert_consent(
            db,
            address=address,
            record_id=None,
            status="verified",
            purpose="commercial_outreach",
            source_url="telegram://owner-consent-attestation",
            evidence=payload.consent_evidence,
            actor=actor.subject,
        )
        audit(
            db,
            actor.subject,
            "outreach.consent_verified",
            "email",
            consent.address,
            {"evidence_hash": consent.evidence_hash, "source": "telegram_owner_attestation"},
        )
        event_bus.publish(
            db,
            "outreach.consent_verified",
            "email",
            consent.address,
            {"evidence_hash": consent.evidence_hash, "purpose": consent.purpose},
            actor=actor.subject,
            idempotency_key=f"consent:{consent.address}:{consent.evidence_hash}",
        )

    task = Task(
        title=f"Рассылка клиентам по их запросу: {payload.subject}"[:255],
        agent_type="sales",
        priority="high",
        payload={
            "action": "execute_bulk_outreach_campaign",
            "action_kind": "bulk_outreach",
            "source": "telegram_mailing_wizard",
            "original_message": "Подготовить подтверждённую владельцем клиентскую email-рассылку",
            "campaign_key": campaign_key,
            "recipients": recipients,
            "recipient_batches": recipient_batches,
            "batch_size": batch_size,
            "batch_count": len(recipient_batches),
            "recipient_digest": hashlib.sha256("\n".join(recipients).encode()).hexdigest(),
            "subject": payload.subject,
            "body": payload.body,
            "scheduled_at": payload.scheduled_at.isoformat() if payload.scheduled_at else None,
            "attachments": attachments,
            "auto_balance_mailboxes": True,
            "consent_evidence_digest": hashlib.sha256(payload.consent_evidence.encode()).hexdigest(),
            "recipient_source_filename": source_filename,
            "recipient_source_sha256": payload.source_sha256,
        },
        max_attempts=1,
    )
    db.add(task)
    db.flush()
    result = dispatch(db, task)
    audit(
        db,
        actor.subject,
        "campaign.draft_created",
        "task",
        str(task.id),
        {
            "campaign_key": campaign_key,
            "recipient_count": len(recipients),
            "batch_size": batch_size,
            "batch_count": len(recipient_batches),
            "recipient_digest": task.payload["recipient_digest"],
            "source": "telegram_mailing_wizard",
            "recipient_source_filename": source_filename,
            "recipient_source_sha256": payload.source_sha256,
            "attachment_count": len(attachments),
            "attachment_digests": [item["sha256"] for item in attachments],
        },
    )
    db.commit()
    return {
        "status": task.status,
        "task_id": task.id,
        "approval_id": result.get("approval_id"),
        "recipient_count": len(recipients),
        "batch_size": batch_size,
        "batch_count": len(recipient_batches),
        "attachment_count": len(attachments),
        "campaign_key": campaign_key,
        "idempotent_replay": False,
    }


@router.post("/outreach/campaigns/management-companies/draft", status_code=201)
def draft_management_company_campaign(payload: ManagementCompanyCampaignDraft, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "owner")
    suffix = payload.filename.lower().rsplit(".", 1)[-1] if "." in payload.filename else ""
    if suffix not in {"pdf", "doc", "docx", "xls", "xlsx", "odt"}:
        raise HTTPException(422, "Only PDF, Word, Excel or ODT documents are allowed")
    recipients = db.scalars(
        select(OutreachConsent.address)
        .join(BusinessRecord, OutreachConsent.record_id == BusinessRecord.id)
        .where(
            OutreachConsent.status == "verified",
            OutreachConsent.purpose == "commercial_outreach",
            BusinessRecord.record_type == "management_company",
        )
        .order_by(OutreachConsent.address)
    ).all()
    recipients = sorted(set(recipients))
    if not recipients:
        return {"status": "no_verified_recipients", "recipient_count": 0, "message": "Import management companies and record consent evidence before creating a campaign"}
    attachment = {"filename": payload.filename, "content_type": payload.content_type, "content_base64": payload.content_base64}
    digest = hashlib.sha256(f"{payload.subject}\n{payload.body}\n{payload.content_base64}".encode()).hexdigest()
    campaign_key = f"management-companies-{digest[:32]}"
    try:
        attachments = persist_campaign_attachments(campaign_key, [attachment])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    task = Task(
        title=f"Рассылка УК: {payload.filename}"[:255],
        agent_type="sales",
        priority="high",
        payload={
            "action": "execute_bulk_outreach_campaign",
            "action_kind": "bulk_outreach",
            "source": "telegram_document",
            "original_message": f"Разослать документ {payload.filename} по подтверждённой базе УК",
            "campaign_key": campaign_key,
            "recipients": recipients,
            "subject": payload.subject,
            "body": payload.body,
            "scheduled_at": payload.scheduled_at.isoformat() if payload.scheduled_at else None,
            "attachments": attachments,
            "auto_balance_mailboxes": True,
        },
        max_attempts=1,
    )
    db.add(task)
    db.flush()
    result = dispatch(db, task)
    audit(db, actor.subject, "campaign.draft_created", "task", str(task.id), {"campaign_key": campaign_key, "recipient_count": len(recipients), "attachment_sha256": attachments[0]["sha256"]})
    db.commit()
    return {"status": task.status, "task_id": task.id, "approval_id": result.get("approval_id"), "recipient_count": len(recipients), "campaign_key": campaign_key, "subject": payload.subject, "body": payload.body, "attachment_sha256": attachments[0]["sha256"]}


@router.post("/research/management-companies/import", status_code=201)
def import_management_company_registry(payload: ManagementCompanyImport, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Invalid base64 content") from exc
    if len(content) > settings.max_import_bytes:
        raise HTTPException(413, "Import file is too large")
    try:
        result = import_management_companies(db, filename=payload.filename, content=content, source_kind=payload.source_kind, source_url=payload.source_url, actor=actor.subject)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    event_bus.publish(db, "management_company.registry_imported", "import_job", str(result["job_id"]), result, actor=actor.subject)
    audit(db, actor.subject, "management_company.registry_imported", "import_job", str(result["job_id"]), result)
    db.commit()
    return result


@router.post("/research/management-companies/{record_id}/enrich")
def enrich_management_company_contacts(record_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    try:
        result = enrich_management_company(db, record_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    event_bus.publish(db, "management_company.contacts_enriched", "management_company", str(record_id), result, actor=actor.subject)
    audit(db, actor.subject, "management_company.contacts_enriched", "management_company", str(record_id), result)
    db.commit()
    return result


@router.post("/imports/leads", status_code=201)
def import_leads(payload: ImportFile, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    try: content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, binascii.Error): raise HTTPException(422, "Invalid base64 content")
    if len(content) > settings.max_import_bytes: raise HTTPException(413, "Import file is too large")
    job = ImportJob(filename=payload.filename, created_by=actor.subject); db.add(job); db.flush()
    rows = parse_lead_import(payload.filename, content)
    errors = []
    for index, source in enumerate(rows, 2):
        title = str(source.get("title") or source.get("company") or source.get("name") or "").strip()
        email = str(source.get("email") or "").strip().lower()
        if not title:
            errors.append({"row": index, "error": "missing title/company/name"}); continue
        external = email or str(source.get("external_id") or "").strip() or None
        if external and db.scalar(select(BusinessRecord.id).where(BusinessRecord.record_type == "lead", BusinessRecord.external_id == external)):
            job.skipped_rows += 1; continue
        record = BusinessRecord(record_type="lead", external_id=external, title=title, source="import", data={k: v for k, v in source.items() if v not in {None, ""}})
        db.add(record); db.flush(); job.imported_rows += 1
        event_bus.publish(db, "lead.created", "lead", str(record.id), {"title": record.title, "source": "import"}, idempotency_key=f"import:{job.id}:row:{index}")
    job.total_rows = len(rows); job.skipped_rows += len(errors); job.errors = errors; job.status = "completed_with_errors" if errors else "completed"; job.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit(); db.refresh(job)
    return {"job_id": job.id, "status": job.status, "total_rows": job.total_rows, "imported_rows": job.imported_rows, "skipped_rows": job.skipped_rows, "errors": job.errors}


@router.get("/inbox")
def inbox(status: Optional[str] = None, channel: Optional[str] = None, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    query = select(InboxMessage).order_by(InboxMessage.received_at.desc())
    if status: query = query.where(InboxMessage.status == status)
    if channel: query = query.where(InboxMessage.channel == channel)
    rows = db.scalars(query.limit(500)).all()
    return [{"id": x.id, "channel": x.channel, "external_id": x.external_id, "sender": x.sender, "recipient": x.recipient, "subject": x.subject, "body": x.body, "status": x.status, "record_id": x.record_id, "data": x.data, "received_at": x.received_at} for x in rows]


@router.post("/inbox", status_code=201)
def receive_message(payload: InboxMessageCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    if payload.record_id and not db.get(BusinessRecord, payload.record_id): raise HTTPException(404, "Record not found")
    values = payload.model_dump(); values["received_at"] = values["received_at"] or datetime.now(timezone.utc).replace(tzinfo=None)
    row = InboxMessage(**values); db.add(row)
    try: db.flush()
    except IntegrityError: db.rollback(); raise HTTPException(409, "Message already received")
    aggregate = "lead" if row.record_id else "inbox"
    aggregate_id = str(row.record_id or row.id)
    event_bus.publish(db, "inbox.message_received", aggregate, aggregate_id, {"message_id": row.id, "channel": row.channel, "sender": row.sender})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status}


@router.patch("/inbox/{message_id}")
def update_inbox(message_id: int, payload: InboxStatusUpdate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(InboxMessage, message_id)
    if not row: raise HTTPException(404, "Message not found")
    if payload.record_id and not db.get(BusinessRecord, payload.record_id): raise HTTPException(404, "Record not found")
    row.status = payload.status
    if payload.record_id is not None: row.record_id = payload.record_id
    event_bus.publish(db, "inbox.message_updated", "inbox", str(row.id), {"status": row.status, "record_id": row.record_id})
    db.commit()
    return {"id": row.id, "status": row.status, "record_id": row.record_id}


@router.get("/marketing/content")
def content_plan(status: Optional[str] = None, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    query = select(ContentItem).order_by(ContentItem.scheduled_at, ContentItem.id)
    if status: query = query.where(ContentItem.status == status)
    rows = db.scalars(query).all()
    return [{"id": x.id, "campaign_id": x.campaign_id, "channel": x.channel, "title": x.title, "body": x.body, "status": x.status, "scheduled_at": x.scheduled_at, "published_at": x.published_at, "metrics": x.metrics} for x in rows]


@router.post("/marketing/content", status_code=201)
def create_content(payload: ContentItemCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    if payload.campaign_id:
        campaign = db.get(BusinessRecord, payload.campaign_id)
        if not campaign or campaign.record_type != "campaign": raise HTTPException(404, "Campaign not found")
    if payload.status == "scheduled" and not payload.scheduled_at: raise HTTPException(422, "Scheduled content requires scheduled_at")
    row = ContentItem(**payload.model_dump()); db.add(row); db.flush()
    event_bus.publish(db, "marketing.content_created", "campaign", str(payload.campaign_id or row.id), {"content_id": row.id, "channel": row.channel, "status": row.status})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status}


@router.get("/hr/staffing")
def staffing(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    employees = db.scalars(select(OperatingEntity).where(OperatingEntity.entity_type == "employee")).all()
    vacancies = db.scalars(select(OperatingEntity).where(OperatingEntity.entity_type == "vacancy")).all()
    shifts = db.scalars(select(OperatingEntity).where(OperatingEntity.entity_type == "shift")).all()
    reserve = [x for x in employees if x.status == "reserve" or x.data.get("reserve")]
    unfilled = [x for x in shifts if not x.data.get("employee_id") and x.status not in {"completed", "cancelled"}]
    return {"employees": len(employees), "reserve": [entity_view(x) for x in reserve], "vacancies": [entity_view(x) for x in vacancies if x.status == "active"], "unfilled_shifts": [entity_view(x) for x in unfilled]}


@router.get("/hr/vacancies/{vacancy_id}/telegram-draft")
def vacancy_telegram_draft(vacancy_id: int, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    vacancy = db.get(OperatingEntity, vacancy_id)
    if not vacancy or vacancy.entity_type != "vacancy": raise HTTPException(404, "Vacancy not found")
    data = vacancy.data
    parts = [f"🧹 {vacancy.name}"]
    if data.get("district"): parts.append(f"📍 Район: {data['district']}")
    if data.get("schedule"): parts.append(f"🕒 График: {data['schedule']}")
    if data.get("rate"): parts.append(f"💰 Ставка: {data['rate']} ₽")
    if data.get("requirements"): parts.append(f"Требования: {data['requirements']}")
    if data.get("contact"): parts.append(f"Связь: {data['contact']}")
    return {"vacancy_id": vacancy.id, "text": "\n".join(parts), "requires_owner_approval_before_hiring": True}


@router.get("/finance/payment-calendar")
def payment_calendar(days: int = Query(default=30, ge=1, le=365), db: Session = Depends(get_db), _: Principal = Depends(principal)):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    from datetime import timedelta
    rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "payment", BusinessRecord.deadline_at.is_not(None), BusinessRecord.deadline_at <= now + timedelta(days=days)).order_by(BusinessRecord.deadline_at)).all()
    return [{"id": x.id, "title": x.title, "status": x.status, "amount": float(x.data.get("amount", 0) or 0), "deadline_at": x.deadline_at, "overdue": bool(x.deadline_at and x.deadline_at < now and x.status not in {"paid", "cancelled"})} for x in rows]


@router.get("/operations/quality")
def quality(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    complaints = db.scalars(select(OperatingEntity).where(OperatingEntity.entity_type == "complaint")).all()
    open_rows = [x for x in complaints if x.status not in {"resolved", "closed"}]
    breached = [x for x in open_rows if x.data.get("sla_deadline") and str(x.data["sla_deadline"]) < datetime.now(timezone.utc).isoformat()]
    return {"total_complaints": len(complaints), "open": len(open_rows), "sla_breached": len(breached), "items": [entity_view(x) for x in open_rows]}
