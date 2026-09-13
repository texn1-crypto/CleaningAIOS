from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import BusinessRecord, TenderAssessmentSnapshot, TenderDocument


APPLICATION_MANIFEST_VERSION = "tender-application-evidence-manifest-v1"
APPLICATION_MANIFEST_KIND = "application_evidence_manifest"


class TenderApplicationManifestError(ValueError):
    pass


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _assessment_integrity_valid(snapshot: TenderAssessmentSnapshot) -> bool:
    source = snapshot.input_snapshot
    result = snapshot.result_snapshot
    if not isinstance(source, dict) or not isinstance(result, dict):
        return False
    if _digest(source) != snapshot.input_hash:
        return False
    if (
        result.get("input_hash") != snapshot.input_hash
        or result.get("rules_version") != snapshot.rules_version
        or result.get("status") != snapshot.status
        or result.get("recommendation") != snapshot.recommendation
    ):
        return False
    checklist = result.get("application_checklist")
    approval_card = result.get("approval_card")
    if not isinstance(checklist, dict) or not isinstance(approval_card, dict):
        return False
    checklist_hash = checklist.get("checklist_hash")
    checklist_core = {
        key: value for key, value in checklist.items() if key != "checklist_hash"
    }
    return bool(
        isinstance(checklist_hash, str)
        and re.fullmatch(r"[a-f0-9]{64}", checklist_hash)
        and _digest(checklist_core) == checklist_hash
        and checklist.get("assessment_input_hash") == snapshot.input_hash
        and approval_card.get("input_hash") == snapshot.input_hash
        and approval_card.get("application_checklist_hash") == checklist_hash
        and result.get("automatic_submission_allowed") is False
        and result.get("owner_participation_approval_required") is True
        and result.get("separate_submission_approval_required") is True
    )


def _application_manifest(
    tender: BusinessRecord,
    snapshot: TenderAssessmentSnapshot,
) -> dict[str, Any]:
    if snapshot.record_id != tender.id or not _assessment_integrity_valid(snapshot):
        raise TenderApplicationManifestError(
            "Tender assessment snapshot integrity check failed"
        )
    tender_data = tender.data if isinstance(tender.data, dict) else {}
    if (
        tender_data.get("latest_decision_snapshot_id") != snapshot.id
        or tender_data.get("latest_decision_snapshot_hash") != snapshot.input_hash
    ):
        raise TenderApplicationManifestError("Tender assessment snapshot is stale")

    result = snapshot.result_snapshot
    checklist = result["application_checklist"]
    if (
        snapshot.status != "ready_for_owner_review"
        or snapshot.recommendation != "consider_participation"
        or checklist.get("ready_for_owner_review") is not True
        or checklist.get("data_completeness_percent") != 100
        or checklist.get("blocking_item_codes")
        or checklist.get("verification_item_codes")
        or result.get("hard_stops")
        or result.get("verification_gaps")
    ):
        raise TenderApplicationManifestError(
            "Tender assessment is not ready for an application manifest"
        )

    source = snapshot.input_snapshot
    requirement_provenance = (
        "checksum_bound_manager_review"
        if source.get("requirement_review_hashes")
        else "manager_structured_evidence"
    )
    try:
        return {
            "version": APPLICATION_MANIFEST_VERSION,
            "package_type": "draft_application_evidence_manifest",
            "package_status": "requires_owner_participation_approval",
            "generation": {
                "mode": "deterministic_local",
                "external_ai_used": False,
                "free_form_ai_fields_allowed": False,
            },
            "tender": source["source"],
            "assessment": {
                "snapshot_id": snapshot.id,
                "input_hash": snapshot.input_hash,
                "rules_version": snapshot.rules_version,
                "status": snapshot.status,
                "recommendation": snapshot.recommendation,
                "application_checklist_hash": checklist["checklist_hash"],
            },
            "requirements": {
                "provenance": requirement_provenance,
                "review_hashes": source.get("requirement_review_hashes", []),
                "facts": source["requirements"],
            },
            "qualification_checks": source["qualification_checks"],
            "product_compliance": source["product_compliance"],
            "supplier_quote": source["supplier_quote"],
            "owner_economic_inputs": source["economics"],
            "deterministic_economics": result["economics"],
            "risk": result["risk"],
            "application_checklist": checklist,
            "authorization": {
                "participation_approved": False,
                "submission_approved": False,
                "signature_approved": False,
                "payment_approved": False,
                "automatic_submission_allowed": False,
                "owner_participation_approval_required": True,
                "separate_submission_approval_required": True,
            },
        }
    except (KeyError, TypeError) as exc:
        raise TenderApplicationManifestError(
            "Tender assessment snapshot integrity check failed"
        ) from exc


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _write_once(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise TenderApplicationManifestError(
                "Tender application manifest storage conflict"
            )
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != content:
            raise TenderApplicationManifestError(
                "Tender application manifest storage conflict"
            )
        return
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def verify_application_manifest_artifact(
    document: TenderDocument,
) -> tuple[Path, dict[str, Any]]:
    analysis = document.analysis if isinstance(document.analysis, dict) else {}
    if analysis.get("kind") != APPLICATION_MANIFEST_KIND:
        raise TenderApplicationManifestError(
            "Tender application manifest is unavailable"
        )
    root = Path(settings.document_storage_path).resolve()
    path = Path(document.storage_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TenderApplicationManifestError(
            "Tender application manifest is outside protected storage"
        ) from exc
    if not path.is_file() or path.stat().st_size > settings.max_document_bytes:
        raise TenderApplicationManifestError(
            "Tender application manifest file is unavailable"
        )
    content = path.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    if (
        not re.fullmatch(r"[a-f0-9]{64}", document.checksum or "")
        or checksum != document.checksum
        or analysis.get("manifest_hash") != checksum
    ):
        raise TenderApplicationManifestError(
            "Tender application manifest checksum mismatch"
        )
    try:
        manifest = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TenderApplicationManifestError(
            "Tender application manifest is invalid"
        ) from exc
    assessment = manifest.get("assessment") if isinstance(manifest, dict) else None
    authorization = (
        manifest.get("authorization") if isinstance(manifest, dict) else None
    )
    if (
        not isinstance(assessment, dict)
        or not isinstance(authorization, dict)
        or manifest.get("version") != APPLICATION_MANIFEST_VERSION
        or assessment.get("snapshot_id") != analysis.get("assessment_snapshot_id")
        or assessment.get("input_hash") != analysis.get("assessment_input_hash")
        or assessment.get("application_checklist_hash")
        != analysis.get("application_checklist_hash")
        or authorization.get("automatic_submission_allowed") is not False
    ):
        raise TenderApplicationManifestError(
            "Tender application manifest integrity check failed"
        )
    return path, manifest


def persist_application_manifest(
    db: Session,
    tender: BusinessRecord,
    snapshot: TenderAssessmentSnapshot,
    *,
    actor: str,
) -> tuple[TenderDocument, dict[str, Any], bool]:
    manifest = _application_manifest(tender, snapshot)
    content = _manifest_bytes(manifest)
    if len(content) > settings.max_document_bytes:
        raise TenderApplicationManifestError(
            "Tender application manifest exceeds the size limit"
        )
    manifest_hash = hashlib.sha256(content).hexdigest()
    source_url = f"internal://tender-application-manifest/{snapshot.input_hash}"
    existing = db.scalar(
        select(TenderDocument).where(
            TenderDocument.record_id == tender.id,
            TenderDocument.source_url == source_url,
        )
    )
    if existing is not None:
        _path, stored_manifest = verify_application_manifest_artifact(existing)
        if stored_manifest != manifest:
            raise TenderApplicationManifestError(
                "Tender application manifest integrity check failed"
            )
        return existing, stored_manifest, False

    root = Path(settings.document_storage_path).resolve()
    directory = root / "generated" / "tender-application-manifests" / str(tender.id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot.input_hash}.json"
    _write_once(path, content)
    row = TenderDocument(
        record_id=tender.id,
        name=f"tender-application-manifest-{snapshot.id}.json",
        source_url=source_url,
        content_type="application/json",
        storage_path=str(path),
        checksum=manifest_hash,
        status="generated",
        analysis={
            "kind": APPLICATION_MANIFEST_KIND,
            "manifest_version": APPLICATION_MANIFEST_VERSION,
            "manifest_hash": manifest_hash,
            "assessment_snapshot_id": snapshot.id,
            "assessment_input_hash": snapshot.input_hash,
            "application_checklist_hash": manifest["assessment"][
                "application_checklist_hash"
            ],
            "generation_mode": "deterministic_local",
            "created_by": actor,
            "automatic_submission_allowed": False,
            "owner_participation_approval_required": True,
            "separate_submission_approval_required": True,
        },
        analyzed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(TenderDocument).where(
                TenderDocument.record_id == tender.id,
                TenderDocument.source_url == source_url,
            )
        )
        if existing is None:
            raise
        _path, stored_manifest = verify_application_manifest_artifact(existing)
        if stored_manifest != manifest:
            raise TenderApplicationManifestError(
                "Tender application manifest integrity check failed"
            )
        return existing, stored_manifest, False
    return row, manifest, True


def application_manifest_view(
    document: TenderDocument,
    manifest: dict[str, Any],
    *,
    created: bool,
) -> dict[str, Any]:
    analysis = document.analysis
    return {
        "document_id": document.id,
        "record_id": document.record_id,
        "name": document.name,
        "content_type": document.content_type,
        "status": document.status,
        "manifest_version": analysis["manifest_version"],
        "manifest_hash": analysis["manifest_hash"],
        "assessment_snapshot_id": analysis["assessment_snapshot_id"],
        "assessment_input_hash": analysis["assessment_input_hash"],
        "application_checklist_hash": analysis["application_checklist_hash"],
        "package_status": manifest["package_status"],
        "automatic_submission_allowed": False,
        "owner_participation_approval_required": True,
        "separate_submission_approval_required": True,
        "created": created,
        "created_at": document.created_at,
    }
