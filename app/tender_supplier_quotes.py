from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    BusinessRecord,
    TenderDocument,
    TenderPrequalificationSnapshot,
    TenderSupplierQuoteSnapshot,
)
from .schemas import (
    TenderEvidenceRef,
    TenderSupplierQuoteCreate,
    TenderSupplierQuoteInput,
)
from .tender_supplier_candidates import validate_quote_candidate_binding


RULES_VERSION = "tender-supplier-quote-v1"
MONEY_QUANTUM = Decimal("0.01")
QUANTITY_QUANTUM = Decimal("0.001")


def _money(value: Decimal) -> str:
    return format(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _quantity(value: Decimal) -> str:
    return format(value.quantize(QUANTITY_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _naive_utc(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _evidence_view(ref: TenderEvidenceRef) -> dict[str, Any]:
    return {
        "document_id": ref.document_id,
        "document_checksum": ref.document_checksum.lower(),
        "locator": ref.locator.strip(),
        "excerpt": ref.excerpt.strip(),
    }


def decision_quote_view(quote: TenderSupplierQuoteInput) -> dict[str, Any]:
    """Canonical quote subset accepted by the decision engine."""

    return {
        "supplier_name": quote.supplier_name.strip(),
        "quote_reference": quote.quote_reference.strip(),
        "total_cost": _money(quote.total_cost),
        "currency": quote.currency,
        "vat_included": quote.vat_included,
        "stock_status": quote.stock_status,
        "valid_until": _iso(quote.valid_until),
        "evidence": sorted(
            (_evidence_view(ref) for ref in quote.evidence),
            key=lambda item: (
                item["document_id"],
                item["locator"],
                item["excerpt"],
            ),
        ),
    }


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_evidence(
    evidence: list[TenderEvidenceRef],
    documents: dict[int, TenderDocument],
) -> None:
    for ref in evidence:
        document = documents.get(ref.document_id)
        if document is None:
            raise ValueError(
                f"Document {ref.document_id} does not belong to this tender"
            )
        actual = str(document.checksum or "").lower()
        if not actual or actual != ref.document_checksum.lower():
            raise ValueError(
                f"Checksum mismatch for tender document {ref.document_id}"
            )


def build_supplier_quote_snapshot(
    tender: BusinessRecord,
    prequalification: TenderPrequalificationSnapshot,
    documents: list[TenderDocument],
    payload: TenderSupplierQuoteCreate,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build an evidence-bound quote record; unknown facts fail closed."""

    if prequalification.record_id != tender.id:
        raise ValueError("Prequalification snapshot does not belong to this tender")
    if prequalification.status != "eligible":
        raise ValueError(
            "Prequalification must be eligible before recording supplier quotes"
        )
    document_map = {document.id: document for document in documents}
    _validate_evidence(payload.evidence, document_map)

    quoted_at = _utc(payload.quoted_at)
    valid_until = _utc(payload.valid_until)
    verified_at = _utc(payload.verified_at) if payload.verified_at else None
    if valid_until <= quoted_at:
        raise ValueError("Supplier quote validity must end after quote timestamp")
    if verified_at is not None and verified_at < quoted_at:
        raise ValueError("Supplier quote cannot be verified before it was issued")

    quote = decision_quote_view(payload)
    canonical = {
        "rules_version": RULES_VERSION,
        "source": {
            "record_id": tender.id,
            "provider": tender.source,
            "external_id": tender.external_id or "",
        },
        "prequalification_snapshot_hash": prequalification.input_hash,
        "quote": {
            **quote,
            "supplier_identifier": payload.supplier_identifier.strip(),
            "product_sku": payload.product_sku.strip(),
            "brand": payload.brand.strip(),
            "manufacturer": payload.manufacturer.strip(),
            "country": payload.country.strip(),
            "unit_price": _money(payload.unit_price),
            "minimum_order": _quantity(payload.minimum_order),
            "requested_quantity": _quantity(payload.requested_quantity),
            "quantity_available": (
                _quantity(payload.quantity_available)
                if payload.quantity_available is not None
                else None
            ),
            "stock_location": payload.stock_location.strip(),
            "lead_time_days": payload.lead_time_days,
            "delivery_cost": _money(payload.delivery_cost),
            "delivery_included": payload.delivery_included,
            "payment_terms": payload.payment_terms.strip(),
            "quoted_at": _iso(payload.quoted_at),
            "specification_match": payload.specification_match,
            "certificate_status": payload.certificate_status,
            "supplier_reliability": payload.supplier_reliability,
            "source": payload.source.strip(),
            "verified_at": _iso(payload.verified_at) if payload.verified_at else None,
        },
        "decision_quote": quote,
    }
    if payload.supplier_candidate_snapshot_hash is not None:
        canonical["supplier_candidate_snapshot_hash"] = (
            payload.supplier_candidate_snapshot_hash
        )
    input_hash = _digest(canonical)
    current_time = _utc(now or datetime.now(timezone.utc))
    hard_stops: list[str] = []
    verification_gaps: list[str] = []
    risk_factors: list[str] = []

    if payload.stock_status == "unavailable":
        hard_stops.append("supplier_quote:stock_unavailable")
    elif payload.stock_status == "unknown":
        verification_gaps.append("supplier_quote:stock_unknown")
    if payload.specification_match == "mismatch":
        hard_stops.append("supplier_quote:specification_mismatch")
    elif payload.specification_match == "unknown":
        verification_gaps.append("supplier_quote:specification_unknown")
    if payload.certificate_status in {"missing", "unknown"}:
        verification_gaps.append(
            f"supplier_quote:certificate_{payload.certificate_status}"
        )
    if payload.supplier_reliability == "blocked":
        hard_stops.append("supplier_quote:supplier_blocked")
    elif payload.supplier_reliability == "unknown":
        verification_gaps.append("supplier_quote:supplier_reliability_unknown")
    if payload.quantity_available is None:
        verification_gaps.append("supplier_quote:quantity_available_unknown")
    elif payload.quantity_available < payload.requested_quantity:
        hard_stops.append("supplier_quote:insufficient_quantity")
    if payload.minimum_order > payload.requested_quantity:
        hard_stops.append("supplier_quote:minimum_order_not_met")
    if not payload.stock_location:
        verification_gaps.append("supplier_quote:stock_location_missing")
    if payload.lead_time_days is None:
        verification_gaps.append("supplier_quote:lead_time_unknown")
    if verified_at is None:
        verification_gaps.append("supplier_quote:verification_missing")
    elif verified_at > current_time:
        verification_gaps.append("supplier_quote:verification_in_future")
    if quoted_at > current_time:
        verification_gaps.append("supplier_quote:timestamp_in_future")
    if valid_until <= current_time:
        verification_gaps.append("supplier_quote:expired")
    elif (valid_until - current_time).total_seconds() < 72 * 3600:
        risk_factors.append("supplier_quote:expires_under_72h")

    if hard_stops:
        status = "rejected"
    elif verification_gaps:
        status = "needs_verification"
    else:
        status = "verified"
    result = {
        "status": status,
        "input_hash": input_hash,
        "rules_version": RULES_VERSION,
        "prequalification_snapshot_hash": prequalification.input_hash,
        "hard_stops": sorted(hard_stops),
        "verification_gaps": sorted(verification_gaps),
        "risk_factors": sorted(risk_factors),
        "decision_quote": quote,
        "economics_allowed": status == "verified",
        "rfq_or_order_automatic_execution_allowed": False,
    }
    if payload.supplier_candidate_snapshot_hash is not None:
        result["supplier_candidate_snapshot_hash"] = (
            payload.supplier_candidate_snapshot_hash
        )
    return canonical, result


def persist_supplier_quote_snapshot(
    db: Session,
    tender: BusinessRecord,
    payload: TenderSupplierQuoteCreate,
    *,
    actor: str,
    now: datetime | None = None,
) -> tuple[TenderSupplierQuoteSnapshot, bool]:
    prequalification = db.scalar(
        select(TenderPrequalificationSnapshot).where(
            TenderPrequalificationSnapshot.record_id == tender.id,
            TenderPrequalificationSnapshot.input_hash
            == payload.prequalification_snapshot_hash,
        )
    )
    if prequalification is None:
        raise ValueError("Prequalification snapshot is unavailable")
    validate_quote_candidate_binding(
        db,
        tender,
        candidate_snapshot_hash=payload.supplier_candidate_snapshot_hash,
        prequalification_snapshot_hash=prequalification.input_hash,
        supplier_name=payload.supplier_name,
        supplier_identifier=payload.supplier_identifier,
        now=now,
    )
    documents = list(
        db.scalars(
            select(TenderDocument)
            .where(TenderDocument.record_id == tender.id)
            .order_by(TenderDocument.id)
        ).all()
    )
    canonical, result = build_supplier_quote_snapshot(
        tender,
        prequalification,
        documents,
        payload,
        now=now,
    )
    existing = db.scalar(
        select(TenderSupplierQuoteSnapshot).where(
            TenderSupplierQuoteSnapshot.record_id == tender.id,
            TenderSupplierQuoteSnapshot.input_hash == result["input_hash"],
        )
    )
    if existing is not None:
        return existing, False
    row = TenderSupplierQuoteSnapshot(
        record_id=tender.id,
        input_hash=result["input_hash"],
        prequalification_snapshot_hash=prequalification.input_hash,
        supplier_candidate_snapshot_hash=payload.supplier_candidate_snapshot_hash,
        rules_version=RULES_VERSION,
        status=result["status"],
        supplier_name=payload.supplier_name.strip(),
        supplier_identifier=payload.supplier_identifier.strip(),
        quote_reference=payload.quote_reference.strip(),
        quoted_at=_naive_utc(payload.quoted_at),
        valid_until=_naive_utc(payload.valid_until),
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
            select(TenderSupplierQuoteSnapshot).where(
                TenderSupplierQuoteSnapshot.record_id == tender.id,
                TenderSupplierQuoteSnapshot.input_hash == result["input_hash"],
            )
        )
        if existing is None:
            raise
        return existing, False
    return row, True


def supplier_quote_snapshot_view(
    row: TenderSupplierQuoteSnapshot,
) -> dict[str, Any]:
    return {
        "id": row.id,
        "record_id": row.record_id,
        "input_hash": row.input_hash,
        "prequalification_snapshot_hash": row.prequalification_snapshot_hash,
        "supplier_candidate_snapshot_hash": row.supplier_candidate_snapshot_hash,
        "rules_version": row.rules_version,
        "status": row.status,
        "quote": row.input_snapshot.get("quote", {}),
        "result": row.result_snapshot,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


def supplier_quote_snapshot_integrity_valid(
    row: TenderSupplierQuoteSnapshot,
) -> bool:
    result = row.result_snapshot if isinstance(row.result_snapshot, dict) else {}
    return (
        isinstance(row.input_snapshot, dict)
        and _digest(row.input_snapshot) == row.input_hash
        and result.get("input_hash") == row.input_hash
        and result.get("status") == row.status
        and row.supplier_candidate_snapshot_hash
        == row.input_snapshot.get("supplier_candidate_snapshot_hash")
        and row.supplier_candidate_snapshot_hash
        == result.get("supplier_candidate_snapshot_hash")
    )
