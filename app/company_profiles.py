from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import CompanyProfileSnapshot, CompanyRequisite
from .schemas import CompanyCapabilityFact, CompanyProfileSnapshotCreate


RULES_VERSION = "company-profile-v1"

REQUIRED_CAPABILITIES: dict[str, str] = {
    "required_license": "Обязательная лицензия или подтверждённая неприменимость",
    "relevant_experience": "Релевантный подтверждённый опыт",
    "geographic_capability": "География исполнения",
    "execution_capacity": "Производственная мощность",
    "working_capital": "Оборотный капитал",
    "security_access": "Доступ к обеспечению заявки или договора",
    "entity_eligibility": "Правоспособность юридического лица",
    "platform_accreditation": "Аккредитация на электронной площадке",
    "risk_policy": "Соответствие внутренней политике риска",
    "conflict_of_interest": "Отсутствие конфликта интересов",
}

PREQUALIFICATION_CAPABILITY_MAP: dict[str, str] = {
    "required_license": "required_license",
    "relevant_experience": "relevant_experience",
    "allowed_geography": "geographic_capability",
    "execution_capacity": "execution_capacity",
    "working_capital": "working_capital",
    "security_access": "security_access",
    "entity_eligibility": "entity_eligibility",
    "platform_accreditation": "platform_accreditation",
    "risk_policy": "risk_policy",
    "conflict_of_interest": "conflict_of_interest",
}

_BANK_FIELDS = (
    "settlement_account",
    "bank_name",
    "bank_inn",
    "bic",
    "correspondent_account",
)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _iso(value: datetime) -> str:
    return _utc_naive(value).isoformat(timespec="seconds") + "Z"


def _decimal(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _digest(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _banking_requisites_hash(requisites: CompanyRequisite) -> str:
    values = {field: str(getattr(requisites, field) or "") for field in _BANK_FIELDS}
    values["currency"] = requisites.currency
    return _digest(values)


def _banking_requisites_present(requisites: CompanyRequisite) -> bool:
    return all(str(getattr(requisites, field) or "").strip() for field in _BANK_FIELDS)


def _masked(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    return "*" * max(0, len(value) - visible) + value[-visible:]


def _canonical_capability(fact: CompanyCapabilityFact) -> dict[str, Any]:
    hashes = [value.lower() for value in fact.evidence_file_hashes]
    if len(hashes) != len(set(hashes)):
        raise ValueError(f"Duplicate evidence file hashes for capability {fact.code}")
    if any(len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value) for value in hashes):
        raise ValueError(f"Invalid evidence file hash for capability {fact.code}")
    return {
        "code": fact.code,
        "description": fact.description.strip(),
        "status": fact.status,
        "evidence_file_hashes": sorted(hashes),
    }


def build_company_profile_snapshot(
    requisites: CompanyRequisite,
    payload: CompanyProfileSnapshotCreate,
) -> tuple[dict[str, Any], dict[str, Any], datetime]:
    """Build a deterministic profile without exposing payment account values."""

    verified_at = _utc_naive(payload.verified_at)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if verified_at > now + timedelta(minutes=5):
        raise ValueError("verified_at cannot be in the future")
    evaluation_date = max(verified_at.date(), now.date()).isoformat()

    document_hashes = [document.file_hash.lower() for document in payload.documents]
    if len(document_hashes) != len(set(document_hashes)):
        raise ValueError("Company profile document file_hash values must be unique")
    documents: list[dict[str, Any]] = []
    for document in payload.documents:
        if not document.issuer.strip():
            raise ValueError(
                f"Company document {document.document_type} issuer cannot be blank"
            )
        if document.expiry_date <= document.issue_date:
            raise ValueError(
                f"Company document {document.document_type} expiry_date must follow issue_date"
            )
        documents.append(
            {
                "document_type": document.document_type,
                "issue_date": document.issue_date.isoformat(),
                "expiry_date": document.expiry_date.isoformat(),
                "issuer": document.issuer.strip(),
                "verification_status": document.verification_status,
                "file_hash": document.file_hash.lower(),
            }
        )
    documents.sort(key=lambda item: (item["document_type"], item["file_hash"]))
    document_by_hash = {item["file_hash"]: item for item in documents}

    codes = [fact.code for fact in payload.capabilities]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        raise ValueError(f"Duplicate company capability codes: {', '.join(duplicates)}")
    unsupported = sorted(set(codes) - set(REQUIRED_CAPABILITIES))
    if unsupported:
        raise ValueError(f"Unsupported company capability codes: {', '.join(unsupported)}")
    capabilities = sorted(
        (_canonical_capability(fact) for fact in payload.capabilities),
        key=lambda item: item["code"],
    )
    if any(not item["description"] for item in capabilities):
        raise ValueError("Company capability description cannot be blank")
    for capability in capabilities:
        for file_hash in capability["evidence_file_hashes"]:
            if file_hash not in document_by_hash:
                raise ValueError(
                    f"Capability {capability['code']} cites an unavailable company document"
                )

    if not payload.taxation_regime.strip():
        raise ValueError("taxation_regime cannot be blank")
    canonical = {
        "rules_version": RULES_VERSION,
        "verified_at": _iso(verified_at),
        "requisites": {
            "profile_id": requisites.id,
            "profile_name": requisites.profile_name,
            "legal_name": requisites.legal_name,
            "inn": requisites.inn,
            "kpp": requisites.kpp,
            "ogrn": requisites.ogrn,
            "legal_address": requisites.legal_address,
            "active": requisites.active,
            "currency": requisites.currency,
            "banking_requisites_present": _banking_requisites_present(requisites),
            "banking_requisites_hash": _banking_requisites_hash(requisites),
        },
        "taxation_regime": payload.taxation_regime.strip(),
        "vat_status": payload.vat_status,
        "categories_allowed": sorted(
            {value.strip() for value in payload.categories_allowed if value.strip()}
        ),
        "geographic_capabilities": sorted(
            {value.strip() for value in payload.geographic_capabilities if value.strip()}
        ),
        "limits": {
            "internal_min_margin_percent": _decimal(payload.internal_min_margin_percent),
            "available_financing": _decimal(payload.available_financing),
            "credit_limit": _decimal(payload.credit_limit),
            "max_exposure": _decimal(payload.max_exposure),
            "working_capital_limit": _decimal(payload.working_capital_limit),
            "currency": requisites.currency,
        },
        "risk_flags": sorted({value.strip() for value in payload.risk_flags if value.strip()}),
        "documents": documents,
        "capabilities": capabilities,
    }
    input_hash = _digest(canonical)

    missing_identity = sorted(
        name
        for name, value in {
            "kpp": requisites.kpp,
            "ogrn": requisites.ogrn,
            "legal_address": requisites.legal_address,
        }.items()
        if not str(value or "").strip()
    )
    missing_profile_fields = sorted(
        name
        for name, value in {
            "banking_requisites": canonical["requisites"]["banking_requisites_present"],
            "categories_allowed": bool(canonical["categories_allowed"]),
            "geographic_capabilities": bool(canonical["geographic_capabilities"]),
            **{
                name: value is not None
                for name, value in canonical["limits"].items()
                if name != "currency"
            },
        }.items()
        if not value
    )
    missing_capabilities = sorted(set(REQUIRED_CAPABILITIES) - set(codes))
    unknown_capabilities = sorted(
        item["code"] for item in capabilities if item["status"] == "unknown"
    )
    failed_capabilities = sorted(
        item["code"] for item in capabilities if item["status"] == "not_satisfied"
    )
    evidence_missing = sorted(
        item["code"]
        for item in capabilities
        if item["status"] != "unknown" and not item["evidence_file_hashes"]
    )
    invalid_documents = sorted(
        item["document_type"]
        for item in documents
        if item["verification_status"] == "invalid"
    )
    unknown_documents = sorted(
        item["document_type"]
        for item in documents
        if item["verification_status"] == "unknown"
    )
    expired_documents = sorted(
        item["document_type"]
        for item in documents
        if item["expiry_date"] < evaluation_date
    )
    not_yet_valid_documents = sorted(
        item["document_type"]
        for item in documents
        if item["issue_date"] > verified_at.date().isoformat()
    )
    unverified_evidence = sorted(
        item["code"]
        for item in capabilities
        if any(
            document_by_hash[file_hash]["verification_status"] != "verified"
            or document_by_hash[file_hash]["expiry_date"] < evaluation_date
            or document_by_hash[file_hash]["issue_date"] > verified_at.date().isoformat()
            for file_hash in item["evidence_file_hashes"]
        )
    )
    verification_gaps = [
        *(f"identity:{name}:missing" for name in missing_identity),
        *(f"profile:{name}:missing" for name in missing_profile_fields),
        *(f"capability:{code}:missing" for code in missing_capabilities),
        *(f"capability:{code}:unknown" for code in unknown_capabilities),
        *(f"capability:{code}:evidence_missing" for code in evidence_missing),
        *(f"capability:{code}:evidence_unverified" for code in unverified_evidence),
        *(f"document:{code}:unknown" for code in unknown_documents),
        *(f"document:{code}:expired" for code in expired_documents),
        *(f"document:{code}:not_yet_valid" for code in not_yet_valid_documents),
    ]
    if payload.vat_status == "unknown":
        verification_gaps.append("vat_status:unknown")
    verification_gaps.sort()
    hard_stops = [
        *(f"capability:{code}:not_satisfied" for code in failed_capabilities),
        *(f"document:{code}:invalid" for code in invalid_documents),
        *(f"risk:{value}" for value in canonical["risk_flags"]),
    ]
    if not requisites.active:
        hard_stops.append("requisites:inactive")
    hard_stops.sort()
    if hard_stops:
        status = "restricted"
    elif verification_gaps:
        status = "needs_verification"
    else:
        status = "verified"

    valid_through_date = min(document.expiry_date for document in payload.documents)
    valid_through = datetime.combine(valid_through_date, time.max)
    result = {
        "status": status,
        "input_hash": input_hash,
        "rules_version": RULES_VERSION,
        "company_identifier_suffix": requisites.inn[-4:],
        "required_capability_codes": sorted(REQUIRED_CAPABILITIES),
        "missing_capability_codes": missing_capabilities,
        "unknown_capability_codes": unknown_capabilities,
        "failed_capability_codes": failed_capabilities,
        "invalid_documents": invalid_documents,
        "expired_documents": expired_documents,
        "verification_gaps": verification_gaps,
        "hard_stops": hard_stops,
        "valid_through": _iso(valid_through),
        "prequalification_binding_allowed": status == "verified",
        "automatic_external_action_allowed": False,
    }
    return canonical, result, valid_through


def validate_company_profile_snapshot(row: CompanyProfileSnapshot) -> None:
    if _digest(row.input_snapshot) != row.input_hash:
        raise ValueError("Company profile snapshot integrity check failed")
    requisites = row.input_snapshot.get("requisites", {})
    if requisites.get("profile_id") != row.company_requisite_id:
        raise ValueError("Company profile requisites binding is invalid")
    if requisites.get("inn") != row.company_identifier:
        raise ValueError("Company profile legal identifier binding is invalid")
    if row.result_snapshot.get("input_hash") != row.input_hash:
        raise ValueError("Company profile result hash does not match input")
    if row.result_snapshot.get("status") != row.status:
        raise ValueError("Company profile result status does not match snapshot")
    if row.result_snapshot.get("valid_through") != _iso(row.valid_through):
        raise ValueError("Company profile validity binding is invalid")


def persist_company_profile_snapshot(
    db: Session,
    requisites: CompanyRequisite,
    payload: CompanyProfileSnapshotCreate,
    *,
    actor: str,
) -> tuple[CompanyProfileSnapshot, bool]:
    canonical, result, valid_through = build_company_profile_snapshot(
        requisites,
        payload,
    )
    existing = db.scalar(
        select(CompanyProfileSnapshot).where(
            CompanyProfileSnapshot.company_requisite_id == requisites.id,
            CompanyProfileSnapshot.input_hash == result["input_hash"],
        )
    )
    if existing is not None:
        validate_company_profile_snapshot(existing)
        return existing, False
    row = CompanyProfileSnapshot(
        company_requisite_id=requisites.id,
        company_identifier=requisites.inn,
        input_hash=result["input_hash"],
        rules_version=RULES_VERSION,
        status=result["status"],
        verified_at=_utc_naive(payload.verified_at),
        valid_through=valid_through,
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
            select(CompanyProfileSnapshot).where(
                CompanyProfileSnapshot.company_requisite_id == requisites.id,
                CompanyProfileSnapshot.input_hash == result["input_hash"],
            )
        )
        if existing is None:
            raise
        validate_company_profile_snapshot(existing)
        return existing, False
    return row, True


def company_profile_snapshot_view(row: CompanyProfileSnapshot) -> dict[str, Any]:
    profile = deepcopy(row.input_snapshot)
    profile_requisites = profile.get("requisites", {})
    for field in ("inn", "kpp", "ogrn"):
        profile_requisites[field] = _masked(str(profile_requisites.get(field) or ""))
    return {
        "id": row.id,
        "company_requisite_id": row.company_requisite_id,
        "company_identifier": _masked(row.company_identifier),
        "input_hash": row.input_hash,
        "rules_version": row.rules_version,
        "status": row.status,
        "verified_at": row.verified_at,
        "valid_through": row.valid_through,
        "profile": profile,
        "result": row.result_snapshot,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }
