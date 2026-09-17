from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AuthorityEnvelope, AuthorityEnvelopeUse


class AutonomyMode(str, Enum):
    AUTO_SAFE = "AUTO_SAFE"
    AUTO_WITHIN_LIMIT = "AUTO_WITHIN_LIMIT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    FORBIDDEN = "FORBIDDEN"


AUTO_SAFE_ACTIONS = frozenset(
    {
        "tender_source_refresh",
        "tender_document_download",
        "tender_document_analyze",
        "tender_economics_calculate",
        "tender_draft_prepare",
        "public_contact_discovery",
        "lead_deduplicate",
        "lead_qualify",
        "reply_collect",
        "reply_classify",
        "commercial_draft_prepare",
        "contract_draft_prepare",
        "marketing_metrics_collect",
        "marketing_campaign_pause",
        "report_generate",
        "notification_queue",
    }
)

AUTO_WITHIN_LIMIT_ACTIONS = frozenset(
    {
        "supplier_rfq",
        "inbound_lead_reply",
        "outreach_follow_up",
        "calendar_invitation",
        "marketing_campaign_manage",
        "marketing_bid_adjustment",
    }
)

APPROVAL_KIND_BY_ACTION = {
    "marketing_budget_increase": "financial",
    "marketing_invoice_payment": "financial",
    "contract_commitment": "contract",
    "tender_participation": "tender_participation",
    "tender_submission": "tender_submission",
    "bulk_cold_outreach": "bulk_outreach",
    "final_hiring_decision": "hr_final",
}

FORBIDDEN_ACTIONS = frozenset(
    {
        "automatic_payment",
        "automatic_bank_requisites_change",
        "automatic_electronic_signature",
        "automatic_contract_signature",
        "automatic_tender_submission",
        "automatic_kill_switch_disable",
        "automatic_irreversible_delete",
        "secret_or_personal_data_to_ai",
    }
)

CAPABILITY_BY_LIMITED_ACTION = {
    "supplier_rfq": "bulk_outreach",
    "inbound_lead_reply": "bulk_outreach",
    "outreach_follow_up": "bulk_outreach",
    "calendar_invitation": "bulk_outreach",
    "marketing_campaign_manage": "financial",
    "marketing_bid_adjustment": "financial",
}

AMOUNT_REQUIRED_ACTIONS = frozenset(
    {"marketing_campaign_manage", "marketing_bid_adjustment"}
)

SCOPE_FIELDS = (
    "channel",
    "region",
    "service_type",
    "recipient_category",
    "template_key",
    "tender_category",
)


class AuthorityScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: list[str] = Field(default_factory=list, max_length=32)
    regions: list[str] = Field(default_factory=list, max_length=64)
    service_types: list[str] = Field(default_factory=list, max_length=64)
    recipient_categories: list[str] = Field(default_factory=list, max_length=64)
    template_keys: list[str] = Field(default_factory=list, max_length=64)
    tender_categories: list[str] = Field(default_factory=list, max_length=64)


class AuthorityLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_actions_total: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    max_actions_per_day: Optional[int] = Field(default=None, ge=1, le=100_000)
    max_amount_per_action: Optional[Decimal] = Field(default=None, ge=0)
    max_daily_amount: Optional[Decimal] = Field(default=None, ge=0)
    max_weekly_amount: Optional[Decimal] = Field(default=None, ge=0)
    max_monthly_amount: Optional[Decimal] = Field(default=None, ge=0)
    max_recipients_per_action: Optional[int] = Field(default=None, ge=1, le=10_000)
    minimum_margin_percent: Optional[Decimal] = Field(default=None, ge=0, le=100)
    maximum_risk_score: Optional[int] = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def bounded(self) -> "AuthorityLimits":
        values = self.model_dump(exclude_none=True)
        if not values:
            raise ValueError("At least one deterministic authority limit is required")
        return self


class AuthorityEnvelopeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope_key: str = Field(
        min_length=3, max_length=128, pattern=r"^[a-z0-9][a-z0-9_.:-]+$"
    )
    action: str = Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")
    scope: AuthorityScope = Field(default_factory=AuthorityScope)
    limits: AuthorityLimits
    starts_at: Optional[datetime] = None
    expires_at: datetime
    rationale: str = Field(min_length=3, max_length=1000)


class AuthorityEnvelopeRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=1000)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def authority_context_digest(context: dict[str, Any]) -> str:
    return _digest(context)


def action_policy(action: str) -> dict[str, Any]:
    if action in FORBIDDEN_ACTIONS:
        return {"action": action, "mode": AutonomyMode.FORBIDDEN.value}
    if action in APPROVAL_KIND_BY_ACTION:
        return {
            "action": action,
            "mode": AutonomyMode.APPROVAL_REQUIRED.value,
            "approval_kind": APPROVAL_KIND_BY_ACTION[action],
        }
    if action in AUTO_WITHIN_LIMIT_ACTIONS:
        return {
            "action": action,
            "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
            "capability": CAPABILITY_BY_LIMITED_ACTION[action],
        }
    if action in AUTO_SAFE_ACTIONS:
        return {"action": action, "mode": AutonomyMode.AUTO_SAFE.value}
    return {
        "action": action,
        "mode": AutonomyMode.APPROVAL_REQUIRED.value,
        "approval_kind": None,
        "reason": "unknown_action_fails_closed",
    }


def authority_policy_catalog() -> list[dict[str, Any]]:
    actions = sorted(
        AUTO_SAFE_ACTIONS
        | AUTO_WITHIN_LIMIT_ACTIONS
        | set(APPROVAL_KIND_BY_ACTION)
        | FORBIDDEN_ACTIONS
    )
    return [action_policy(action) for action in actions]


def _envelope_input(
    *,
    envelope_key: str,
    action: str,
    scope: dict[str, Any],
    limits: dict[str, Any],
    starts_at: datetime,
    expires_at: datetime,
    rationale: str,
) -> dict[str, Any]:
    return {
        "envelope_key": envelope_key,
        "action": action,
        "scope": scope,
        "limits": limits,
        "starts_at": starts_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "rationale": rationale.strip(),
    }


def envelope_view(row: AuthorityEnvelope, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or now_utc()
    effective_status = row.status
    if effective_status == "active" and row.expires_at <= current:
        effective_status = "expired"
    return {
        "id": row.id,
        "envelope_key": row.envelope_key,
        "action": row.action,
        "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
        "status": effective_status,
        "scope": row.scope,
        "limits": row.limits,
        "input_hash": row.input_hash,
        "rationale": row.rationale,
        "starts_at": row.starts_at,
        "expires_at": row.expires_at,
        "approved_by": row.approved_by,
        "version": row.version,
        "revoked_by": row.revoked_by,
        "revoked_at": row.revoked_at,
        "revocation_reason": row.revocation_reason,
        "created_at": row.created_at,
    }


def create_authority_envelope(
    db: Session,
    payload: AuthorityEnvelopeCreate,
    *,
    actor: str,
    now: datetime | None = None,
) -> tuple[AuthorityEnvelope, bool]:
    if payload.action not in AUTO_WITHIN_LIMIT_ACTIONS:
        raise ValueError("Authority envelopes are allowed only for AUTO_WITHIN_LIMIT actions")
    current = now or now_utc()
    existing = db.scalar(
        select(AuthorityEnvelope).where(
            AuthorityEnvelope.envelope_key == payload.envelope_key
        )
    )
    starts_at = _utc_naive(
        payload.starts_at or (existing.starts_at if existing else current)
    )
    expires_at = _utc_naive(payload.expires_at)
    if starts_at > current + timedelta(minutes=5):
        raise ValueError("Authority envelope cannot start more than five minutes in the future")
    if expires_at <= current:
        raise ValueError("Authority envelope expiry must be in the future")
    if expires_at > current + timedelta(days=366):
        raise ValueError("Authority envelope cannot be valid for more than 366 days")
    scope = payload.scope.model_dump(mode="json")
    limits = payload.limits.model_dump(mode="json", exclude_none=True)
    if payload.action in AMOUNT_REQUIRED_ACTIONS and not any(
        name in limits
        for name in (
            "max_amount_per_action",
            "max_daily_amount",
            "max_weekly_amount",
            "max_monthly_amount",
        )
    ):
        raise ValueError("Marketing authority requires an explicit monetary limit")
    canonical = _envelope_input(
        envelope_key=payload.envelope_key,
        action=payload.action,
        scope=scope,
        limits=limits,
        starts_at=starts_at,
        expires_at=expires_at,
        rationale=payload.rationale,
    )
    digest = _digest(canonical)
    if existing:
        if existing.input_hash == digest:
            return existing, False
        raise ValueError("Authority envelope key already exists with different terms")
    row = AuthorityEnvelope(
        envelope_key=payload.envelope_key,
        action=payload.action,
        scope=scope,
        limits=limits,
        input_hash=digest,
        rationale=payload.rationale.strip(),
        starts_at=starts_at,
        expires_at=expires_at,
        approved_by=actor,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        concurrent = db.scalar(
            select(AuthorityEnvelope).where(
                AuthorityEnvelope.envelope_key == payload.envelope_key
            )
        )
        if concurrent and concurrent.input_hash == digest:
            return concurrent, False
        raise ValueError("Authority envelope key already exists") from exc
    return row, True


def revoke_authority_envelope(
    row: AuthorityEnvelope,
    *,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> bool:
    if row.status == "revoked":
        return False
    if row.status != "active":
        raise ValueError("Only an active authority envelope can be revoked")
    row.status = "revoked"
    row.revoked_by = actor
    row.revoked_at = now or now_utc()
    row.revocation_reason = reason.strip()
    row.version += 1
    return True


def _scope_matches(scope: dict[str, Any], context: dict[str, Any]) -> bool:
    for field in SCOPE_FIELDS:
        allowed = scope.get(f"{field}s")
        if not allowed:
            continue
        actual = str(context.get(field) or "").strip()
        if not actual or actual not in allowed:
            return False
    return True


def _period_start(current: datetime, period: str) -> datetime:
    if period == "day":
        return current.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start = current - timedelta(days=current.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _usage_totals(
    db: Session,
    envelope_id: int,
    *,
    since: datetime | None = None,
) -> tuple[int, Decimal]:
    query = select(
        func.count(AuthorityEnvelopeUse.id),
        func.coalesce(func.sum(AuthorityEnvelopeUse.amount), 0),
    ).where(AuthorityEnvelopeUse.envelope_id == envelope_id)
    if since is not None:
        query = query.where(AuthorityEnvelopeUse.occurred_at >= since)
    count, amount = db.execute(query).one()
    return int(count or 0), Decimal(str(amount or 0))


def _limit_failure(
    db: Session,
    row: AuthorityEnvelope,
    *,
    context: dict[str, Any],
    current: datetime,
) -> str | None:
    limits = row.limits or {}
    try:
        amount = Decimal(str(context.get("amount", 0) or 0))
    except Exception:
        return "invalid_amount"
    if not amount.is_finite() or amount < 0:
        return "invalid_amount"
    if row.action in AMOUNT_REQUIRED_ACTIONS and "amount" not in context:
        return "amount_required"
    recipients = context.get("recipients", 1)
    if isinstance(recipients, bool) or not isinstance(recipients, int) or recipients < 1:
        return "invalid_recipient_count"
    if (
        limits.get("max_recipients_per_action") is not None
        and recipients > int(limits["max_recipients_per_action"])
    ):
        return "max_recipients_per_action_exceeded"
    if (
        limits.get("max_amount_per_action") is not None
        and amount > Decimal(str(limits["max_amount_per_action"]))
    ):
        return "max_amount_per_action_exceeded"
    if limits.get("minimum_margin_percent") is not None:
        if "margin_percent" not in context:
            return "margin_percent_required"
        if Decimal(str(context["margin_percent"])) < Decimal(
            str(limits["minimum_margin_percent"])
        ):
            return "minimum_margin_not_met"
    if limits.get("maximum_risk_score") is not None:
        if "risk_score" not in context:
            return "risk_score_required"
        risk = context["risk_score"]
        if isinstance(risk, bool) or not isinstance(risk, int) or risk < 0 or risk > 100:
            return "invalid_risk_score"
        if risk > int(limits["maximum_risk_score"]):
            return "maximum_risk_exceeded"
    total_count, _ = _usage_totals(db, row.id)
    if (
        limits.get("max_actions_total") is not None
        and total_count + 1 > int(limits["max_actions_total"])
    ):
        return "max_actions_total_exceeded"
    day_count, day_amount = _usage_totals(
        db, row.id, since=_period_start(current, "day")
    )
    if (
        limits.get("max_actions_per_day") is not None
        and day_count + 1 > int(limits["max_actions_per_day"])
    ):
        return "max_actions_per_day_exceeded"
    for period, field in (
        ("day", "max_daily_amount"),
        ("week", "max_weekly_amount"),
        ("month", "max_monthly_amount"),
    ):
        if limits.get(field) is None:
            continue
        used_amount = (
            day_amount
            if period == "day"
            else _usage_totals(db, row.id, since=_period_start(current, period))[1]
        )
        if used_amount + amount > Decimal(str(limits[field])):
            return f"{field}_exceeded"
    return None


def authorize_within_envelope(
    db: Session,
    *,
    action: str,
    context: dict[str, Any],
    idempotency_key: str,
    actor: str,
    task_id: int | None,
    consume: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    if action not in AUTO_WITHIN_LIMIT_ACTIONS:
        return {
            "allowed": False,
            "reason": "action_is_not_auto_within_limit",
            "mode": action_policy(action)["mode"],
        }
    if not idempotency_key or len(idempotency_key) > 255:
        return {
            "allowed": False,
            "reason": "autonomy_idempotency_key_required",
            "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
        }
    existing_use = db.scalar(
        select(AuthorityEnvelopeUse).where(
            AuthorityEnvelopeUse.idempotency_key == idempotency_key
        )
    )
    context_digest = authority_context_digest(context)
    if existing_use:
        if (
            existing_use.action != action
            or existing_use.context_digest != context_digest
            or existing_use.task_id != task_id
            or existing_use.actor != actor
        ):
            return {
                "allowed": False,
                "reason": "autonomy_idempotency_conflict",
                "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
            }
        return {
            "allowed": True,
            "reason": "authority_envelope_idempotent_replay",
            "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
            "envelope_id": existing_use.envelope_id,
            "usage_id": existing_use.id,
            "usage_created": False,
        }
    current = now or now_utc()
    rows = list(
        db.scalars(
            select(AuthorityEnvelope)
            .where(
                AuthorityEnvelope.action == action,
                AuthorityEnvelope.status == "active",
                AuthorityEnvelope.starts_at <= current,
                AuthorityEnvelope.expires_at > current,
            )
            .order_by(AuthorityEnvelope.expires_at, AuthorityEnvelope.id)
            .with_for_update()
        ).all()
    )
    failures: list[str] = []
    for row in rows:
        if not _scope_matches(row.scope or {}, context):
            failures.append("scope_mismatch")
            continue
        canonical = _envelope_input(
            envelope_key=row.envelope_key,
            action=row.action,
            scope=row.scope or {},
            limits=row.limits or {},
            starts_at=row.starts_at,
            expires_at=row.expires_at,
            rationale=row.rationale,
        )
        if _digest(canonical) != row.input_hash:
            failures.append("envelope_integrity_failed")
            continue
        failure = _limit_failure(db, row, context=context, current=current)
        if failure:
            failures.append(failure)
            continue
        if not consume:
            return {
                "allowed": True,
                "reason": "authority_envelope_available",
                "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
                "envelope_id": row.id,
                "usage_created": False,
            }
        amount = Decimal(str(context.get("amount", 0) or 0)).quantize(Decimal("0.01"))
        recipients = int(context.get("recipients", 1))
        usage = AuthorityEnvelopeUse(
            envelope_id=row.id,
            task_id=task_id,
            action=action,
            idempotency_key=idempotency_key,
            context_digest=context_digest,
            amount=amount,
            unit_count=recipients,
            actor=actor,
            occurred_at=current,
        )
        try:
            with db.begin_nested():
                db.add(usage)
                db.flush()
        except IntegrityError:
            concurrent = db.scalar(
                select(AuthorityEnvelopeUse).where(
                    AuthorityEnvelopeUse.idempotency_key == idempotency_key
                )
            )
            if (
                concurrent
                and concurrent.action == action
                and concurrent.context_digest == context_digest
                and concurrent.task_id == task_id
                and concurrent.actor == actor
            ):
                return {
                    "allowed": True,
                    "reason": "authority_envelope_idempotent_replay",
                    "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
                    "envelope_id": concurrent.envelope_id,
                    "usage_id": concurrent.id,
                    "usage_created": False,
                }
            return {
                "allowed": False,
                "reason": "autonomy_idempotency_conflict",
                "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
            }
        return {
            "allowed": True,
            "reason": "authority_envelope_authorized",
            "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
            "envelope_id": row.id,
            "usage_id": usage.id,
            "usage_created": True,
        }
    return {
        "allowed": False,
        "reason": failures[0] if failures else "authority_envelope_required",
        "mode": AutonomyMode.AUTO_WITHIN_LIMIT.value,
        "candidate_envelopes": len(rows),
    }
