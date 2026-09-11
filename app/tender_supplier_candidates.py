from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    BusinessRecord,
    TenderPrequalificationSnapshot,
    TenderSupplierCandidateSnapshot,
)
from .schemas import TenderSupplierCandidate, TenderSupplierCandidateSnapshotCreate


RULES_VERSION = "tender-supplier-candidates-v1"
MINIMUM_ELIGIBLE_SUPPLIERS = 2


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Supplier candidate timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Supplier candidate timestamp is invalid") from exc
    return _utc(parsed)


def _text(value: str) -> str:
    return " ".join(value.split())


def _public_https_url(value: str) -> str:
    raw = value.strip()
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Supplier candidate source must be a credential-free HTTPS URL"
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname == "localhost" or hostname.endswith(".local"):
            raise ValueError("Supplier candidate source must use a public host")
    else:
        if any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_multicast,
                address.is_reserved,
                address.is_unspecified,
            )
        ):
            raise ValueError("Supplier candidate source must use a public host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Supplier candidate source port is invalid") from exc
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", "", ""))


def _candidate_input(candidate: TenderSupplierCandidate) -> dict[str, Any]:
    value = {
        "supplier_name": _text(candidate.supplier_name),
        "supplier_identifier": candidate.supplier_identifier.strip(),
        "product_name": _text(candidate.product_name),
        "product_sku": candidate.product_sku.strip(),
        "manufacturer": _text(candidate.manufacturer),
        "country": _text(candidate.country),
        "source_kind": candidate.source_kind,
        "source_url": _public_https_url(candidate.source_url),
        "observed_at": _iso(candidate.observed_at),
        "valid_until": _iso(candidate.valid_until),
        "specification_match": candidate.specification_match,
        "certificate_status": candidate.certificate_status,
        "supplier_reliability": candidate.supplier_reliability,
    }
    return {**value, "candidate_hash": _digest(value)}


def _prequalification_integrity_valid(
    snapshot: TenderPrequalificationSnapshot,
) -> bool:
    source = snapshot.input_snapshot
    result = snapshot.result_snapshot
    return bool(
        isinstance(source, dict)
        and isinstance(result, dict)
        and _digest(source) == snapshot.input_hash
        and result.get("input_hash") == snapshot.input_hash
        and result.get("rules_version") == snapshot.rules_version
        and result.get("status") == snapshot.status
        and result.get("supplier_discovery_allowed") is True
    )


def _candidate_result(candidate: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    hard_stops: list[str] = []
    verification_gaps: list[str] = []
    observed_at = _parse_iso(candidate["observed_at"])
    valid_until = _parse_iso(candidate["valid_until"])
    if valid_until <= observed_at:
        raise ValueError("Supplier candidate validity must end after observation")
    if observed_at > now:
        raise ValueError("Supplier candidate cannot be observed in the future")
    if valid_until <= now:
        verification_gaps.append("source_expired")
    if candidate["specification_match"] == "mismatch":
        hard_stops.append("specification_mismatch")
    elif candidate["specification_match"] == "unknown":
        verification_gaps.append("specification_unknown")
    if candidate["certificate_status"] in {"missing", "unknown"}:
        verification_gaps.append(
            f"certificate_{candidate['certificate_status']}"
        )
    if candidate["supplier_reliability"] == "blocked":
        hard_stops.append("supplier_blocked")
    elif candidate["supplier_reliability"] == "unknown":
        verification_gaps.append("supplier_reliability_unknown")
    if hard_stops:
        status = "rejected"
    elif verification_gaps:
        status = "needs_verification"
    else:
        status = "eligible_for_quote"
    return {
        "candidate_hash": candidate["candidate_hash"],
        "supplier_name": candidate["supplier_name"],
        "supplier_identifier": candidate["supplier_identifier"],
        "status": status,
        "hard_stops": sorted(hard_stops),
        "verification_gaps": sorted(verification_gaps),
    }


def build_supplier_candidate_snapshot(
    tender: BusinessRecord,
    prequalification: TenderPrequalificationSnapshot,
    payload: TenderSupplierCandidateSnapshotCreate,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if prequalification.record_id != tender.id:
        raise ValueError("Prequalification snapshot does not belong to this tender")
    if (
        prequalification.status != "eligible"
        or not _prequalification_integrity_valid(prequalification)
    ):
        raise ValueError(
            "An integrity-valid eligible prequalification is required before "
            "supplier discovery"
        )
    candidates = sorted(
        (_candidate_input(candidate) for candidate in payload.candidates),
        key=lambda item: (
            item["supplier_identifier"],
            item["product_sku"],
            item["source_url"],
        ),
    )
    identifiers = [candidate["supplier_identifier"] for candidate in candidates]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Supplier candidate identifiers must be unique")
    canonical = {
        "rules_version": RULES_VERSION,
        "source": {
            "record_id": tender.id,
            "provider": tender.source,
            "external_id": tender.external_id or "",
        },
        "prequalification_snapshot_hash": prequalification.input_hash,
        "discovery_mode": payload.discovery_mode,
        "candidates": candidates,
    }
    input_hash = _digest(canonical)
    current_time = _utc(now or datetime.now(timezone.utc))
    candidate_results = [
        _candidate_result(candidate, now=current_time) for candidate in candidates
    ]
    eligible_count = sum(
        result["status"] == "eligible_for_quote" for result in candidate_results
    )
    needs_verification_count = sum(
        result["status"] == "needs_verification" for result in candidate_results
    )
    rejected_count = sum(
        result["status"] == "rejected" for result in candidate_results
    )
    if eligible_count >= MINIMUM_ELIGIBLE_SUPPLIERS:
        status = "ready_for_quote_collection"
    elif needs_verification_count:
        status = "needs_verification"
    else:
        status = "rejected"
    result_core = {
        "input_hash": input_hash,
        "rules_version": RULES_VERSION,
        "prequalification_snapshot_hash": prequalification.input_hash,
        "status": status,
        "candidate_count": len(candidates),
        "eligible_candidate_count": eligible_count,
        "needs_verification_candidate_count": needs_verification_count,
        "rejected_candidate_count": rejected_count,
        "minimum_eligible_suppliers_required": MINIMUM_ELIGIBLE_SUPPLIERS,
        "candidate_results": candidate_results,
        "quote_collection_allowed": status == "ready_for_quote_collection",
        "automatic_rfq_allowed": False,
        "automatic_order_allowed": False,
    }
    return canonical, {**result_core, "result_hash": _digest(result_core)}


def supplier_candidate_snapshot_integrity_valid(
    snapshot: TenderSupplierCandidateSnapshot,
) -> bool:
    source = snapshot.input_snapshot
    result = snapshot.result_snapshot
    if not isinstance(source, dict) or not isinstance(result, dict):
        return False
    candidates = source.get("candidates")
    if not isinstance(candidates, list):
        return False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        candidate_hash = candidate.get("candidate_hash")
        core = {key: value for key, value in candidate.items() if key != "candidate_hash"}
        if not isinstance(candidate_hash, str) or _digest(core) != candidate_hash:
            return False
    result_hash = result.get("result_hash")
    result_core = {key: value for key, value in result.items() if key != "result_hash"}
    return bool(
        _digest(source) == snapshot.input_hash
        and isinstance(result_hash, str)
        and _digest(result_core) == result_hash
        and result.get("input_hash") == snapshot.input_hash
        and result.get("rules_version") == snapshot.rules_version
        and result.get("status") == snapshot.status
        and result.get("candidate_count") == snapshot.candidate_count
        and source.get("prequalification_snapshot_hash")
        == snapshot.prequalification_snapshot_hash
        and result.get("prequalification_snapshot_hash")
        == snapshot.prequalification_snapshot_hash
    )


def persist_supplier_candidate_snapshot(
    db: Session,
    tender: BusinessRecord,
    payload: TenderSupplierCandidateSnapshotCreate,
    *,
    actor: str,
    now: datetime | None = None,
) -> tuple[TenderSupplierCandidateSnapshot, bool]:
    prequalification = db.scalar(
        select(TenderPrequalificationSnapshot).where(
            TenderPrequalificationSnapshot.record_id == tender.id,
            TenderPrequalificationSnapshot.input_hash
            == payload.prequalification_snapshot_hash,
        )
    )
    if prequalification is None:
        raise ValueError("Prequalification snapshot is unavailable")
    latest_prequalification = db.scalar(
        select(TenderPrequalificationSnapshot)
        .where(TenderPrequalificationSnapshot.record_id == tender.id)
        .order_by(TenderPrequalificationSnapshot.id.desc())
        .limit(1)
    )
    if latest_prequalification is None or latest_prequalification.id != prequalification.id:
        raise ValueError("Prequalification snapshot is stale")
    canonical, result = build_supplier_candidate_snapshot(
        tender,
        prequalification,
        payload,
        now=now,
    )
    existing = db.scalar(
        select(TenderSupplierCandidateSnapshot).where(
            TenderSupplierCandidateSnapshot.record_id == tender.id,
            TenderSupplierCandidateSnapshot.input_hash == result["input_hash"],
        )
    )
    if existing is not None:
        if not supplier_candidate_snapshot_integrity_valid(existing):
            raise ValueError("Supplier candidate snapshot integrity check failed")
        return existing, False
    row = TenderSupplierCandidateSnapshot(
        record_id=tender.id,
        input_hash=result["input_hash"],
        prequalification_snapshot_hash=prequalification.input_hash,
        rules_version=RULES_VERSION,
        status=result["status"],
        candidate_count=len(canonical["candidates"]),
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
            select(TenderSupplierCandidateSnapshot).where(
                TenderSupplierCandidateSnapshot.record_id == tender.id,
                TenderSupplierCandidateSnapshot.input_hash == result["input_hash"],
            )
        )
        if existing is None:
            raise
        if not supplier_candidate_snapshot_integrity_valid(existing):
            raise ValueError("Supplier candidate snapshot integrity check failed")
        return existing, False
    return row, True


def supplier_candidate_snapshot_view(
    snapshot: TenderSupplierCandidateSnapshot,
) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "record_id": snapshot.record_id,
        "input_hash": snapshot.input_hash,
        "prequalification_snapshot_hash": snapshot.prequalification_snapshot_hash,
        "rules_version": snapshot.rules_version,
        "status": snapshot.status,
        "candidate_count": snapshot.candidate_count,
        "discovery_mode": snapshot.input_snapshot.get("discovery_mode"),
        "candidates": snapshot.input_snapshot.get("candidates", []),
        "result": snapshot.result_snapshot,
        "created_by": snapshot.created_by,
        "created_at": snapshot.created_at,
    }


def validate_quote_candidate_binding(
    db: Session,
    tender: BusinessRecord,
    *,
    candidate_snapshot_hash: str | None,
    prequalification_snapshot_hash: str,
    supplier_name: str,
    supplier_identifier: str,
    now: datetime | None = None,
) -> TenderSupplierCandidateSnapshot | None:
    if candidate_snapshot_hash is None:
        return None
    snapshot = db.scalar(
        select(TenderSupplierCandidateSnapshot).where(
            TenderSupplierCandidateSnapshot.record_id == tender.id,
            TenderSupplierCandidateSnapshot.input_hash == candidate_snapshot_hash,
        )
    )
    if snapshot is None:
        raise ValueError("Supplier candidate snapshot is unavailable")
    latest = db.scalar(
        select(TenderSupplierCandidateSnapshot)
        .where(TenderSupplierCandidateSnapshot.record_id == tender.id)
        .order_by(TenderSupplierCandidateSnapshot.id.desc())
        .limit(1)
    )
    if latest is None or latest.id != snapshot.id:
        raise ValueError("Supplier candidate snapshot is stale")
    latest_prequalification = db.scalar(
        select(TenderPrequalificationSnapshot)
        .where(TenderPrequalificationSnapshot.record_id == tender.id)
        .order_by(TenderPrequalificationSnapshot.id.desc())
        .limit(1)
    )
    if (
        latest_prequalification is None
        or latest_prequalification.input_hash != prequalification_snapshot_hash
    ):
        raise ValueError("Prequalification snapshot is stale")
    if (
        not supplier_candidate_snapshot_integrity_valid(snapshot)
        or snapshot.status != "ready_for_quote_collection"
        or snapshot.prequalification_snapshot_hash
        != prequalification_snapshot_hash
    ):
        raise ValueError("Supplier candidate snapshot is not eligible for quote binding")
    results = snapshot.result_snapshot.get("candidate_results", [])
    eligible_identifiers = {
        result.get("supplier_identifier")
        for result in results
        if isinstance(result, dict) and result.get("status") == "eligible_for_quote"
    }
    current_time = _utc(now or datetime.now(timezone.utc))
    for candidate in snapshot.input_snapshot.get("candidates", []):
        if not isinstance(candidate, dict):
            continue
        if (
            candidate.get("supplier_identifier") == supplier_identifier.strip()
            and str(candidate.get("supplier_name", "")).casefold()
            == _text(supplier_name).casefold()
            and supplier_identifier.strip() in eligible_identifiers
        ):
            if _parse_iso(candidate.get("observed_at")) > current_time:
                raise ValueError("Supplier candidate observation is in the future")
            if _parse_iso(candidate.get("valid_until")) <= current_time:
                raise ValueError("Supplier candidate evidence is stale")
            return snapshot
    raise ValueError("Supplier quote does not match an eligible candidate identity")
