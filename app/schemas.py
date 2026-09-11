from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Optional

from pydantic import BaseModel, EmailStr, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    description: str = ""
    priority: str = Field(default="normal", pattern="^(low|normal|high|critical)$")
    agent_type: str = "orchestrator"
    assigned_to: str = Field(default="", max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    run_after: Optional[datetime] = None
    due_at: Optional[datetime] = None
    max_attempts: int = Field(default=3, ge=1, le=20)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)


class DecisionCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    rationale: str = ""
    kind: str = "operational"
    payload: dict[str, Any] = Field(default_factory=dict)


class RecordCreate(BaseModel):
    record_type: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
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


class KnowledgeDocumentCreate(BaseModel):
    namespace: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    title: str = Field(min_length=2, max_length=255)
    source_uri: str = Field(min_length=5, max_length=1024)
    content: str = Field(min_length=1, max_length=200_000)
    content_type: str = Field(
        default="text/plain",
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$",
    )
    minimum_role: str = Field(
        default="viewer",
        pattern="^(viewer|operator|manager|admin|owner)$",
    )
    confidence: float = Field(default=1.0, ge=0, le=1)
    source_updated_at: Optional[datetime] = None
    valid_until: Optional[datetime] = None


class ApprovalDecision(BaseModel):
    note: str = ""


class TelegramIdentityRequest(BaseModel):
    user_id: int
    chat_id: int
    minimum_role: str = Field(
        default="viewer",
        pattern="^(viewer|operator|manager|admin|owner)$",
    )


class TelegramIdentityBind(BaseModel):
    user_id: int
    chat_id: int
    role: str = Field(pattern="^(viewer|operator|manager|admin|owner)$")


class TelegramApprovalCallback(BaseModel):
    user_id: int
    chat_id: int
    callback_token: str = Field(min_length=20, max_length=64)
    note: str = Field(default="Решение принято в Telegram", max_length=2000)


class TelegramAlertCallback(BaseModel):
    user_id: int
    chat_id: int
    callback_token: str = Field(min_length=20, max_length=64)


class TelegramTaskQuery(BaseModel):
    user_id: int
    chat_id: int
    view: str = Field(
        default="all",
        pattern="^(all|mine|overdue|critical|critical_events)$",
    )
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=10, ge=1, le=20)


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


class TenderEvaluationRequest(BaseModel):
    description: Optional[str] = Field(default=None, max_length=20_000)
    category: Optional[str] = Field(default=None, max_length=1000)
    purchase_object: Optional[str] = Field(default=None, max_length=5000)
    region: Optional[str] = Field(default=None, max_length=1000)
    place_of_performance: Optional[str] = Field(default=None, max_length=5000)
    contract_value: Optional[float] = Field(default=None, gt=0)
    contract_months: Optional[float] = Field(default=None, gt=0, le=1200)
    monthly_payroll: Optional[float] = Field(default=None, ge=0)
    monthly_materials: Optional[float] = Field(default=None, ge=0)
    monthly_logistics: Optional[float] = Field(default=None, ge=0)
    monthly_other_costs: Optional[float] = Field(default=0, ge=0)
    tax_percent: Optional[float] = Field(default=None, ge=0, le=100)
    payment_delay_days: Optional[int] = Field(default=None, ge=0, le=3650)
    available_working_capital: Optional[float] = Field(default=None, ge=0)
    min_margin_percent: Optional[float] = Field(default=None, ge=0, le=100)
    company_fit: Optional[float] = Field(default=None, ge=0, le=100)
    logistics_fit: Optional[float] = Field(default=None, ge=0, le=100)
    staffing_fit: Optional[float] = Field(default=None, ge=0, le=100)
    application_security: float = Field(default=0, ge=0)
    performance_security: float = Field(default=0, ge=0)
    onboarding_costs: float = Field(default=0, ge=0)
    contingency_percent: float = Field(default=5, ge=0, le=100)
    conservative_cost_increase_percent: float = Field(default=15, ge=0, le=500)
    conservative_revenue_decrease_percent: float = Field(default=0, ge=0, le=100)
    legal_risk_flags: list[str] = Field(default_factory=list, max_length=100)
    required_documents: list[str] = Field(default_factory=list, max_length=500)
    queue_participation_review: bool = True


class TenderEvidenceRef(BaseModel):
    document_id: int = Field(gt=0)
    document_checksum: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    locator: str = Field(min_length=1, max_length=128)
    excerpt: str = Field(min_length=1, max_length=2000)


class TenderRequirementFact(BaseModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    description: str = Field(min_length=2, max_length=2000)
    mandatory: bool = True
    status: str = Field(pattern="^(satisfied|not_satisfied|unknown)$")
    evidence: list[TenderEvidenceRef] = Field(default_factory=list, max_length=20)


class TenderQualificationFact(BaseModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    description: str = Field(min_length=2, max_length=2000)
    status: str = Field(pattern="^(satisfied|not_satisfied|unknown)$")
    evidence: list[TenderEvidenceRef] = Field(default_factory=list, max_length=20)


class CompanyProfileDocument(BaseModel):
    document_type: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    issue_date: date
    expiry_date: date
    issuer: str = Field(min_length=2, max_length=255)
    verification_status: str = Field(pattern="^(verified|invalid|unknown)$")
    file_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class CompanyCapabilityFact(BaseModel):
    code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    description: str = Field(min_length=2, max_length=1000)
    status: str = Field(pattern="^(satisfied|not_satisfied|unknown)$")
    evidence_file_hashes: list[
        Annotated[str, Field(pattern=r"^[a-fA-F0-9]{64}$")]
    ] = Field(default_factory=list, max_length=20)


class CompanyProfileSnapshotCreate(BaseModel):
    verified_at: datetime
    taxation_regime: str = Field(min_length=1, max_length=128)
    vat_status: str = Field(pattern="^(payer|exempt|unknown)$")
    categories_allowed: list[
        Annotated[str, Field(min_length=1, max_length=255)]
    ] = Field(default_factory=list, max_length=200)
    geographic_capabilities: list[
        Annotated[str, Field(min_length=1, max_length=255)]
    ] = Field(default_factory=list, max_length=200)
    internal_min_margin_percent: Optional[Decimal] = Field(
        default=None,
        ge=0,
        le=100,
        max_digits=7,
        decimal_places=4,
    )
    available_financing: Optional[Decimal] = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=2,
    )
    credit_limit: Optional[Decimal] = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=2,
    )
    max_exposure: Optional[Decimal] = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=2,
    )
    working_capital_limit: Optional[Decimal] = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=2,
    )
    risk_flags: list[
        Annotated[str, Field(min_length=1, max_length=500)]
    ] = Field(default_factory=list, max_length=200)
    documents: list[CompanyProfileDocument] = Field(min_length=1, max_length=500)
    capabilities: list[CompanyCapabilityFact] = Field(min_length=1, max_length=200)


class TenderPrequalificationCreate(BaseModel):
    checks: list[TenderQualificationFact] = Field(min_length=1, max_length=200)
    company_profile_snapshot_hash: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )


class TenderProductComplianceParameter(BaseModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    parameter: str = Field(min_length=1, max_length=255)
    required_value: str = Field(min_length=1, max_length=1000)
    offered_value: str = Field(default="", max_length=1000)
    mandatory: bool = True
    match_status: str = Field(pattern="^(match|mismatch|unknown)$")
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1, max_digits=5, decimal_places=4)
    evidence: list[TenderEvidenceRef] = Field(default_factory=list, max_length=20)


class TenderProductSpecificationReviewDecision(BaseModel):
    candidate_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted: bool
    corrected_parameter: Optional[str] = Field(default=None, min_length=1, max_length=200)
    corrected_value: Optional[str] = Field(default=None, min_length=1, max_length=500)
    reason: str = Field(default="", max_length=1000)


class TenderProductSpecificationReviewCreate(BaseModel):
    document_checksum: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    extractor_version: str = Field(min_length=1, max_length=128)
    decisions: list[TenderProductSpecificationReviewDecision] = Field(
        min_length=1,
        max_length=500,
    )


class TenderRequirementReviewDecision(BaseModel):
    candidate_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted: bool
    corrected_code: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    )
    corrected_description: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=2000,
    )
    mandatory: Optional[bool] = None
    compliance_status: str = Field(
        default="unknown",
        pattern="^(satisfied|not_satisfied|unknown)$",
    )
    reason: str = Field(default="", max_length=1000)


class TenderRequirementReviewCreate(BaseModel):
    document_checksum: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    extractor_version: str = Field(min_length=1, max_length=128)
    decisions: list[TenderRequirementReviewDecision] = Field(
        min_length=1,
        max_length=500,
    )


class TenderProductComparisonReviewDecision(BaseModel):
    candidate_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    match_status: str = Field(pattern="^(match|mismatch|unknown)$")
    reason: str = Field(default="", max_length=1000)


class TenderProductComparisonReviewCreate(BaseModel):
    draft_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decisions: list[TenderProductComparisonReviewDecision] = Field(
        min_length=1,
        max_length=500,
    )


class TenderSupplierQuoteInput(BaseModel):
    supplier_name: str = Field(min_length=2, max_length=255)
    quote_reference: str = Field(min_length=1, max_length=255)
    total_cost: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(default="RUB", pattern="^RUB$")
    vat_included: bool
    stock_status: str = Field(pattern="^(confirmed|unknown|unavailable)$")
    valid_until: datetime
    evidence: list[TenderEvidenceRef] = Field(min_length=1, max_length=20)


class TenderSupplierQuoteCreate(TenderSupplierQuoteInput):
    prequalification_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    supplier_identifier: str = Field(min_length=1, max_length=64)
    product_sku: str = Field(min_length=1, max_length=128)
    brand: str = Field(default="", max_length=128)
    manufacturer: str = Field(min_length=1, max_length=255)
    country: str = Field(min_length=2, max_length=128)
    unit_price: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    minimum_order: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=3)
    requested_quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=3)
    quantity_available: Optional[Decimal] = Field(default=None, ge=0, max_digits=18, decimal_places=3)
    stock_location: str = Field(default="", max_length=255)
    lead_time_days: Optional[int] = Field(default=None, ge=0, le=3650)
    delivery_cost: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    delivery_included: bool = False
    payment_terms: str = Field(min_length=1, max_length=1000)
    quoted_at: datetime
    specification_match: str = Field(pattern="^(match|mismatch|unknown)$")
    certificate_status: str = Field(pattern="^(valid|not_required|missing|unknown)$")
    supplier_reliability: str = Field(pattern="^(trusted|unknown|blocked)$")
    source: str = Field(min_length=1, max_length=1024)
    verified_at: Optional[datetime] = None


class TenderDecisionSnapshotCreate(BaseModel):
    requirements: list[TenderRequirementFact] = Field(min_length=1, max_length=500)
    qualification_checks: list[TenderQualificationFact] = Field(min_length=1, max_length=200)
    prequalification_snapshot_hash: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    supplier_quote_snapshot_hash: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    product_compliance_required: bool = False
    product_comparison_review_hash: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    product_compliance: list[TenderProductComplianceParameter] = Field(
        default_factory=list,
        max_length=500,
    )
    supplier_quote: TenderSupplierQuoteInput
    contract_value: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    contract_months: int = Field(gt=0, le=1200)
    payroll_cost: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    logistics_cost: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    other_direct_cost: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    onboarding_cost: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    application_security: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    performance_security: Decimal = Field(default=Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    available_working_capital: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    payment_delay_days: int = Field(ge=0, le=3650)
    tax_percent: Decimal = Field(ge=0, le=100, max_digits=7, decimal_places=4)
    contingency_percent: Decimal = Field(default=Decimal("5"), ge=0, le=100, max_digits=7, decimal_places=4)
    minimum_margin_percent: Decimal = Field(ge=0, le=100, max_digits=7, decimal_places=4)
    conservative_cost_increase_percent: Decimal = Field(default=Decimal("15"), ge=0, le=500, max_digits=7, decimal_places=4)
    conservative_revenue_decrease_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100, max_digits=7, decimal_places=4)
    auction_expected_discount_percent: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=100,
        max_digits=7,
        decimal_places=4,
    )
    maximum_risk_score: int = Field(default=35, ge=0, le=100)
    queue_participation_review: bool = True


class MailboxCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    address: EmailStr
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    username: str = ""
    secret_ref: str = Field(default="", pattern="^$|^SMTP_[A-Z0-9_]+$")
    imap_host: str = ""
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_username: str = ""
    imap_secret_ref: str = Field(default="", pattern="^$|^IMAP_[A-Z0-9_]+$")
    inbound_enabled: bool = False
    per_minute: int = Field(default=10, ge=1, le=1000)
    per_day: int = Field(default=7, ge=1, le=100000)


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


class ManagementCompanyImport(ImportFile):
    source_kind: str = Field(
        pattern="^(gis_housing|housing_inspection|company_website|manual_public_export|owner_baseline)$"
    )
    source_url: str = Field(min_length=8, max_length=1024)


class OutreachConsentUpsert(BaseModel):
    address: EmailStr
    record_id: Optional[int] = None
    status: str = Field(default="verified", pattern="^(verified|revoked)$")
    purpose: str = Field(default="commercial_outreach", min_length=3, max_length=128)
    source_url: str = Field(min_length=8, max_length=1024)
    evidence: str = Field(min_length=10, max_length=4000)


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
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    auto_balance_mailboxes: bool = True


class CustomerRequestedCampaignDraft(BaseModel):
    recipients: list[EmailStr] = Field(min_length=1, max_length=1000)
    consent_evidence: str = Field(min_length=10, max_length=4000)
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=10000)
    source_filename: Optional[str] = Field(default=None, max_length=255)
    source_sha256: Optional[str] = Field(default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    scheduled_at: Optional[datetime] = None


class ManagementCompanyCampaignDraft(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=128)
    content_base64: str = Field(min_length=1)
    subject: str = Field(default="Предложение по клининговому обслуживанию", min_length=1, max_length=255)
    body: str = Field(
        default=(
            "Добрый день! Предлагаем обсудить профессиональное клининговое обслуживание ваших объектов. "
            "Подробное предложение приложено к письму. Если тема актуальна, ответьте на это сообщение — "
            "мы уточним параметры объекта и подготовим расчёт."
        ),
        min_length=1,
        max_length=20_000,
    )
    scheduled_at: Optional[datetime] = None


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


class RequestAnalysisCreate(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    intent: dict[str, Any] = Field(default_factory=dict)
    source_channel: str = Field(default="telegram", pattern="^(telegram|web|api|other)$")
    source_user: str = Field(default="owner", min_length=1, max_length=128)


class ImprovementUpdate(BaseModel):
    status: str = Field(pattern="^(queued|handed_off|in_progress|implemented|rejected|blocked)$")
    implementation_summary: str = Field(default="", max_length=8000)
    test_evidence: list[dict[str, Any]] = Field(default_factory=list)


class ExternalActionsKillSwitchUpdate(BaseModel):
    active: bool
    reason: str = Field(default="", max_length=500)


class CapabilityFlagUpdate(BaseModel):
    enabled: bool
    reason: str = Field(min_length=3, max_length=500)


class DeliveryEventCreate(BaseModel):
    event_type: str = Field(pattern="^(delivered|bounce|complaint|unsubscribe)$")
    recipient: EmailStr
    message_id: Optional[int] = None
    reason: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class ContentItemCreate(BaseModel):
    campaign_id: Optional[int] = None
    channel: str = Field(pattern="^(telegram|vk|odnoklassniki|instagram|website|email|other)$")
    title: str = Field(min_length=2, max_length=255)
    body: str = ""
    status: str = Field(default="idea", pattern="^(idea|draft|approval|scheduled|published|cancelled)$")
    scheduled_at: Optional[datetime] = None
    metrics: dict[str, Any] = Field(default_factory=dict)


class ContentItemUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=2, max_length=255)
    body: Optional[str] = None
    status: Optional[str] = Field(default=None, pattern="^(idea|draft|approval|scheduled|published|cancelled)$")
    scheduled_at: Optional[datetime] = None
    metrics: Optional[dict[str, Any]] = None


class PublicLeadCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(default="", max_length=32)
    email: Optional[EmailStr] = None
    company: str = Field(default="", max_length=255)
    service: str = Field(pattern="^(mcd|business_center|commercial|general|other)$")
    object_area: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    budget: Optional[float] = Field(default=None, ge=0, le=1_000_000_000)
    urgency: str = Field(default="month", pattern="^(today|week|month|planning)$")
    message: str = Field(default="", max_length=3000)
    consent: bool
    source: str = Field(default="website", max_length=128)
    utm_source: str = Field(default="", max_length=128)
    utm_medium: str = Field(default="", max_length=128)
    utm_campaign: str = Field(default="", max_length=128)
    website: str = Field(default="", max_length=255, description="Spam honeypot; must remain empty")


class LeadAutopilotCreate(BaseModel):
    conversation_id: str = Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    requester_key: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(default="", max_length=32)
    email: Optional[EmailStr] = None
    company: str = Field(default="", max_length=255)
    telegram_username: str = Field(default="", max_length=64, pattern=r"^$|^[A-Za-z0-9_]{5,32}$")
    service: str = Field(pattern="^(mcd|business_center|office|retail|industrial|warehouse|commercial|general|other)$")
    cleaning_kind: str = Field(default="maintenance", pattern="^(maintenance|general|post_construction)$")
    object_area: float = Field(gt=0, le=10_000_000)
    location: str = Field(min_length=2, max_length=500)
    frequency: str = Field(pattern="^(once|weekly|weekdays|daily|custom)$")
    urgency: str = Field(default="month", pattern="^(today|week|month|planning)$")
    message: str = Field(default="", max_length=3000)
    consent: bool


class MarketingProviderCreate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    platform: str = Field(pattern="^(yandex_direct|yandex_business|vk_ads|2gis|avito|telegram_ads|agency|other)$")
    contact: str = Field(default="", max_length=320)
    website: str = Field(default="", max_length=1024)
    status: str = Field(default="scouting", pattern="^(scouting|contacted|proposal|active|paused|rejected)$")
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=4000)


class MarketingExperimentCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    channel: str = Field(pattern="^(yandex_direct|yandex_business|vk_ads|2gis|avito|telegram_ads|seo|content|other)$")
    hypothesis: str = Field(min_length=5, max_length=4000)
    audience: str = Field(min_length=2, max_length=2000)
    offer: str = Field(min_length=2, max_length=2000)
    primary_metric: str = Field(default="qualified_leads", max_length=128)
    budget_limit: float = Field(default=0, ge=0, le=1_000_000_000)
    utm_campaign: str = Field(min_length=2, max_length=128)


class MarketingExperimentLaunch(BaseModel):
    approval_id: Optional[int] = None
    external_campaign_id: str = Field(default="", max_length=255)


class MarketingInvoiceCreate(BaseModel):
    provider_id: int
    requisites_profile_id: int
    invoice_number: str = Field(min_length=1, max_length=128)
    amount: float = Field(gt=0, le=1_000_000_000)
    currency: str = Field(default="RUB", pattern="^RUB$")
    due_at: Optional[datetime] = None
    document_url: str = Field(default="", max_length=1024)
    description: str = Field(default="", max_length=2000)


class CompanyRequisiteCreate(BaseModel):
    profile_name: str = Field(min_length=2, max_length=128)
    legal_name: str = Field(min_length=2, max_length=255)
    inn: str = Field(pattern="^(?:[0-9]{10}|[0-9]{12})$")
    kpp: str = Field(default="", pattern="^$|^[0-9]{9}$")
    ogrn: str = Field(default="", pattern="^$|^(?:[0-9]{13}|[0-9]{15})$")
    settlement_account: str = Field(default="", pattern="^$|^[0-9]{20}$")
    currency: str = Field(default="RUB", pattern="^(?:RUB|RUR)$")
    bank_name: str = Field(default="", max_length=255)
    bank_inn: str = Field(default="", pattern="^$|^[0-9]{10}$")
    bank_address: str = Field(default="", max_length=500)
    bic: str = Field(default="", pattern="^$|^[0-9]{9}$")
    correspondent_account: str = Field(default="", pattern="^$|^[0-9]{20}$")
    legal_address: str = Field(default="", max_length=500)


class MediaAssetCreate(BaseModel):
    content_item_id: Optional[int] = None
    kind: str = Field(pattern="^(image|video)$")
    title: str = Field(min_length=2, max_length=255)
    prompt: str = Field(default="", max_length=8000)
    provider: str = Field(default="", max_length=128)
    public_url: str = Field(default="", max_length=1024)
    storage_path: str = Field(default="", max_length=1024)
    alt_text: str = Field(default="", max_length=500)
    status: str = Field(default="queued", pattern="^(queued|generating|ready|published|failed|credentials_required|adapter_required)$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediaAssetUpdate(BaseModel):
    provider: Optional[str] = Field(default=None, max_length=128)
    public_url: Optional[str] = Field(default=None, max_length=1024)
    storage_path: Optional[str] = Field(default=None, max_length=1024)
    alt_text: Optional[str] = Field(default=None, max_length=500)
    status: Optional[str] = Field(default=None, pattern="^(queued|generating|ready|published|failed|credentials_required|adapter_required)$")
    metadata: Optional[dict[str, Any]] = None
