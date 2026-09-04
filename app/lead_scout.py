from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .llm import llm_advisor
from .models import AuditLog, BusinessRecord


TARGET_REGIONS = {
    "москва": "Москва",
    "московская область": "Московская область",
    "мо": "Московская область",
    "санкт-петербург": "Санкт-Петербург",
    "санкт петербург": "Санкт-Петербург",
    "спб": "Санкт-Петербург",
    "ленинградская область": "Ленинградская область",
    "ло": "Ленинградская область",
}
FREE_MAIL_DOMAINS = {
    "bk.ru",
    "gmail.com",
    "hotmail.com",
    "inbox.ru",
    "list.ru",
    "mail.ru",
    "outlook.com",
    "rambler.ru",
    "ya.ru",
    "yahoo.com",
    "yandex.com",
    "yandex.ru",
}
ROLE_MAILBOXES = {
    "admin",
    "client",
    "clients",
    "commercial",
    "company",
    "contact",
    "hello",
    "info",
    "mail",
    "office",
    "order",
    "reception",
    "sales",
    "secretary",
    "service",
    "support",
    "tender",
    "zakaz",
    "zakupki",
}
EMAIL_PATTERN = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_region(value: object) -> str | None:
    normalized = " ".join(str(value or "").lower().replace("ё", "е").split())
    return TARGET_REGIONS.get(normalized)


def _safe_public_url(value: object) -> str | None:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        return None
    if hostname == "localhost" or hostname.endswith((".local", ".internal", ".localhost")):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return None
    return urlunparse(("https", parsed.netloc.lower(), parsed.path or "/", "", parsed.query, ""))


def _normalized_email(value: object) -> str | None:
    email = str(value or "").strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        return None
    local_part, domain = email.rsplit("@", 1)
    mailbox_role = re.split(r"[+._-]", local_part, maxsplit=1)[0]
    if domain in FREE_MAIL_DOMAINS or mailbox_role not in ROLE_MAILBOXES:
        return None
    return email


def _normalized_phone(value: object) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not 10 <= len(digits) <= 15:
        return None
    return "+" + digits


def _normalized_source_set(citations: list[object]) -> set[str]:
    return {
        url
        for item in citations
        if (url := _safe_public_url(item)) is not None
    }


def _lead_key(name: str, region: str) -> str:
    normalized_name = " ".join(name.lower().replace("ё", "е").split())
    digest = hashlib.sha256(f"{normalized_name}\0{region}".encode()).hexdigest()
    return f"public-lead:{digest}"


def _contact_owners(rows: list[BusinessRecord]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for row in rows:
        data = row.data or {}
        for value in list(data.get("public_emails") or []) + list(data.get("public_phones") or []):
            owners[str(value)] = str(row.external_id or "")
    return owners


def persist_public_business_leads(
    db: Session,
    *,
    provider_result: dict[str, Any],
    allowed_regions: set[str] | None = None,
    max_results: int = 50,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate and persist public organization contacts without granting outreach consent."""

    current = observed_at or _utcnow()
    citations = _normalized_source_set(list(provider_result.get("citations") or []))
    existing_rows = db.scalars(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "lead",
            BusinessRecord.source == "perplexity_public_business_search",
        )
    ).all()
    by_external_id = {str(row.external_id): row for row in existing_rows if row.external_id}
    contact_owners = _contact_owners(existing_rows)
    created = 0
    updated = 0
    rejected: dict[str, int] = {}
    evidence: list[dict[str, Any]] = []

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    candidates = list(provider_result.get("leads") or [])[: max(1, min(max_results, 50))]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            reject("invalid_shape")
            continue
        name = " ".join(str(candidate.get("organization_name") or "").split())[:255]
        region = _normalize_region(candidate.get("region"))
        source_url = _safe_public_url(candidate.get("source_url"))
        if not name:
            reject("missing_organization")
            continue
        if region is None:
            reject("outside_target_regions")
            continue
        if allowed_regions is not None and region not in allowed_regions:
            reject("outside_requested_regions")
            continue
        if source_url is None or source_url not in citations:
            reject("source_not_cited")
            continue
        if candidate.get("contact_scope") != "organization" or bool(candidate.get("contact_person_named")):
            reject("personal_contact")
            continue

        external_id = _lead_key(name, region)
        email = _normalized_email(candidate.get("email"))
        phone = _normalized_phone(candidate.get("phone"))
        if email and contact_owners.get(email, external_id) != external_id:
            email = None
        if phone and contact_owners.get(phone, external_id) != external_id:
            phone = None
        if not email and not phone:
            reject("no_safe_corporate_contact")
            continue

        website = _safe_public_url(candidate.get("website"))
        row = by_external_id.get(external_id)
        old_data = (row.data or {}) if row is not None else {}
        public_emails = sorted(set(old_data.get("public_emails") or []) | ({email} if email else set()))
        public_phones = sorted(set(old_data.get("public_phones") or []) | ({phone} if phone else set()))
        source_urls = sorted(set(old_data.get("source_urls") or []) | {source_url})
        data = {
            "region": region,
            "website": website or old_data.get("website") or "",
            "public_emails": public_emails,
            "public_phones": public_phones,
            "source_urls": source_urls,
            "last_verified_at": current.isoformat(),
            "contact_scope": "organization",
            "outreach_consent": "not_verified",
            "marketing_contact_allowed": False,
            "automatic_outreach": False,
        }
        if row is None:
            row = BusinessRecord(
                record_type="lead",
                external_id=external_id,
                title=name,
                status="researched",
                owner="sales",
                source="perplexity_public_business_search",
                data=data,
            )
            db.add(row)
            db.flush()
            by_external_id[external_id] = row
            created += 1
        else:
            row.title = name
            row.status = "researched"
            row.owner = "sales"
            row.data = data
            updated += 1
        for contact in public_emails + public_phones:
            contact_owners[contact] = external_id
        evidence.append({"record_id": row.id, "region": region, "source_url": source_url})

    db.add(
        AuditLog(
            actor="lead_scout",
            action="public_business_leads.reviewed",
            resource_type="lead_batch",
            resource_id=current.isoformat(),
            details={
                "candidates": len(candidates),
                "created": created,
                "updated": updated,
                "rejected": dict(sorted(rejected.items())),
                "outreach_consent_created": False,
                "outbound_messages_created": False,
            },
        )
    )
    db.flush()
    return {
        "status": "completed",
        "created": created,
        "updated": updated,
        "rejected": dict(sorted(rejected.items())),
        "records": evidence,
        "consent": {
            "status": "not_verified",
            "marketing_contact_allowed": False,
        },
        "external_messages_sent": False,
        "evidence": evidence,
    }


def run_public_lead_scout(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    requested_regions = payload.get("regions") or sorted(set(TARGET_REGIONS.values()))
    if isinstance(requested_regions, str):
        requested_regions = [requested_regions]
    regions = sorted(
        {
            region
            for value in requested_regions
            if (region := _normalize_region(value)) is not None
        }
    )
    if not regions:
        regions = sorted(set(TARGET_REGIONS.values()))
    try:
        max_results = max(1, min(int(payload.get("max_results") or 20), 50))
    except (TypeError, ValueError):
        max_results = 20
    provider_result = llm_advisor.discover_public_business_leads(
        {
            "research_kind": "public_business_lead_discovery",
            "regions": regions,
            "customer_profile": (
                "Организации с объектами, которым потенциально нужны регулярная уборка, "
                "генеральная уборка или обслуживание территории"
            ),
            "max_results": max_results,
            "constraints": {
                "public_business_sources_only": True,
                "organization_contacts_only": True,
                "personal_contacts_forbidden": True,
                "outreach_consent_inference_forbidden": True,
                "automatic_outreach_forbidden": True,
            },
        }
    )
    if provider_result.get("status") != "succeeded":
        return {
            "status": provider_result.get("status") or "unavailable",
            "provider": provider_result.get("provider"),
            "reason": provider_result.get("error") or "Public-source research provider is unavailable",
            "credentials_required": (
                ["PERPLEXITY_API_KEY"]
                if provider_result.get("status") == "credentials_required"
                else []
            ),
            "external_messages_sent": False,
            "evidence": [],
        }
    persisted = persist_public_business_leads(
        db,
        provider_result=provider_result,
        allowed_regions=set(regions),
        max_results=max_results,
    )
    return {
        **persisted,
        "provider": provider_result.get("provider"),
        "model": provider_result.get("model"),
        "prompt": provider_result.get("prompt"),
        "citations_reviewed": len(_normalized_source_set(list(provider_result.get("citations") or []))),
    }
