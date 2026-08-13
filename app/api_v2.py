from __future__ import annotations

import base64
import binascii
import hashlib
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import SessionLocal
from .config import settings
from .models import ApprovalRequest, BusinessGoal, BusinessRecord, ContentItem, Decision, DecisionOutcome, ImportJob, ImprovementRequest, InboxMessage, MessageTemplate, OperatingEntity, OutboundMessage, OutreachConsent, SenderMailbox, Suppression, Task, TenderDocument
from .integrations import collect_tenders, download_tender_document
from .improvements import retry_workspace_handoff
from .management_companies import enrich_management_company, import_management_companies
from .operations import business_graph, create_ceo_actions, entity_view, goal_progress, parse_lead_import, score_tender, simulate_site, site_economics, validate_entity
from .orchestrator import audit, dispatch
from .outreach import campaign_approval_payload, persist_campaign_attachments, queue_campaign, upsert_consent, verified_recipients
from .platform import approval_engine, event_bus
from .schemas import CampaignLaunch, ContentItemCreate, CustomerRequestedCampaignDraft, DecisionOutcomeCreate, DeliveryEventCreate, GoalCreate, GoalProgressUpdate, ImportFile, ImprovementUpdate, InboxMessageCreate, InboxStatusUpdate, MailboxCreate, ManagementCompanyCampaignDraft, ManagementCompanyImport, OperatingEntityCreate, OperatingEntityUpdate, OutreachConsentUpsert, RequestAnalysisCreate, SimulationRequest, StructuredDecisionCreate, TemplateCreate, TenderDocumentCreate
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
    parse_callback_token,
)
from .schemas import TelegramApprovalCallback, TelegramIdentityBind, TelegramIdentityRequest

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


def _require_telegram_owner(
    db: Session,
    payload: TelegramIdentityRequest | TelegramApprovalCallback,
):
    identity, reason = authorize_identity(
        db,
        user_id=payload.user_id,
        chat_id=payload.chat_id,
        minimum_role="owner",
    )
    if identity is None:
        db.commit()
        raise HTTPException(403, f"Telegram owner authorization failed: {reason}")
    return identity


@router.post("/telegram/control/approvals")
def telegram_approval_cards(
    payload: TelegramIdentityRequest,
    db: Session = Depends(get_db),
    channel: Principal = Depends(principal),
):
    require_role(channel, "owner")
    identity = _require_telegram_owner(db, payload)
    rows = db.scalars(
        select(ApprovalRequest)
        .where(ApprovalRequest.status == "pending")
        .order_by(ApprovalRequest.id.desc())
        .limit(20)
    ).all()
    return {"role": identity.role, "items": [approval_card(row) for row in rows]}


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
    return [{"id": x.id, "name": x.name, "address": x.address, "active": x.active, "per_minute": x.per_minute, "per_day": x.per_day, "sent_today": x.sent_today, "last_sent_at": x.last_sent_at, "secret_configured": bool(x.secret_ref and os.environ.get(x.secret_ref)), "inbound_enabled": x.inbound_enabled, "inbound_secret_configured": bool(x.imap_secret_ref and os.environ.get(x.imap_secret_ref))} for x in rows]


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
    mailboxes = db.scalars(select(SenderMailbox).order_by(SenderMailbox.id)).all()
    ready_mailboxes = sum(
        1
        for row in mailboxes
        if row.active
        and row.smtp_host
        and (row.username or row.address)
        and row.secret_ref
        and bool(os.environ.get(row.secret_ref))
    )
    default_sender_ready = bool(
        settings.smtp_host
        and settings.smtp_username
        and settings.smtp_password
        and settings.smtp_from_email
    )
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
    campaign_digest = hashlib.sha256(
        ("\n".join(recipients) + "\n" + payload.subject + "\n" + payload.body).encode()
    ).hexdigest()
    campaign_key = f"customer-requested-{campaign_digest[:32]}"

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
            "attachments": [],
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
