from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    BusinessRecord,
    TenderDocument,
    TenderPrequalificationSnapshot,
)
from .schemas import TenderPrequalificationCreate, TenderQualificationFact
from .tender_autopilot import EvidenceBindingError


RULES_VERSION = "tender-prequalification-v1"

REQUIRED_CHECKS: dict[str, str] = {
    "required_license": "Обязательная лицензия или её неприменимость",
    "relevant_experience": "Требуемый подтверждённый опыт",
    "allowed_geography": "Допустимая география исполнения",
    "delivery_schedule": "Выполнимый срок поставки или оказания услуг",
    "execution_capacity": "Достаточная производственная мощность",
    "working_capital": "Достаточный доступный оборотный капитал",
    "security_access": "Возможность предоставить требуемое обеспечение",
    "entity_eligibility": "Допустимая субъектность участника",
    "platform_accreditation": "Требуемая аккредитация площадки",
    "national_regime": "Выполнимость требований национального режима",
    "technical_compatibility": "Техническая совместимость предмета закупки",
    "risk_policy": "Соответствие утверждённой политике риска",
    "submission_deadline": "Достаточный срок до подачи заявки",
    "conflict_of_interest": "Отсутствие конфликта интересов",
}


def _evidence_view(fact: TenderQualificationFact) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "document_id": ref.document_id,
                "document_checksum": ref.document_checksum.lower(),
                "locator": ref.locator.strip(),
                "excerpt": ref.excerpt.strip(),
            }
            for ref in fact.evidence
        ),
        key=lambda item: (item["document_id"], item["locator"], item["excerpt"]),
    )


def _canonical_check(fact: TenderQualificationFact) -> dict[str, Any]:
    return {
        "code": fact.code,
        "description": fact.description.strip(),
        "status": fact.status,
        "evidence": _evidence_view(fact),
    }


def _digest(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_checks(
    checks: list[TenderQualificationFact],
    documents: dict[int, TenderDocument],
) -> None:
    codes = [check.code for check in checks]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        raise ValueError(
            f"Duplicate prequalification codes: {', '.join(duplicates)}"
        )
    unsupported = sorted(set(codes) - set(REQUIRED_CHECKS))
    if unsupported:
        raise ValueError(
            f"Unsupported prequalification codes: {', '.join(unsupported)}"
        )
    for check in checks:
        for ref in check.evidence:
            document = documents.get(ref.document_id)
            if document is None:
                raise EvidenceBindingError(
                    f"Document {ref.document_id} does not belong to this tender"
                )
            actual = str(document.checksum or "").lower()
            if not actual or actual != ref.document_checksum.lower():
                raise EvidenceBindingError(
                    f"Checksum mismatch for tender document {ref.document_id}"
                )


def build_tender_prequalification(
    tender: BusinessRecord,
    documents: list[TenderDocument],
    payload: TenderPrequalificationCreate,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate fixed prequalification constraints without guessing missing facts."""

    document_map = {document.id: document for document in documents}
    _validate_checks(payload.checks, document_map)
    canonical = {
        "rules_version": RULES_VERSION,
        "source": {
            "record_id": tender.id,
            "provider": tender.source,
            "external_id": tender.external_id or "",
        },
        "checks": sorted(
            (_canonical_check(check) for check in payload.checks),
            key=lambda item: item["code"],
        ),
    }
    input_hash = _digest(canonical)
    by_code = {check.code: check for check in payload.checks}
    missing_codes = sorted(set(REQUIRED_CHECKS) - set(by_code))
    unknown_codes = sorted(
        code for code, check in by_code.items() if check.status == "unknown"
    )
    evidence_missing_codes = sorted(
        code
        for code, check in by_code.items()
        if check.status != "unknown" and not check.evidence
    )
    failed = sorted(
        (
            check
            for check in payload.checks
            if check.status == "not_satisfied" and check.evidence
        ),
        key=lambda check: check.code,
    )
    hard_stops = [f"qualification:{check.code}:not_satisfied" for check in failed]
    verification_gaps = [
        *(f"qualification:{code}:missing" for code in missing_codes),
        *(f"qualification:{code}:unknown" for code in unknown_codes),
        *(f"qualification:{code}:evidence_missing" for code in evidence_missing_codes),
    ]
    if hard_stops:
        status = "ineligible"
    elif verification_gaps:
        status = "needs_verification"
    else:
        status = "eligible"
    result = {
        "status": status,
        "input_hash": input_hash,
        "rules_version": RULES_VERSION,
        "required_check_codes": sorted(REQUIRED_CHECKS),
        "missing_check_codes": missing_codes,
        "unknown_check_codes": unknown_codes,
        "evidence_missing_codes": evidence_missing_codes,
        "hard_stops": hard_stops,
        "verification_gaps": sorted(verification_gaps),
        "explainable_reasons": [
            {
                "code": check.code,
                "description": check.description.strip(),
                "evidence_document_ids": sorted(
                    {ref.document_id for ref in check.evidence}
                ),
            }
            for check in failed
        ],
        "counts": {
            "required": len(REQUIRED_CHECKS),
            "provided": len(payload.checks),
            "satisfied": sum(
                check.status == "satisfied" for check in payload.checks
            ),
            "not_satisfied": sum(
                check.status == "not_satisfied" for check in payload.checks
            ),
            "unknown": len(unknown_codes),
        },
        "supplier_discovery_allowed": status == "eligible",
        "automatic_participation_allowed": False,
    }
    return canonical, result


def persist_tender_prequalification(
    db: Session,
    tender: BusinessRecord,
    payload: TenderPrequalificationCreate,
    *,
    actor: str,
) -> tuple[TenderPrequalificationSnapshot, bool]:
    documents = list(
        db.scalars(
            select(TenderDocument)
            .where(TenderDocument.record_id == tender.id)
            .order_by(TenderDocument.id)
        ).all()
    )
    canonical, result = build_tender_prequalification(tender, documents, payload)
    existing = db.scalar(
        select(TenderPrequalificationSnapshot).where(
            TenderPrequalificationSnapshot.record_id == tender.id,
            TenderPrequalificationSnapshot.input_hash == result["input_hash"],
        )
    )
    if existing is not None:
        return existing, False
    row = TenderPrequalificationSnapshot(
        record_id=tender.id,
        input_hash=result["input_hash"],
        rules_version=RULES_VERSION,
        status=result["status"],
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
            select(TenderPrequalificationSnapshot).where(
                TenderPrequalificationSnapshot.record_id == tender.id,
                TenderPrequalificationSnapshot.input_hash == result["input_hash"],
            )
        )
        if existing is None:
            raise
        return existing, False
    return row, True


def tender_prequalification_view(
    row: TenderPrequalificationSnapshot,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "record_id": row.record_id,
        "input_hash": row.input_hash,
        "rules_version": row.rules_version,
        "status": row.status,
        "checks": row.input_snapshot.get("checks", []),
        "result": row.result_snapshot,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }
