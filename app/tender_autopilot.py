from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import BusinessRecord, TenderAssessmentSnapshot, TenderDocument
from .schemas import (
    TenderDecisionSnapshotCreate,
    TenderEvidenceRef,
    TenderQualificationFact,
    TenderRequirementFact,
)


RULES_VERSION = "tender-decision-v2"
MONEY_QUANTUM = Decimal("0.01")
PERCENT_QUANTUM = Decimal("0.01")


class EvidenceBindingError(ValueError):
    """Evidence points outside the tender or no longer matches its document."""


def _money(value: Decimal) -> str:
    return format(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _percent(value: Decimal) -> str:
    return format(value.quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _evidence_view(ref: TenderEvidenceRef) -> dict[str, Any]:
    return {
        "document_id": ref.document_id,
        "document_checksum": ref.document_checksum.lower(),
        "locator": ref.locator.strip(),
        "excerpt": ref.excerpt.strip(),
    }


def _fact_view(fact: TenderRequirementFact | TenderQualificationFact) -> dict[str, Any]:
    result = {
        "code": fact.code,
        "description": fact.description.strip(),
        "status": fact.status,
        "evidence": sorted(
            (_evidence_view(ref) for ref in fact.evidence),
            key=lambda item: (item["document_id"], item["locator"], item["excerpt"]),
        ),
    }
    if isinstance(fact, TenderRequirementFact):
        result["mandatory"] = fact.mandatory
    return result


def _validate_unique_codes(
    facts: Iterable[TenderRequirementFact | TenderQualificationFact],
    *,
    group: str,
) -> None:
    codes: set[str] = set()
    duplicates: set[str] = set()
    for fact in facts:
        if fact.code in codes:
            duplicates.add(fact.code)
        codes.add(fact.code)
    if duplicates:
        raise ValueError(f"Duplicate {group} codes: {', '.join(sorted(duplicates))}")


def _validate_evidence(
    refs: Iterable[TenderEvidenceRef],
    documents: dict[int, TenderDocument],
) -> None:
    for ref in refs:
        document = documents.get(ref.document_id)
        if document is None:
            raise EvidenceBindingError(
                f"Document {ref.document_id} does not belong to this tender"
            )
        actual = str(document.checksum or "").lower()
        expected = ref.document_checksum.lower()
        if not actual or actual != expected:
            raise EvidenceBindingError(
                f"Checksum mismatch for tender document {ref.document_id}"
            )


def _scenario(
    *,
    revenue: Decimal,
    direct_cost: Decimal,
    tax_percent: Decimal,
    contingency_percent: Decimal,
) -> dict[str, str]:
    tax = revenue * tax_percent / Decimal("100")
    contingency = direct_cost * contingency_percent / Decimal("100")
    total_cost = direct_cost + tax + contingency
    profit = revenue - total_cost
    margin = profit / revenue * Decimal("100") if revenue else Decimal("-100")
    return {
        "revenue": _money(revenue),
        "direct_cost": _money(direct_cost),
        "tax": _money(tax),
        "contingency": _money(contingency),
        "total_cost": _money(total_cost),
        "net_profit": _money(profit),
        "margin_percent": _percent(margin),
    }


def _canonical_input(
    tender: BusinessRecord,
    payload: TenderDecisionSnapshotCreate,
) -> dict[str, Any]:
    tender_data = tender.data or {}
    return {
        "rules_version": RULES_VERSION,
        "source": {
            "record_id": tender.id,
            "platform": str(tender_data.get("platform") or tender.source or ""),
            "external_id": str(tender.external_id or tender_data.get("external_id") or ""),
            "source_url": str(tender_data.get("source_url") or ""),
            "deadline_at": _iso(tender.deadline_at) if tender.deadline_at else "",
            "title": tender.title,
        },
        "requirements": sorted(
            (_fact_view(fact) for fact in payload.requirements),
            key=lambda item: item["code"],
        ),
        "qualification_checks": sorted(
            (_fact_view(fact) for fact in payload.qualification_checks),
            key=lambda item: item["code"],
        ),
        "supplier_quote": {
            "supplier_name": payload.supplier_quote.supplier_name.strip(),
            "quote_reference": payload.supplier_quote.quote_reference.strip(),
            "total_cost": _money(payload.supplier_quote.total_cost),
            "currency": payload.supplier_quote.currency,
            "vat_included": payload.supplier_quote.vat_included,
            "stock_status": payload.supplier_quote.stock_status,
            "valid_until": _iso(payload.supplier_quote.valid_until),
            "evidence": sorted(
                (_evidence_view(ref) for ref in payload.supplier_quote.evidence),
                key=lambda item: (item["document_id"], item["locator"], item["excerpt"]),
            ),
        },
        "economics": {
            "contract_value": _money(payload.contract_value),
            "contract_months": payload.contract_months,
            "supplier_cost": _money(payload.supplier_quote.total_cost),
            "payroll_cost": _money(payload.payroll_cost),
            "logistics_cost": _money(payload.logistics_cost),
            "other_direct_cost": _money(payload.other_direct_cost),
            "onboarding_cost": _money(payload.onboarding_cost),
            "application_security": _money(payload.application_security),
            "performance_security": _money(payload.performance_security),
            "available_working_capital": _money(payload.available_working_capital),
            "payment_delay_days": payload.payment_delay_days,
            "tax_percent": _percent(payload.tax_percent),
            "contingency_percent": _percent(payload.contingency_percent),
            "minimum_margin_percent": _percent(payload.minimum_margin_percent),
            "conservative_cost_increase_percent": _percent(
                payload.conservative_cost_increase_percent
            ),
            "conservative_revenue_decrease_percent": _percent(
                payload.conservative_revenue_decrease_percent
            ),
            "auction_expected_discount_percent": _percent(
                payload.auction_expected_discount_percent
            ),
            "maximum_risk_score": payload.maximum_risk_score,
        },
    }


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_tender_assessment(
    tender: BusinessRecord,
    documents: list[TenderDocument],
    payload: TenderDecisionSnapshotCreate,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a deterministic, fail-closed and evidence-bound decision passport."""

    _validate_unique_codes(payload.requirements, group="requirement")
    _validate_unique_codes(payload.qualification_checks, group="qualification")
    document_map = {document.id: document for document in documents}
    for fact in [*payload.requirements, *payload.qualification_checks]:
        _validate_evidence(fact.evidence, document_map)
    _validate_evidence(payload.supplier_quote.evidence, document_map)

    canonical = _canonical_input(tender, payload)
    input_hash = _digest(canonical)
    current_time = _utc(now or datetime.now(timezone.utc))
    source = canonical["source"]

    verification_gaps: list[str] = []
    hard_stops: list[str] = []
    risk_factors: list[str] = []
    for field in ("external_id", "source_url", "deadline_at"):
        if not source[field]:
            verification_gaps.append(f"source.{field}:missing")
    if source["source_url"] and not source["source_url"].startswith("https://"):
        verification_gaps.append("source.source_url:https_required")

    deadline = _utc(tender.deadline_at) if tender.deadline_at else None
    if deadline and deadline <= current_time:
        hard_stops.append("submission_deadline_passed")
    elif deadline and (deadline - current_time).total_seconds() < 72 * 3600:
        risk_factors.append("submission_deadline_under_72h")

    for fact in payload.requirements:
        if fact.mandatory and fact.status == "not_satisfied":
            hard_stops.append(f"requirement:{fact.code}:not_satisfied")
        elif fact.mandatory and fact.status == "unknown":
            verification_gaps.append(f"requirement:{fact.code}:unknown")
        elif not fact.mandatory and fact.status == "unknown":
            risk_factors.append(f"optional_requirement:{fact.code}:unknown")
        if fact.status == "satisfied" and not fact.evidence:
            verification_gaps.append(f"requirement:{fact.code}:evidence_missing")

    for fact in payload.qualification_checks:
        if fact.status == "not_satisfied":
            hard_stops.append(f"qualification:{fact.code}:not_satisfied")
        elif fact.status == "unknown":
            verification_gaps.append(f"qualification:{fact.code}:unknown")
        elif not fact.evidence:
            verification_gaps.append(f"qualification:{fact.code}:evidence_missing")

    quote = payload.supplier_quote
    quote_valid_until = _utc(quote.valid_until)
    if quote.stock_status == "unavailable":
        hard_stops.append("supplier_stock_unavailable")
    elif quote.stock_status == "unknown":
        verification_gaps.append("supplier_stock_unknown")
    if quote_valid_until <= current_time:
        verification_gaps.append("supplier_quote_expired")
    elif (quote_valid_until - current_time).total_seconds() < 72 * 3600:
        risk_factors.append("supplier_quote_expires_under_72h")

    recurring_direct_cost = (
        quote.total_cost
        + payload.payroll_cost
        + payload.logistics_cost
        + payload.other_direct_cost
    )
    direct_cost = recurring_direct_cost + payload.onboarding_cost
    base = _scenario(
        revenue=payload.contract_value,
        direct_cost=direct_cost,
        tax_percent=payload.tax_percent,
        contingency_percent=payload.contingency_percent,
    )
    conservative = _scenario(
        revenue=payload.contract_value
        * (Decimal("1") - payload.conservative_revenue_decrease_percent / Decimal("100")),
        direct_cost=direct_cost
        * (Decimal("1") + payload.conservative_cost_increase_percent / Decimal("100")),
        tax_percent=payload.tax_percent,
        contingency_percent=payload.contingency_percent,
    )

    funding_months = max(1, (payload.payment_delay_days + 29) // 30)
    monthly_direct_cost = recurring_direct_cost / Decimal(payload.contract_months)
    working_capital_required = (
        monthly_direct_cost * Decimal(funding_months)
        + payload.application_security
        + payload.performance_security
        + payload.onboarding_cost
    )
    capital_gap = max(
        Decimal("0"),
        working_capital_required - payload.available_working_capital,
    )

    denominator = Decimal("1") - (
        payload.tax_percent + payload.minimum_margin_percent
    ) / Decimal("100")
    stop_price: Decimal | None = None
    if denominator <= 0:
        hard_stops.append("invalid_profit_policy")
    else:
        stop_price = (
            direct_cost
            * (Decimal("1") + payload.contingency_percent / Decimal("100"))
            / denominator
        )

    auction_expected_bid = payload.contract_value * (
        Decimal("1")
        - payload.auction_expected_discount_percent / Decimal("100")
    )
    auction_expected = _scenario(
        revenue=auction_expected_bid,
        direct_cost=direct_cost,
        tax_percent=payload.tax_percent,
        contingency_percent=payload.contingency_percent,
    )
    maximum_safe_discount_percent: Decimal | None = None
    auction_stop_invariant_holds = False
    if stop_price is not None:
        maximum_safe_discount_percent = max(
            Decimal("0"),
            (payload.contract_value - stop_price)
            / payload.contract_value
            * Decimal("100"),
        )
        auction_stop_invariant_holds = auction_expected_bid >= stop_price

    economic_hard_stops: list[str] = []
    if Decimal(base["net_profit"]) <= 0:
        economic_hard_stops.append("base_scenario_non_profitable")
    if Decimal(base["margin_percent"]) < payload.minimum_margin_percent:
        economic_hard_stops.append("margin_below_owner_minimum")
    if Decimal(conservative["net_profit"]) <= 0:
        economic_hard_stops.append("conservative_scenario_non_profitable")
    if capital_gap > 0:
        economic_hard_stops.append("working_capital_shortfall")
    if stop_price is not None and not auction_stop_invariant_holds:
        economic_hard_stops.append("auction_forecast_below_stop_price")

    economics_valid = not verification_gaps
    if economics_valid:
        hard_stops.extend(economic_hard_stops)

    risk_score = min(
        100,
        10
        + sum(5 for factor in risk_factors if factor.startswith("optional_requirement:"))
        + (15 if "submission_deadline_under_72h" in risk_factors else 0)
        + (10 if "supplier_quote_expires_under_72h" in risk_factors else 0),
    )

    if hard_stops:
        status, recommendation = "not_viable", "skip"
    elif verification_gaps:
        status, recommendation = "needs_verification", "collect_data"
    elif risk_score > payload.maximum_risk_score:
        status, recommendation = "owner_risk_review_required", "revise_or_skip"
    else:
        status, recommendation = "ready_for_owner_review", "consider_participation"

    participation_review_available = status == "ready_for_owner_review"
    economics_input = canonical["economics"]
    referenced_document_ids = sorted(
        {
            ref.document_id
            for fact in [*payload.requirements, *payload.qualification_checks]
            for ref in fact.evidence
        }
        | {ref.document_id for ref in payload.supplier_quote.evidence}
    )
    result = {
        "status": status,
        "recommendation": recommendation,
        "input_hash": input_hash,
        "economics_input_hash": _digest(economics_input),
        "rules_version": RULES_VERSION,
        "source": source,
        "requirements": {
            "total": len(payload.requirements),
            "mandatory": sum(fact.mandatory for fact in payload.requirements),
            "satisfied": sum(fact.status == "satisfied" for fact in payload.requirements),
            "unknown": sum(fact.status == "unknown" for fact in payload.requirements),
            "not_satisfied": sum(
                fact.status == "not_satisfied" for fact in payload.requirements
            ),
        },
        "qualification": {
            "total": len(payload.qualification_checks),
            "satisfied": sum(
                fact.status == "satisfied" for fact in payload.qualification_checks
            ),
            "unknown": sum(
                fact.status == "unknown" for fact in payload.qualification_checks
            ),
            "not_satisfied": sum(
                fact.status == "not_satisfied" for fact in payload.qualification_checks
            ),
        },
        "supplier": {
            "name": quote.supplier_name,
            "quote_reference": quote.quote_reference,
            "total_cost": _money(quote.total_cost),
            "currency": quote.currency,
            "vat_included": quote.vat_included,
            "stock_status": quote.stock_status,
            "valid_until": _iso(quote.valid_until),
        },
        "economics": {
            "valid": economics_valid,
            "currency": "RUB",
            "base": base,
            "conservative": conservative,
            "stop_price": _money(stop_price) if stop_price is not None else None,
            "economic_hard_stops": economic_hard_stops,
            "working_capital": {
                "funding_months": funding_months,
                "required": _money(working_capital_required),
                "available": _money(payload.available_working_capital),
                "gap": _money(capital_gap),
            },
        },
        "auction_forecast": {
            "model": "deterministic_owner_assumption_v1",
            "external_ai_used": False,
            "starting_price": _money(payload.contract_value),
            "expected_discount_percent": _percent(
                payload.auction_expected_discount_percent
            ),
            "expected_bid": _money(auction_expected_bid),
            "expected_economics": auction_expected,
            "stop_price": _money(stop_price) if stop_price is not None else None,
            "maximum_safe_discount_percent": (
                _percent(maximum_safe_discount_percent)
                if maximum_safe_discount_percent is not None
                else None
            ),
            "hard_stop_invariant": "expected_bid_greater_than_or_equal_to_stop_price",
            "hard_stop_invariant_holds": auction_stop_invariant_holds,
            "automatic_bidding_allowed": False,
        },
        "verification_gaps": sorted(set(verification_gaps)),
        "hard_stops": sorted(set(hard_stops)),
        "risk": {
            "score": risk_score,
            "maximum_allowed": payload.maximum_risk_score,
            "factors": sorted(set(risk_factors)),
        },
        "decision_record": {
            "decision": recommendation,
            "alternatives": ["collect_data", "skip", "revise_or_skip", "consider_participation"],
            "key_factors": sorted(
                set(verification_gaps + hard_stops + risk_factors)
            ),
            "evidence_document_ids": referenced_document_ids,
            "rules_version": RULES_VERSION,
        },
        "approval_card": {
            "title": tender.title,
            "expected_net_profit": base["net_profit"],
            "conservative_net_profit": conservative["net_profit"],
            "stop_price": _money(stop_price) if stop_price is not None else None,
            "risk_score": risk_score,
            "capital_required": _money(working_capital_required),
            "supplier": quote.supplier_name,
            "status": status,
            "input_hash": input_hash,
        },
        "participation_review_available": participation_review_available,
        "owner_participation_approval_required": True,
        "separate_submission_approval_required": True,
        "automatic_submission_allowed": False,
    }
    return canonical, result


def persist_tender_assessment(
    db: Session,
    tender: BusinessRecord,
    payload: TenderDecisionSnapshotCreate,
    *,
    actor: str,
    now: datetime | None = None,
) -> tuple[TenderAssessmentSnapshot, bool]:
    documents = db.scalars(
        select(TenderDocument)
        .where(TenderDocument.record_id == tender.id)
        .order_by(TenderDocument.id)
    ).all()
    canonical, result = build_tender_assessment(
        tender,
        list(documents),
        payload,
        now=now,
    )
    existing = db.scalar(
        select(TenderAssessmentSnapshot).where(
            TenderAssessmentSnapshot.record_id == tender.id,
            TenderAssessmentSnapshot.input_hash == result["input_hash"],
        )
    )
    if existing is not None:
        return existing, False
    row = TenderAssessmentSnapshot(
        record_id=tender.id,
        input_hash=result["input_hash"],
        rules_version=RULES_VERSION,
        status=result["status"],
        recommendation=result["recommendation"],
        input_snapshot=canonical,
        result_snapshot=result,
        created_by=actor,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(TenderAssessmentSnapshot).where(
                TenderAssessmentSnapshot.record_id == tender.id,
                TenderAssessmentSnapshot.input_hash == result["input_hash"],
            )
        )
        if existing is None:
            raise
        return existing, False
    return row, True


def tender_assessment_view(row: TenderAssessmentSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "record_id": row.record_id,
        "input_hash": row.input_hash,
        "rules_version": row.rules_version,
        "status": row.status,
        "recommendation": row.recommendation,
        "result": row.result_snapshot,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }
