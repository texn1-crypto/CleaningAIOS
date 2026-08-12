from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ApprovalKind(str, Enum):
    FINANCIAL = "financial"
    LEGAL = "legal"
    CONTRACT = "contract"
    HR_FINAL = "hr_final"
    TENDER_SUBMISSION = "tender_submission"
    BULK_OUTREACH = "bulk_outreach"


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    priority: Mapped[str] = mapped_column(String(32), default="normal")
    agent_type: Mapped[str] = mapped_column(String(64), default="orchestrator", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    rationale: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(64), default="operational")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="orchestrator")
    decided_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AgentState(Base):
    __tablename__ = "agent_states"
    agent_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class BusinessRecord(Base):
    __tablename__ = "business_records"
    __table_args__ = (UniqueConstraint("record_type", "external_id", name="uq_record_external"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    record_type: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(64), default="new", index=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(255), default="manual")
    deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ContactEvent(Base):
    __tablename__ = "contact_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("business_records.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Suppression(Base):
    __tablename__ = "suppressions"
    address: Mapped[str] = mapped_column(String(320), primary_key=True)
    reason: Mapped[str] = mapped_column(String(64), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (UniqueConstraint("campaign_key", "recipient", name="uq_campaign_recipient"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sender_mailboxes.id"), nullable=True, index=True)
    template_id: Mapped[Optional[int]] = mapped_column(ForeignKey("message_templates.id"), nullable=True)
    campaign_key: Mapped[str] = mapped_column(String(128), index=True)
    recipient: Mapped[str] = mapped_column(String(320), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str] = mapped_column(String(128), default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RoleBinding(Base):
    __tablename__ = "role_bindings"
    subject: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DomainEvent(Base):
    __tablename__ = "domain_events"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_event_idempotency"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(64), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CompanyKnowledge(Base):
    __tablename__ = "company_knowledge"
    __table_args__ = (UniqueConstraint("namespace", "key", name="uq_knowledge_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(255), default="system")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_type: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    correlation_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    cost: Mapped[float] = mapped_column(Float, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    action_kind: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64), default="task")
    resource_id: Mapped[str] = mapped_column(String(128), default="")
    requested_by: Mapped[str] = mapped_column(String(128), default="system")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decided_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class OperatingEntity(Base):
    """Linked system-of-record entity: client, site, contract, employee, shift or complaint."""
    __tablename__ = "operating_entities"
    __table_args__ = (UniqueConstraint("entity_type", "external_id", name="uq_operating_external"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(64), default="active", index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("operating_entities.id"), nullable=True, index=True)
    owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class BusinessGoal(Base):
    __tablename__ = "business_goals"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    owner: Mapped[str] = mapped_column(String(128), default="ceo")
    metric: Mapped[str] = mapped_column(String(128))
    baseline: Mapped[float] = mapped_column(Float, default=0)
    target: Mapped[float] = mapped_column(Float)
    current: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str] = mapped_column(String(32), default="")
    deadline_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    strategy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DecisionOutcome(Base):
    __tablename__ = "decision_outcomes"
    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), unique=True, index=True)
    expected_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    actual_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    successful: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    measured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TenderDocument(Base):
    __tablename__ = "tender_documents"
    __table_args__ = (UniqueConstraint("record_id", "source_url", name="uq_tender_document_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("business_records.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(String(1024), default="")
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    storage_path: Mapped[str] = mapped_column(String(1024), default="")
    checksum: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="registered", index=True)
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    analyzed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SenderMailbox(Base):
    __tablename__ = "sender_mailboxes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    address: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    username: Mapped[str] = mapped_column(String(320), default="")
    secret_ref: Mapped[str] = mapped_column(String(255), default="")
    per_minute: Mapped[int] = mapped_column(Integer, default=10)
    per_day: Mapped[int] = mapped_column(Integer, default=100)
    sent_today: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_type: Mapped[str] = mapped_column(String(64), default="leads")
    filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="processing", index=True)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    imported_rows: Mapped[int] = mapped_column(Integer, default=0)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(128), default="api-user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_inbox_external"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    sender: Mapped[str] = mapped_column(String(320), default="", index=True)
    recipient: Mapped[str] = mapped_column(String(320), default="")
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="unread", index=True)
    record_id: Mapped[Optional[int]] = mapped_column(ForeignKey("business_records.id"), nullable=True, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ContentItem(Base):
    __tablename__ = "content_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[Optional[int]] = mapped_column(ForeignKey("business_records.id"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="idea", index=True)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImprovementRequest(Base):
    __tablename__ = "improvement_requests"
    __table_args__ = (UniqueConstraint("dedup_key", name="uq_improvement_dedup"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(64))
    source_channel: Mapped[str] = mapped_column(String(32), default="telegram", index=True)
    source_user: Mapped[str] = mapped_column(String(128), default="owner")
    request_text: Mapped[str] = mapped_column(Text)
    intent: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    capability_score: Mapped[float] = mapped_column(Float, default=0)
    classification: Mapped[str] = mapped_column(String(64), default="capability_gap", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    missing_capabilities: Mapped[list[Any]] = mapped_column(JSON, default=list)
    suggested_function: Mapped[str] = mapped_column(Text, default="")
    codex_prompt: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[list[Any]] = mapped_column(JSON, default=list)
    test_plan: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    handoff_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    workspace_conversation_url: Mapped[str] = mapped_column(Text, default="")
    workspace_run_id: Mapped[str] = mapped_column(String(128), default="")
    implementation_summary: Mapped[str] = mapped_column(Text, default="")
    test_evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CompanyRequisite(Base):
    """Legal payment requisites only; never stores online-banking credentials."""
    __tablename__ = "company_requisites"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_name: Mapped[str] = mapped_column(String(128), unique=True)
    legal_name: Mapped[str] = mapped_column(String(255))
    inn: Mapped[str] = mapped_column(String(12), index=True)
    kpp: Mapped[str] = mapped_column(String(9), default="")
    ogrn: Mapped[str] = mapped_column(String(15), default="")
    settlement_account: Mapped[str] = mapped_column(String(20), default="")
    bank_name: Mapped[str] = mapped_column(String(255), default="")
    bic: Mapped[str] = mapped_column(String(9), default="")
    correspondent_account: Mapped[str] = mapped_column(String(20), default="")
    legal_address: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OwnerNotification(Base):
    __tablename__ = "owner_notifications"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_owner_notification_idempotency"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(32), index=True)
    recipient: Mapped[str] = mapped_column(String(320), default="")
    resource_type: Mapped[str] = mapped_column(String(64), default="")
    resource_id: Mapped[str] = mapped_column(String(128), default="")
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    content_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("content_items.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(128), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    public_url: Mapped[str] = mapped_column(String(1024), default="")
    storage_path: Mapped[str] = mapped_column(String(1024), default="")
    alt_text: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
