from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    AuditLog,
    BusinessRecord,
    OutreachConsent,
    Suppression,
)
from .notifications import queue_owner_notification


QUALIFICATION_ACTION = "prioritize_owner_review_leads"
QUALIFICATION_RECORD_TYPE = "lead_qualification_digest"
QUALIFICATION_SCHEMA_VERSION = 1
QUALIFICATION_LIMIT = 200
LEGACY_PUBLIC_RESEARCH_SOURCE = "perplexity_public_business_search"


def _qualification_candidate_clause() -> Any:
    return or_(
        BusinessRecord.status == "owner_review",
        and_(
            BusinessRecord.status == "researched",
            BusinessRecord.source == LEGACY_PUBLIC_RESEARCH_SOURCE,
        ),
    )


def qualification_candidate_count(db: Session) -> int:
    """Count owner-review leads plus legacy validated public leads awaiting promotion."""

    return int(
        db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == "lead",
                _qualification_candidate_clause(),
            )
        )
        or 0
    )


def qualification_backlog(db: Session) -> dict[str, Any]:
    """Return a stable key for candidates not yet processed by qualification."""

    rows = db.scalars(
        select(BusinessRecord)
        .where(
            BusinessRecord.record_type == "lead",
            _qualification_candidate_clause(),
        )
        .order_by(BusinessRecord.id)
    ).all()
    lead_ids = [
        row.id
        for row in rows
        if row.status == "researched"
        or not str((row.data or {}).get("qualification_fingerprint") or "")
    ]
    fingerprint = hashlib.sha256(
        ",".join(str(lead_id) for lead_id in lead_ids).encode()
    ).hexdigest()
    return {
        "count": len(lead_ids),
        "fingerprint": fingerprint,
    }


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def _regions() -> list[str]:
    configured = [
        _text(item).casefold()
        for item in settings.management_contact_regions.split("|")
        if _text(item)
    ]
    return configured or ["санкт-петербург", "ленинградская область"]


def _service_area_fit(data: dict[str, Any]) -> bool:
    region = _text(data.get("region") or data.get("city")).casefold()
    if not region:
        return False
    aliases = {
        "спб": "санкт-петербург",
        "ленобласть": "ленинградская область",
        "ло": "ленинградская область",
    }
    normalized = aliases.get(region, region)
    return any(
        re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", normalized)
        is not None
        for candidate in _regions()
    )


def _fresh_verification(data: dict[str, Any], *, current: datetime) -> bool:
    raw = data.get("last_verified_at") or data.get("verified_at")
    if not isinstance(raw, str):
        return False
    try:
        verified_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if verified_at.tzinfo is not None:
        verified_at = verified_at.astimezone(timezone.utc).replace(tzinfo=None)
    return current - timedelta(days=90) <= verified_at <= current + timedelta(minutes=5)


def _truthy_any(data: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(bool(data.get(key)) for key in keys)


def _organization_type(data: dict[str, Any]) -> str:
    value = _text(data.get("organization_type")).casefold().replace("ё", "е")
    aliases = {
        "бизнес-центр": "business_center",
        "бизнес центр": "business_center",
        "склад": "warehouse",
        "торговый центр": "shopping_center",
        "тк": "shopping_center",
        "трк": "shopping_center",
        "трц": "shopping_center",
        "тц": "shopping_center",
        "ук": "management_company",
        "управляющая компания": "management_company",
    }
    return aliases.get(value, value.replace(" ", "_"))


def _approved_contact_path(
    db: Session,
    *,
    lead_id: int,
    emails: list[str],
) -> bool:
    for email in emails:
        normalized = email.strip().lower()
        if not normalized or db.scalar(
            select(Suppression).where(Suppression.address == normalized)
        ) is not None:
            continue
        consent = db.scalar(
            select(OutreachConsent).where(OutreachConsent.address == normalized)
        )
        if (
            consent is not None
            and consent.record_id in {None, lead_id}
            and consent.status == "verified"
            and consent.purpose == "commercial_outreach"
        ):
            return True
    return False


def _qualification_snapshot(
    db: Session,
    lead: BusinessRecord,
    *,
    current: datetime,
) -> dict[str, Any]:
    data = lead.data if isinstance(lead.data, dict) else {}
    emails = sorted({item.lower() for item in _string_list(data.get("public_emails"))})
    phones = sorted(set(_string_list(data.get("public_phones"))))
    source_urls = sorted(set(_string_list(data.get("source_urls"))))
    region_fit = _service_area_fit(data)
    organization_type = _organization_type(data)
    property_fit = organization_type in {
        "business_center",
        "commercial_property",
        "management_company",
        "office",
        "shopping_center",
        "warehouse",
    }
    organization_verified = bool(data.get("verification_reasons")) and bool(source_urls)
    fresh = _fresh_verification(data, current=current)
    organization_channel = bool(emails or phones)
    approved_channel = _approved_contact_path(db, lead_id=lead.id, emails=emails)
    demand_evidence = _truthy_any(
        data,
        ("public_need_evidence", "need_evidence", "cleaning_requirement"),
    )
    scope_evidence = _truthy_any(
        data,
        ("area_m2", "object_area", "service_scope", "facility_type"),
    )
    frequency_evidence = _truthy_any(data, ("frequency", "schedule"))
    timing_evidence = _truthy_any(
        data,
        ("need_by", "procurement_timing", "tender_deadline"),
    )
    buyer_evidence = _truthy_any(
        data,
        ("buyer_role", "decision_maker_role", "procurement_route"),
    )
    economics_evidence = _truthy_any(
        data,
        ("budget", "estimated_monthly_value", "expected_margin", "target_price"),
    )
    score_breakdown = {
        "service_area_fit": 20 if region_fit else 0,
        "property_type_fit": 15 if property_fit else 0,
        "organization_evidence": 15 if organization_verified else 0,
        "evidence_freshness": 10 if fresh else 0,
        "organization_contact": 10 if organization_channel else 0,
        "approved_contact_path": 10 if approved_channel else 0,
        "public_demand_evidence": 10 if demand_evidence else 0,
        "scope_and_frequency": 5 if scope_evidence and frequency_evidence else 0,
        "timing_and_buyer": 3 if timing_evidence and buyer_evidence else 0,
        "economics": 2 if economics_evidence else 0,
    }
    missing_facts: list[str] = []
    checks = (
        (region_fit, "service_area_fit"),
        (property_fit, "property_type"),
        (organization_verified, "organization_identity_verified"),
        (organization_channel, "public_organization_contact"),
        (demand_evidence, "public_evidence_of_need"),
        (scope_evidence, "cleaning_scope_and_area"),
        (frequency_evidence, "cleaning_frequency"),
        (timing_evidence, "procurement_timing"),
        (buyer_evidence, "buyer_role_or_procurement_route"),
        (economics_evidence, "revenue_and_margin_inputs"),
        (approved_channel, "approved_outreach_channel"),
    )
    missing_facts.extend(name for present, name in checks if not present)
    if not region_fit:
        outcome = "reject"
        next_step = "verify_service_area_or_reject"
    elif not organization_verified or not organization_channel:
        outcome = "research"
        next_step = "verify_organization_and_public_contact"
    elif all(
        (
            demand_evidence,
            scope_evidence,
            frequency_evidence,
            timing_evidence,
            buyer_evidence,
            economics_evidence,
            approved_channel,
        )
    ):
        outcome = "sales_ready"
        next_step = "prepare_relevant_owner_approved_contact"
    elif demand_evidence and timing_evidence:
        outcome = "nurture"
        next_step = "complete_scope_economics_and_contact_permission"
    else:
        outcome = "owner_review"
        next_step = "research_need_scope_timing_and_buyer_route"
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "outcome": outcome,
        "score": sum(score_breakdown.values()),
        "score_breakdown": score_breakdown,
        "missing_facts": missing_facts,
        "next_research_step": next_step,
        "responsible_scout": (
            "management_lead_scout"
            if organization_type == "management_company"
            else "commercial_lead_scout"
        ),
        "contact_path": "approved" if approved_channel else "consent_or_lawful_basis_required",
        "evidence": {
            "lead_id": lead.id,
            "source_urls": source_urls[:5],
            "organization_contact_present": organization_channel,
            "verification_fresh": fresh,
        },
        "automatic_outreach": False,
    }


def _fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _priority_key(row: BusinessRecord) -> tuple[int, float, int]:
    qualification = (row.data or {}).get("qualification") or {}
    outcome_order = {
        "sales_ready": 0,
        "owner_review": 1,
        "nurture": 2,
        "research": 3,
        "reject": 4,
    }
    return (
        outcome_order.get(str(qualification.get("outcome") or "research"), 3),
        -float(qualification.get("score") or 0),
        row.id,
    )


def prioritize_owner_review_leads(
    db: Session,
    *,
    current: datetime | None = None,
    notify_owner: bool = True,
) -> dict[str, Any]:
    """Create a durable, evidence-based owner queue without contacting prospects."""

    evaluated_at = current or utcnow()
    if evaluated_at.tzinfo is not None:
        evaluated_at = evaluated_at.astimezone(timezone.utc).replace(tzinfo=None)
    candidate_leads = list(
        db.scalars(
            select(BusinessRecord)
            .where(
                BusinessRecord.record_type == "lead",
                _qualification_candidate_clause(),
            )
            .order_by(BusinessRecord.id.desc())
        ).all()
    )
    leads = sorted(
        candidate_leads,
        key=lambda lead: (
            bool((lead.data or {}).get("qualification_fingerprint")),
            -float(lead.score or 0),
            -lead.id,
        ),
    )[:QUALIFICATION_LIMIT]
    changed_ids: list[int] = []
    unchanged_ids: list[int] = []
    promoted_ids: list[int] = []
    for lead in leads:
        if (
            lead.status == "researched"
            and lead.source == LEGACY_PUBLIC_RESEARCH_SOURCE
        ):
            lead.status = "owner_review"
            promoted_ids.append(lead.id)
        snapshot = _qualification_snapshot(db, lead, current=evaluated_at)
        fingerprint = _fingerprint(snapshot)
        data = lead.data if isinstance(lead.data, dict) else {}
        if data.get("qualification_fingerprint") == fingerprint:
            unchanged_ids.append(lead.id)
            continue
        lead.data = {
            **data,
            "qualification": {
                **snapshot,
                "evaluated_at": evaluated_at.isoformat(),
            },
            "qualification_fingerprint": fingerprint,
            "evidence_gaps": snapshot["missing_facts"],
            "next_action": snapshot["next_research_step"],
            "next_action_at": (evaluated_at + timedelta(hours=24)).isoformat(),
        }
        changed_ids.append(lead.id)

    ranked = sorted(leads, key=_priority_key)
    outcomes = Counter(
        str(((lead.data or {}).get("qualification") or {}).get("outcome") or "research")
        for lead in ranked
    )
    top = ranked[:5]
    batch_fingerprint = hashlib.sha256(
        json.dumps(
            [
                {
                    "lead_id": lead.id,
                    "qualification_fingerprint": (lead.data or {}).get(
                        "qualification_fingerprint"
                    ),
                }
                for lead in ranked
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    day = evaluated_at.date().isoformat()
    external_id = f"lead-qualification:{day}"
    digest = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == QUALIFICATION_RECORD_TYPE,
            BusinessRecord.external_id == external_id,
        )
    )
    digest_created = digest is None
    if digest is None:
        digest = BusinessRecord(
            record_type=QUALIFICATION_RECORD_TYPE,
            external_id=external_id,
            title=f"Очередь квалификации лидов за {day}",
            status="completed",
            owner="sales",
            source="deterministic_lead_qualification",
        )
        db.add(digest)
        db.flush()
    digest.status = "completed"
    digest.score = None
    digest.data = {
        "generated_at": evaluated_at.isoformat(),
        "lead_count": len(leads),
        "changed_count": len(changed_ids),
        "promoted_count": len(promoted_ids),
        "batch_fingerprint": batch_fingerprint,
        "outcomes": dict(sorted(outcomes.items())),
        "priority_lead_ids": [lead.id for lead in top],
        "automatic_outreach": False,
        "external_messages_sent": False,
    }
    notification = None
    if notify_owner:
        names = ", ".join(_text(lead.title)[:80] for lead in top) or "очередь пуста"
        notification = queue_owner_notification(
            db,
            idempotency_key=(
                f"lead-qualification:{day}:{batch_fingerprint[:20]}:telegram"
            ),
            channel="telegram",
            resource_type=QUALIFICATION_RECORD_TYPE,
            resource_id=str(digest.id),
            subject="Очередь квалификации коммерческих лидов",
            body=(
                f"Проверено карточек: {len(leads)}. "
                f"Готовы к продажам по подтверждённым данным: {outcomes.get('sales_ready', 0)}. "
                f"Требуют решения владельца: {outcomes.get('owner_review', 0)}. "
                f"Первые в очереди: {names}. "
                "Для каждой карточки сохранены пробелы и следующий исследовательский шаг. "
                "Сообщения потенциальным заказчикам не отправлялись."
            ),
            data={
                "digest_id": digest.id,
                "lead_count": len(leads),
                "promoted_count": len(promoted_ids),
                "batch_fingerprint": batch_fingerprint,
                "outcomes": dict(sorted(outcomes.items())),
                "priority_lead_ids": [lead.id for lead in top],
                "automatic_outreach": False,
            },
            severity="normal",
            correlation_id=f"lead-qualification:{day}",
        )
    if digest_created or changed_ids or promoted_ids:
        db.add(
            AuditLog(
                actor="sales",
                action="lead.owner_review_prioritized",
                resource_type=QUALIFICATION_RECORD_TYPE,
                resource_id=str(digest.id),
                details={
                    "lead_count": len(leads),
                    "changed_count": len(changed_ids),
                    "unchanged_count": len(unchanged_ids),
                    "promoted_count": len(promoted_ids),
                    "promoted_lead_ids": promoted_ids,
                    "outcomes": dict(sorted(outcomes.items())),
                    "priority_lead_ids": [lead.id for lead in top],
                    "automatic_outreach": False,
                },
            )
        )
    return {
        "status": "completed",
        "lead_count": len(leads),
        "changed_lead_ids": changed_ids,
        "unchanged_lead_ids": unchanged_ids,
        "promoted_lead_ids": promoted_ids,
        "outcomes": dict(sorted(outcomes.items())),
        "priority_lead_ids": [lead.id for lead in top],
        "digest_id": digest.id,
        "notification_id": notification.id if notification is not None else None,
        "automatic_outreach": False,
        "external_messages_sent": False,
        "evidence": [
            {
                "type": "owner_review_qualification_digest",
                "record_id": digest.id,
                "lead_count": len(leads),
                "changed_count": len(changed_ids),
            }
        ],
    }
