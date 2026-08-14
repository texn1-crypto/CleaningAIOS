from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BusinessRecord, Task, TenderDocument
from .task_state import record_task_created


REQUIRED_FIELDS = (
    "contract_value",
    "contract_months",
    "monthly_payroll",
    "monthly_materials",
    "monthly_logistics",
    "tax_percent",
    "payment_delay_days",
    "available_working_capital",
    "min_margin_percent",
    "company_fit",
    "logistics_fit",
    "staffing_fit",
)

REQUIRED_SOURCE_FIELDS = ("external_id", "source_url", "deadline_at")

FIELD_LABELS = {
    "contract_value": "цена контракта",
    "contract_months": "срок контракта в месяцах",
    "monthly_payroll": "ежемесячный ФОТ с начислениями",
    "monthly_materials": "ежемесячные материалы и расходники",
    "monthly_logistics": "ежемесячная логистика",
    "tax_percent": "эффективная налоговая ставка",
    "payment_delay_days": "отсрочка оплаты",
    "available_working_capital": "доступный оборотный капитал",
    "min_margin_percent": "минимально допустимая маржа",
    "company_fit": "соответствие опыта компании требованиям",
    "logistics_fit": "логистическая реализуемость",
    "staffing_fit": "обеспеченность персоналом",
    "external_id": "идентификатор закупки на площадке",
    "source_url": "ссылка на первичный источник",
    "deadline_at": "срок подачи заявки",
}

CRITICAL_LEGAL_FLAGS = frozenset(
    {
        "document_critical_risk",
        "license_requirement_unmet",
        "experience_requirement_unmet",
        "mandatory_document_unavailable",
        "prohibited_conflict_of_interest",
        "deadline_impossible",
    }
)

HIGH_LEGAL_FLAGS = frozenset(
    {
        "document_high_risk",
        "unlimited_liability",
        "disproportionate_penalties",
        "unclear_scope",
        "unilateral_customer_termination",
        "no_price_indexation",
        "personal_data_risk",
        "subcontracting_restricted",
    }
)

CLEANING_PATTERNS = (
    r"\bклининг\w*\b",
    r"\bуборк\w*\b",
    r"\bмойк\w*\s+(?:окон|остеклен|фасад)",
    r"\bсанитарн\w*\s+содержан\w*\b",
    r"\bсодержан\w*\s+территор\w*\b",
    r"\bjanitorial\b",
    r"\bcleaning\b",
)

TARGET_REGION_PATTERNS = (
    r"\bсанкт[- ]?петербург\w*\b",
    r"\bспб\b",
    r"\bленинградск\w*\s+област\w*\b",
    r"\bленобласт\w*\b",
)

TERMINAL_TENDER_STATUSES = frozenset({"submitted", "won", "lost", "expired", "cancelled"})


def screening_record_status(evaluation_status: str) -> str:
    if evaluation_status == "expired":
        return "expired"
    if evaluation_status == "data_required":
        return "data_required"
    return "screened"


def classify_tender_scope(title: str, data: dict[str, Any]) -> dict[str, Any]:
    text_parts = [
        title,
        str(data.get("description") or ""),
        str(data.get("category") or ""),
        str(data.get("purchase_object") or ""),
    ]
    region_parts = [
        str(data.get("region") or ""),
        str(data.get("place_of_performance") or ""),
        str(data.get("delivery_address") or ""),
        str(data.get("address") or ""),
    ]
    subject_text = " ".join(text_parts).casefold().replace("ё", "е")
    region_text = " ".join(region_parts).casefold().replace("ё", "е")
    cleaning_match = any(re.search(pattern, subject_text) for pattern in CLEANING_PATTERNS)
    region_match = any(re.search(pattern, region_text) for pattern in TARGET_REGION_PATTERNS)
    subject_known = bool(subject_text.strip())
    region_known = bool(region_text.strip())
    if cleaning_match and region_match:
        status = "relevant"
    elif region_known and not region_match:
        status = "out_of_scope_region"
    else:
        status = "scope_review_required"
    return {
        "status": status,
        "cleaning_match": cleaning_match,
        "target_region_match": region_match,
        "subject_evidence_present": subject_known,
        "region_evidence_present": region_known,
    }


def _number(data: dict[str, Any], name: str, default: float = 0.0) -> float:
    value = data.get(name, default)
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _canonical_input(data: dict[str, Any]) -> dict[str, Any]:
    numeric_fields = (
        *REQUIRED_FIELDS,
        "monthly_other_costs",
        "application_security",
        "performance_security",
        "onboarding_costs",
        "contingency_percent",
        "conservative_cost_increase_percent",
        "conservative_revenue_decrease_percent",
    )
    canonical: dict[str, Any] = {
        name: data.get(name) if data.get(name) is None else _number(data, name)
        for name in numeric_fields
    }
    canonical["legal_risk_flags"] = sorted(
        {str(item).strip() for item in (data.get("legal_risk_flags") or []) if str(item).strip()}
    )
    canonical["required_documents"] = sorted(
        {str(item).strip() for item in (data.get("required_documents") or []) if str(item).strip()}
    )
    canonical["scope_assessment"] = data.get("scope_assessment") or None
    canonical["document_analysis_checksums"] = sorted(
        {str(item).strip() for item in (data.get("document_analysis_checksums") or []) if str(item).strip()}
    )
    canonical["deadline_at"] = str(data.get("deadline_at") or "")
    canonical["source_facts"] = {
        name: str(data.get(name) or "")
        for name in (
            "external_id",
            "source_url",
            "platform",
            "title",
            "description",
            "category",
            "purchase_object",
            "region",
            "place_of_performance",
            "delivery_address",
            "address",
        )
    }
    return canonical


def _fingerprint(canonical: dict[str, Any]) -> str:
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scenario(
    *,
    revenue: float,
    direct_costs: float,
    tax_percent: float,
    contingency_percent: float,
) -> dict[str, float]:
    tax = revenue * tax_percent / 100
    contingency = direct_costs * contingency_percent / 100
    total_costs = direct_costs + tax + contingency
    profit = revenue - total_costs
    margin = profit / revenue * 100 if revenue > 0 else -100.0
    return {
        "monthly_revenue": round(revenue, 2),
        "monthly_direct_costs": round(direct_costs, 2),
        "monthly_tax": round(tax, 2),
        "monthly_contingency": round(contingency, 2),
        "monthly_total_costs": round(total_costs, 2),
        "monthly_profit": round(profit, 2),
        "margin_percent": round(margin, 2),
    }


def evaluate_tender_viability(data: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, evidence-first tender participation assessment.

    The function never authorizes participation or submission. Missing source
    facts produce a data request instead of a guessed score.
    """

    canonical = _canonical_input(data)
    fingerprint = _fingerprint(canonical)
    scope = data.get("scope_assessment") if isinstance(data.get("scope_assessment"), dict) else None
    if scope and scope.get("status") != "relevant":
        return {
            "status": str(scope.get("status") or "scope_review_required"),
            "decision": "verify_scope" if scope.get("status") == "scope_review_required" else "skip",
            "score": None,
            "fingerprint": fingerprint,
            "missing_fields": [],
            "hard_stops": [f"scope:{scope.get('status')}"] if scope.get("status") != "scope_review_required" else [],
            "scope_assessment": scope,
            "legal_review_required": False,
            "participation_review_available": False,
            "owner_participation_approval_required": True,
            "separate_submission_approval_required": True,
            "automatic_submission_allowed": False,
            "evidence": [{"type": "tender_scope", **scope}],
        }

    deadline_value = str(data.get("deadline_at") or "").strip()
    if deadline_value:
        try:
            deadline = datetime.fromisoformat(deadline_value.replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline <= datetime.now(timezone.utc):
                return {
                    "status": "expired",
                    "decision": "skip",
                    "score": None,
                    "fingerprint": fingerprint,
                    "missing_fields": [],
                    "hard_stops": ["submission_deadline_passed"],
                    "scope_assessment": scope,
                    "legal_review_required": False,
                    "participation_review_available": False,
                    "owner_participation_approval_required": True,
                    "separate_submission_approval_required": True,
                    "automatic_submission_allowed": False,
                    "evidence": [{"type": "tender_deadline", "deadline_at": deadline.isoformat(), "open": False}],
                }
        except ValueError:
            return {
                "status": "data_required",
                "decision": "correct_invalid_data",
                "score": None,
                "fingerprint": fingerprint,
                "missing_fields": [],
                "invalid_fields": ["deadline_at"],
                "hard_stops": [],
                "scope_assessment": scope,
                "legal_review_required": False,
                "participation_review_available": False,
                "owner_participation_approval_required": True,
                "separate_submission_approval_required": True,
                "automatic_submission_allowed": False,
                "evidence": [{"type": "tender_deadline", "valid": False}],
            }
    missing = [name for name in REQUIRED_FIELDS if data.get(name) is None]
    missing.extend(name for name in REQUIRED_SOURCE_FIELDS if not str(data.get(name) or "").strip())
    if missing:
        return {
            "status": "data_required",
            "decision": "collect_data",
            "score": None,
            "fingerprint": fingerprint,
            "missing_fields": missing,
            "missing_field_labels": [FIELD_LABELS[name] for name in missing],
            "hard_stops": [],
            "legal_review_required": False,
            "participation_review_available": False,
            "owner_participation_approval_required": True,
            "separate_submission_approval_required": True,
            "automatic_submission_allowed": False,
            "evidence": [{"type": "tender_input_completeness", "complete": False, "missing": missing}],
        }

    invalid = []
    for name in REQUIRED_FIELDS:
        value = _number(data, name)
        if name == "contract_months" and value <= 0:
            invalid.append(name)
        elif name not in {"contract_months"} and value < 0:
            invalid.append(name)
        elif name in {"company_fit", "logistics_fit", "staffing_fit"} and not 0 <= value <= 100:
            invalid.append(name)
    if _number(data, "contract_value") <= 0:
        invalid.append("contract_value")
    if invalid:
        invalid = sorted(set(invalid))
        return {
            "status": "data_required",
            "decision": "correct_invalid_data",
            "score": None,
            "fingerprint": fingerprint,
            "missing_fields": [],
            "invalid_fields": invalid,
            "hard_stops": [],
            "legal_review_required": False,
            "participation_review_available": False,
            "owner_participation_approval_required": True,
            "separate_submission_approval_required": True,
            "automatic_submission_allowed": False,
            "evidence": [{"type": "tender_input_validation", "valid": False, "invalid": invalid}],
        }

    contract_value = _number(data, "contract_value")
    contract_months = _number(data, "contract_months")
    monthly_revenue = contract_value / contract_months
    monthly_direct_costs = sum(
        _number(data, name)
        for name in ("monthly_payroll", "monthly_materials", "monthly_logistics", "monthly_other_costs")
    )
    tax_percent = _number(data, "tax_percent")
    contingency_percent = _number(data, "contingency_percent", 5.0)
    conservative_cost_increase = _number(data, "conservative_cost_increase_percent", 15.0)
    conservative_revenue_decrease = _number(data, "conservative_revenue_decrease_percent", 0.0)

    base = _scenario(
        revenue=monthly_revenue,
        direct_costs=monthly_direct_costs,
        tax_percent=tax_percent,
        contingency_percent=contingency_percent,
    )
    conservative = _scenario(
        revenue=monthly_revenue * (1 - conservative_revenue_decrease / 100),
        direct_costs=monthly_direct_costs * (1 + conservative_cost_increase / 100),
        tax_percent=tax_percent,
        contingency_percent=contingency_percent,
    )

    delay_months = math.ceil(_number(data, "payment_delay_days") / 30)
    funding_months = max(1, delay_months)
    securities = _number(data, "application_security") + _number(data, "performance_security")
    working_capital_required = (
        base["monthly_total_costs"] * funding_months
        + securities
        + _number(data, "onboarding_costs")
    )
    available_capital = _number(data, "available_working_capital")
    capital_gap = max(0.0, working_capital_required - available_capital)
    min_margin = _number(data, "min_margin_percent")

    legal_flags = set(canonical["legal_risk_flags"])
    critical_flags = sorted(legal_flags & CRITICAL_LEGAL_FLAGS)
    high_flags = sorted(legal_flags & HIGH_LEGAL_FLAGS)
    unknown_flags = sorted(legal_flags - CRITICAL_LEGAL_FLAGS - HIGH_LEGAL_FLAGS)

    hard_stops: list[str] = []
    if base["monthly_profit"] <= 0:
        hard_stops.append("base_scenario_non_profitable")
    if base["margin_percent"] < min_margin:
        hard_stops.append("margin_below_owner_minimum")
    if conservative["monthly_profit"] <= 0:
        hard_stops.append("conservative_scenario_non_profitable")
    if capital_gap > 0:
        hard_stops.append("working_capital_shortfall")
    hard_stops.extend(f"legal:{flag}" for flag in critical_flags)

    margin_denominator = max(min_margin * 1.5, 1.0)
    margin_score = _clamp(base["margin_percent"] / margin_denominator * 100)
    fit_score = sum(_number(data, name) for name in ("company_fit", "logistics_fit", "staffing_fit")) / 3
    liquidity_score = 100.0 if working_capital_required <= 0 else _clamp(available_capital / working_capital_required * 100)
    legal_score = _clamp(100 - len(high_flags) * 18 - len(unknown_flags) * 8 - len(critical_flags) * 100)
    score = round(margin_score * 0.45 + fit_score * 0.25 + liquidity_score * 0.15 + legal_score * 0.15, 2)

    legal_review_required = bool(high_flags or unknown_flags or critical_flags)
    participation_review_available = not hard_stops and not legal_review_required and score >= 70
    if hard_stops:
        status, decision = "not_viable", "skip"
    elif legal_review_required:
        status, decision = "legal_review_required", "escalate_legal_review"
    elif score < 70:
        status, decision = "owner_risk_review_required", "revise_or_skip"
    else:
        status, decision = "ready_for_owner_review", "consider_participation"

    return {
        "status": status,
        "decision": decision,
        "score": score,
        "fingerprint": fingerprint,
        "missing_fields": [],
        "hard_stops": hard_stops,
        "scope_assessment": scope,
        "scenarios": {"base": base, "conservative": conservative},
        "working_capital": {
            "payment_delay_months": delay_months,
            "funding_months": funding_months,
            "required": round(working_capital_required, 2),
            "available": round(available_capital, 2),
            "gap": round(capital_gap, 2),
        },
        "score_breakdown": {
            "margin": round(margin_score, 2),
            "company_and_delivery_fit": round(fit_score, 2),
            "liquidity": round(liquidity_score, 2),
            "legal": round(legal_score, 2),
        },
        "legal_risks": {"critical": critical_flags, "high": high_flags, "unclassified": unknown_flags},
        "legal_review_required": legal_review_required,
        "participation_review_available": participation_review_available,
        "owner_participation_approval_required": True,
        "separate_submission_approval_required": True,
        "automatic_submission_allowed": False,
        "evidence": [
            {"type": "tender_input_completeness", "complete": True},
            {"type": "tender_economics", "base": base, "conservative": conservative},
            {"type": "tender_working_capital", "required": round(working_capital_required, 2), "gap": round(capital_gap, 2)},
            {"type": "tender_legal_flags", "critical": critical_flags, "high": high_flags, "unclassified": unknown_flags},
        ],
    }


def ensure_participation_review_task(
    db: Session,
    tender: BusinessRecord,
    evaluation: dict[str, Any],
    *,
    actor: str,
) -> Task | None:
    """Idempotently create the first approval gate for a viable tender."""

    if not evaluation.get("participation_review_available"):
        return None
    title = f"Подготовить пакет участия по тендеру #{tender.id}"
    existing_tasks = db.scalars(
        select(Task).where(Task.agent_type == "tender", Task.title == title).order_by(Task.id.desc())
    ).all()
    existing = next(
        (
            task
            for task in existing_tasks
            if (task.payload or {}).get("evaluation_fingerprint") == evaluation["fingerprint"]
            and task.status in {"open", "queued", "running", "blocked", "done"}
        ),
        None,
    )
    if existing:
        return existing
    task = Task(
        title=title,
        description="После решения владельца сверить документы и подготовить пакет для ручной подачи.",
        status="queued",
        priority="high",
        agent_type="tender",
        payload={
            "action": "prepare_tender_package",
            "action_kind": "tender_participation",
            "record_id": tender.id,
            "evaluation_fingerprint": evaluation["fingerprint"],
            "separate_submission_approval_required": True,
        },
        max_attempts=1,
    )
    db.add(task)
    db.flush()
    record_task_created(
        db,
        task,
        actor=actor,
        reason="tender_ready_for_owner_participation_review",
    )
    return task


def merge_registered_document_risks(
    db: Session,
    tender: BusinessRecord,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Bind persisted legal findings to the calculation input and fingerprint."""

    documents = db.scalars(
        select(TenderDocument).where(TenderDocument.record_id == tender.id).order_by(TenderDocument.id)
    ).all()
    source_flags = data.get("source_legal_risk_flags")
    if not isinstance(source_flags, list):
        source_flags = data.get("legal_risk_flags") or []
    legal_flags = {str(flag).strip() for flag in source_flags if str(flag).strip()}
    checksums: list[str] = []
    for document in documents:
        analysis = document.analysis or {}
        risk_level = str(analysis.get("risk_level") or "").strip().casefold()
        if risk_level == "critical":
            legal_flags.add("document_critical_risk")
        elif risk_level == "high":
            legal_flags.add("document_high_risk")
        legal_flags.update(
            str(flag).strip() for flag in (analysis.get("legal_risk_flags") or []) if str(flag).strip()
        )
        analysis_payload = json.dumps(analysis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        analysis_digest = hashlib.sha256(analysis_payload.encode("utf-8")).hexdigest()
        checksums.append(f"{document.id}:{document.checksum or 'no-file'}:{analysis_digest}")
    return {
        **data,
        "source_legal_risk_flags": sorted(
            {str(flag).strip() for flag in source_flags if str(flag).strip()}
        ),
        "legal_risk_flags": sorted(legal_flags),
        "document_analysis_checksums": sorted(checksums),
    }
