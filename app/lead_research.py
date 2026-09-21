from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .llm import llm_advisor
from .models import AuditLog, BusinessRecord, Task
from .task_state import record_task_created


LEAD_RESEARCH_ACTION = "research_public_lead_evidence"
LEAD_RESEARCH_OUTCOMES = frozenset({"owner_review", "research"})
LEAD_RESEARCH_FIELDS = frozenset(
    {
        "area_m2",
        "budget",
        "buyer_role",
        "decision_maker_role",
        "estimated_monthly_value",
        "expected_margin",
        "facility_type",
        "frequency",
        "need_by",
        "object_area",
        "procurement_route",
        "procurement_timing",
        "public_need_evidence",
        "service_scope",
        "target_price",
    }
)
_SCOUT_AGENTS = frozenset({"commercial_lead_scout", "management_lead_scout"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _batch_size() -> int:
    return max(1, min(int(settings.lead_research_batch_size), 16))


def _safe_public_url(value: object) -> str | None:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return urlunparse(("https", parsed.netloc.lower(), parsed.path or "/", "", parsed.query, ""))


def _normalized_name(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def _string_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _research_key(lead: BusinessRecord) -> str:
    data = lead.data if isinstance(lead.data, dict) else {}
    qualification = data.get("qualification")
    if not isinstance(qualification, dict):
        qualification = {}
    payload = {
        "lead_id": lead.id,
        "qualification_fingerprint": data.get("qualification_fingerprint"),
        "missing_facts": qualification.get("missing_facts") or [],
        "source_urls": data.get("source_urls") or [],
        "website": data.get("website") or "",
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def schedule_public_lead_research(
    db: Session,
    *,
    current: datetime,
    cycle_key: str,
) -> dict[str, Any]:
    """Queue a bounded, idempotent evidence search for already qualified leads."""

    leads = db.scalars(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "lead",
            BusinessRecord.status == "owner_review",
        )
    ).all()
    eligible: list[tuple[BusinessRecord, dict[str, Any]]] = []
    for lead in leads:
        data = lead.data if isinstance(lead.data, dict) else {}
        qualification = data.get("qualification")
        if not isinstance(qualification, dict):
            continue
        if not str(data.get("qualification_fingerprint") or ""):
            continue
        if str(qualification.get("outcome") or "") not in LEAD_RESEARCH_OUTCOMES:
            continue
        missing = {
            str(item)
            for item in qualification.get("missing_facts") or []
            if str(item)
        }
        if not missing.intersection(
            {
                "buyer_role_or_procurement_route",
                "cleaning_frequency",
                "cleaning_scope_and_area",
                "procurement_timing",
                "public_evidence_of_need",
                "revenue_and_margin_inputs",
            }
        ):
            continue
        eligible.append((lead, qualification))

    eligible.sort(
        key=lambda item: (
            0 if item[1].get("outcome") == "owner_review" else 1,
            -float(item[1].get("score") or 0),
            item[0].id,
        )
    )
    if not settings.perplexity_api_key.strip():
        return {
            "status": "credentials_required" if eligible else "idle",
            "eligible_leads": len(eligible),
            "tasks_created": [],
            "tasks_reused": [],
            "batch_limit": _batch_size(),
            "external_messages_sent": False,
        }
    created: list[int] = []
    reused: list[int] = []
    occupied_slots = 0
    for lead, qualification in eligible:
        if occupied_slots >= _batch_size():
            break
        fingerprint = _research_key(lead)
        title = f"Lead evidence research · {lead.id} · {fingerprint[:16]}"
        existing = db.scalar(select(Task).where(Task.title == title))
        if existing is not None:
            reused.append(existing.id)
            if existing.status in {"open", "queued", "running"}:
                occupied_slots += 1
            continue
        agent_type = str(qualification.get("responsible_scout") or "")
        if agent_type not in _SCOUT_AGENTS:
            agent_type = "commercial_lead_scout"
        task = Task(
            title=title,
            description=(
                "Найти только явно опубликованные доказательства потребности, объёма, "
                "сроков, закупочного маршрута и экономики по конкретной организации."
            ),
            agent_type=agent_type,
            status="queued",
            priority="high",
            max_attempts=2,
            run_after=current,
            due_at=current + timedelta(hours=6),
            payload={
                "action": LEAD_RESEARCH_ACTION,
                "record_id": lead.id,
                "research_fingerprint": fingerprint,
                "cycle_key": cycle_key[:128],
                "automatic_outreach": False,
            },
        )
        db.add(task)
        db.flush()
        record_task_created(
            db,
            task,
            actor="ceo",
            reason="lead_evidence_research_due",
        )
        created.append(task.id)
        occupied_slots += 1
    return {
        "status": "scheduled",
        "eligible_leads": len(eligible),
        "tasks_created": created,
        "tasks_reused": reused,
        "batch_limit": _batch_size(),
        "external_messages_sent": False,
    }


def research_public_lead_evidence(
    db: Session,
    payload: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist only cited public facts for one exact organization lead."""

    current = observed_at or utcnow()
    lead = db.get(BusinessRecord, int(payload.get("record_id") or 0))
    if lead is None or lead.record_type != "lead":
        return {
            "status": "not_found",
            "record_id": payload.get("record_id"),
            "external_messages_sent": False,
            "evidence": [],
        }
    data = lead.data if isinstance(lead.data, dict) else {}
    qualification = data.get("qualification")
    if not isinstance(qualification, dict):
        qualification = {}
    source_urls = sorted(
        {
            url
            for value in [data.get("website"), *_string_values(data.get("source_urls"))]
            if (url := _safe_public_url(value)) is not None
        }
    )
    brief = {
        "research_kind": "public_business_lead_evidence",
        "organization_name": lead.title,
        "region": str(data.get("region") or data.get("city") or "")[:255],
        "organization_type": str(data.get("organization_type") or "")[:100],
        "known_public_sources": source_urls[:10],
        "missing_facts": [str(item)[:100] for item in qualification.get("missing_facts") or []][
            :15
        ],
        "constraints": {
            "explicit_public_facts_only": True,
            "source_url_required_per_fact": True,
            "personal_contacts_forbidden": True,
            "consent_inference_forbidden": True,
            "automatic_outreach_forbidden": True,
        },
    }
    provider_result = llm_advisor.research_public_lead_evidence(brief)
    if provider_result.get("status") != "succeeded":
        return {
            "status": provider_result.get("status") or "unavailable",
            "record_id": lead.id,
            "provider": provider_result.get("provider"),
            "reason": provider_result.get("error") or "Public evidence provider is unavailable",
            "credentials_required": (
                ["PERPLEXITY_API_KEY"]
                if provider_result.get("status") == "credentials_required"
                else []
            ),
            "external_messages_sent": False,
            "evidence": [],
        }
    if _normalized_name(provider_result.get("organization_name")) != _normalized_name(
        lead.title
    ):
        return {
            "status": "not_verified",
            "record_id": lead.id,
            "reason": "organization_name_mismatch",
            "external_messages_sent": False,
            "evidence": [],
        }

    citations = {
        url
        for value in provider_result.get("citations") or []
        if (url := _safe_public_url(value)) is not None
    }
    accepted: list[dict[str, str]] = []
    rejected = 0
    for item in provider_result.get("facts") or []:
        if not isinstance(item, dict):
            rejected += 1
            continue
        field = str(item.get("field") or "")
        value = " ".join(str(item.get("value") or "").split())[:1000]
        source_url = _safe_public_url(item.get("source_url"))
        if field not in LEAD_RESEARCH_FIELDS or not value or source_url not in citations:
            rejected += 1
            continue
        accepted.append({"field": field, "value": value, "source_url": source_url})

    canonical = sorted(accepted, key=lambda item: (item["field"], item["source_url"], item["value"]))
    research_fingerprint = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    previous_fingerprint = str(data.get("lead_research_fingerprint") or "")
    changed = bool(canonical) and research_fingerprint != previous_fingerprint
    merged = dict(data)
    if changed:
        for fact in canonical:
            merged[fact["field"]] = fact["value"]
        merged.pop("qualification_fingerprint", None)
        merged["lead_research_fingerprint"] = research_fingerprint
        merged["lead_research_evidence"] = canonical
        merged["lead_research_verified_at"] = current.isoformat()
        merged["source_urls"] = sorted(
            set(_string_values(data.get("source_urls")))
            | {fact["source_url"] for fact in canonical}
        )
    merged["lead_research_last_attempt_at"] = current.isoformat()
    lead.data = merged
    db.add(
        AuditLog(
            actor="lead_scout",
            action="lead.public_evidence_researched",
            resource_type="lead",
            resource_id=str(lead.id),
            details={
                "changed": changed,
                "accepted_fact_count": len(canonical),
                "rejected_fact_count": rejected,
                "accepted_fields": sorted({item["field"] for item in canonical}),
                "source_urls": sorted({item["source_url"] for item in canonical}),
                "automatic_outreach": False,
            },
        )
    )
    db.flush()
    return {
        "status": "completed",
        "record_id": lead.id,
        "changed": changed,
        "accepted_fact_count": len(canonical),
        "rejected_fact_count": rejected,
        "accepted_fields": sorted({item["field"] for item in canonical}),
        "qualification_recheck_required": changed,
        "external_messages_sent": False,
        "evidence": [
            {
                "type": "public_lead_evidence_research",
                "record_id": lead.id,
                "source_urls": sorted({item["source_url"] for item in canonical}),
                "fact_count": len(canonical),
            }
        ],
    }
