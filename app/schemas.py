from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    description: str = ""
    priority: str = Field(default="normal", pattern="^(low|normal|high|critical)$")
    agent_type: str = "orchestrator"
    payload: dict[str, Any] = Field(default_factory=dict)
    run_after: Optional[datetime] = None
    max_attempts: int = Field(default=3, ge=1, le=20)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)


class DecisionCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    rationale: str = ""
    kind: str = "operational"
    payload: dict[str, Any] = Field(default_factory=dict)


class RecordCreate(BaseModel):
    record_type: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=2, max_length=255)
    external_id: Optional[str] = None
    status: str = "new"
    score: Optional[float] = None
    owner: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    source: str = "manual"
    deadline_at: Optional[datetime] = None


class RecordUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=2, max_length=255)
    status: Optional[str] = Field(default=None, min_length=2, max_length=64)
    score: Optional[float] = Field(default=None, ge=0, le=100)
    owner: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    deadline_at: Optional[datetime] = None


class ContactEventCreate(BaseModel):
    channel: str = Field(min_length=2, max_length=32)
    direction: str = Field(pattern="^(inbound|outbound)$")
    subject: str = Field(default="", max_length=255)
    body: str = ""
    outcome: str = Field(default="", max_length=64)


class OutreachCreate(BaseModel):
    campaign_key: str = Field(min_length=2, max_length=128)
    recipient: EmailStr
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    scheduled_at: Optional[datetime] = None
    mailbox_id: Optional[int] = None
    template_id: Optional[int] = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class SuppressionCreate(BaseModel):
    address: EmailStr
    reason: str = "manual"


class KnowledgeCreate(BaseModel):
    namespace: str = Field(min_length=2, max_length=64)
    key: str = Field(min_length=1, max_length=128)
    value: dict[str, Any] = Field(default_factory=dict)
    source: str = "manual"
    confidence: float = Field(default=1.0, ge=0, le=1)
    valid_until: Optional[datetime] = None


class ApprovalDecision(BaseModel):
    note: str = ""


class OperatingEntityCreate(BaseModel):
    entity_type: str = Field(pattern="^(client|site|contract|employee|shift|complaint|vacancy)$")
    name: str = Field(min_length=2, max_length=255)
    external_id: Optional[str] = None
    status: str = "active"
    parent_id: Optional[int] = None
    owner: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class OperatingEntityUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    status: Optional[str] = None
    parent_id: Optional[int] = None
    owner: Optional[str] = None
    data: Optional[dict[str, Any]] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class GoalCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    description: str = ""
    owner: str = "ceo"
    metric: str = Field(min_length=1, max_length=128)
    baseline: float = 0
    target: float
    current: float = 0
    unit: str = ""
    deadline_at: Optional[datetime] = None
    strategy: dict[str, Any] = Field(default_factory=dict)


class GoalProgressUpdate(BaseModel):
    current: float
    note: str = ""


class DecisionOutcomeCreate(BaseModel):
    expected_value: Optional[float] = None
    actual_value: Optional[float] = None
    successful: Optional[bool] = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class TenderDocumentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_url: str = ""
    content_type: str = "application/octet-stream"
    storage_path: str = ""
    checksum: str = ""
    analysis: dict[str, Any] = Field(default_factory=dict)


class MailboxCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    address: EmailStr
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    username: str = ""
    secret_ref: str = Field(default="", pattern="^$|^SMTP_[A-Z0-9_]+$")
    per_minute: int = Field(default=10, ge=1, le=1000)
    per_day: int = Field(default=100, ge=1, le=100000)


class TemplateCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)


class SimulationRequest(BaseModel):
    site_id: Optional[int] = None
    revenue_change_percent: float = 0
    payroll_change_percent: float = 0
    materials_change_percent: float = 0
    penalty_change: float = 0


class ImportFile(BaseModel):
    filename: str = Field(min_length=5, max_length=255)
    content_base64: str = Field(min_length=1)


class StructuredDecisionCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    problem: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    options: list[dict[str, Any]] = Field(default_factory=list)
    expected_value: Optional[float] = None
    risk: str = "medium"
    confidence: float = Field(default=0.5, ge=0, le=1)
    recommended_action: str = ""
    requires_approval: bool = False
    approval_kind: Optional[str] = None
    deadline_at: Optional[datetime] = None


class CampaignLaunch(BaseModel):
    campaign_key: str = Field(min_length=2, max_length=128)
    recipients: list[EmailStr] = Field(min_length=1, max_length=10000)
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)
    mailbox_id: Optional[int] = None
    template_id: Optional[int] = None
    scheduled_at: Optional[datetime] = None
    approval_id: Optional[int] = None


class InboxMessageCreate(BaseModel):
    channel: str = Field(pattern="^(email|telegram|phone|web|whatsapp|other)$")
    external_id: str = Field(min_length=1, max_length=255)
    sender: str = Field(default="", max_length=320)
    recipient: str = Field(default="", max_length=320)
    subject: str = Field(default="", max_length=255)
    body: str = ""
    record_id: Optional[int] = None
    data: dict[str, Any] = Field(default_factory=dict)
    received_at: Optional[datetime] = None


class InboxStatusUpdate(BaseModel):
    status: str = Field(pattern="^(unread|read|assigned|replied|closed|spam)$")
    record_id: Optional[int] = None


class DeliveryEventCreate(BaseModel):
    event_type: str = Field(pattern="^(delivered|bounce|complaint|unsubscribe)$")
    recipient: EmailStr
    message_id: Optional[int] = None
    reason: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ContentItemCreate(BaseModel):
    campaign_id: Optional[int] = None
    channel: str = Field(pattern="^(telegram|vk|website|email|other)$")
    title: str = Field(min_length=2, max_length=255)
    body: str = ""
    status: str = Field(default="idea", pattern="^(idea|draft|approval|scheduled|published|cancelled)$")
    scheduled_at: Optional[datetime] = None
    metrics: dict[str, Any] = Field(default_factory=dict)
