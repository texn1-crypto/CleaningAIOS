from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import BusinessRecord, TenderDocument, TenderSourceRun
from .platform import event_bus
from .tender_intelligence import TERMINAL_TENDER_STATUSES, classify_tender_scope, evaluate_tender_viability, screening_record_status


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BLOCKED_HOST_SUFFIXES = (".internal", ".invalid", ".lan", ".local", ".localhost", ".test")
TENDER_FEED_CONTRACT_VERSION = "tender-feed-v1"


def _safe_source_label(source: str) -> str:
    """Return a query/userinfo-free label suitable for receipts and errors."""

    parsed = urlparse(source)
    hostname = (parsed.hostname or "invalid-host").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = 443 if parsed.scheme == "https" else 80
    port_suffix = f":{port}" if port and port != default_port else ""
    path = parsed.path or "/"
    return f"{parsed.scheme.lower()}://{hostname}{port_suffix}{path}"[:1024]


def tender_source_freshness(
    db: Session,
    *,
    sources: list[str] | None = None,
    now: datetime | None = None,
    slo_minutes: int | None = None,
) -> dict[str, Any]:
    """Evaluate configured feed freshness only from durable source-run receipts."""

    configured_sources = sources if sources is not None else [
        value.strip()
        for value in settings.tender_sources.split(",")
        if value.strip()
    ]
    configured_sources = list(dict.fromkeys(configured_sources))
    threshold_minutes = max(
        5,
        min(
            int(
                slo_minutes
                if slo_minutes is not None
                else settings.tender_source_freshness_slo_minutes
            ),
            7 * 24 * 60,
        ),
    )
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if current.tzinfo is not None:
        current = current.astimezone(timezone.utc).replace(tzinfo=None)
    if not configured_sources:
        return {
            "status": "source_configuration_required",
            "slo_minutes": threshold_minutes,
            "configured_sources": 0,
            "fresh_sources": 0,
            "missed_sources": 0,
            "sources": [],
        }

    source_rows: list[dict[str, Any]] = []
    for source in configured_sources:
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        latest = db.scalar(
            select(TenderSourceRun)
            .where(TenderSourceRun.source_hash == source_hash)
            .order_by(TenderSourceRun.id.desc())
        )
        latest_success = db.scalar(
            select(TenderSourceRun)
            .where(
                TenderSourceRun.source_hash == source_hash,
                TenderSourceRun.status == "completed",
            )
            .order_by(TenderSourceRun.id.desc())
        )
        if latest is None:
            source_status = "unobserved"
        elif latest_success is None:
            source_status = "never_succeeded"
        elif latest.status == "failed" and latest.id > latest_success.id:
            source_status = "latest_failed"
        elif latest_success.finished_at < current - timedelta(minutes=threshold_minutes):
            source_status = "stale"
        else:
            source_status = "fresh"
        success_age_minutes = (
            max(
                0,
                int((current - latest_success.finished_at).total_seconds() // 60),
            )
            if latest_success is not None
            else None
        )
        source_rows.append(
            {
                "source_ref": source_hash[:32],
                "source_label": _safe_source_label(source),
                "status": source_status,
                "last_attempt_status": latest.status if latest else None,
                "last_attempt_at": latest.finished_at if latest else None,
                "last_success_at": (
                    latest_success.finished_at if latest_success else None
                ),
                "last_success_age_minutes": success_age_minutes,
            }
        )
    fresh_count = sum(row["status"] == "fresh" for row in source_rows)
    return {
        "status": "fresh" if fresh_count == len(source_rows) else "missed",
        "slo_minutes": threshold_minutes,
        "configured_sources": len(source_rows),
        "fresh_sources": fresh_count,
        "missed_sources": len(source_rows) - fresh_count,
        "sources": source_rows,
    }


def _safe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise HTTPException(422, "Only HTTP(S) source URLs are supported")
    if parsed.username or parsed.password:
        raise HTTPException(422, "Credentials in source URLs are not supported")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(BLOCKED_HOST_SUFFIXES):
        raise HTTPException(422, "Private or local source URLs are not allowed")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise HTTPException(422, f"Source hostname cannot be resolved: {hostname}") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise HTTPException(422, "Private or local source URLs are not allowed")


def _safe_get(
    client: httpx.Client,
    url: str,
    *,
    require_https: bool = False,
) -> httpx.Response:
    current_url = url
    for _ in range(6):
        if require_https and urlparse(current_url).scheme != "https":
            raise HTTPException(422, "Authenticated source URLs must use HTTPS")
        _safe_url(current_url)
        response = client.get(current_url)
        if getattr(response, "status_code", 200) not in REDIRECT_STATUSES:
            return response
        location = response.headers.get("location")
        response.close()
        if not location:
            raise HTTPException(502, "Tender source returned a redirect without Location")
        current_url = urljoin(current_url, location)
    raise HTTPException(502, "Tender source has too many redirects")


def _tender_feed_version(
    source: str,
    item: dict[str, Any],
    *,
    external_id: str,
    title: str,
) -> tuple[str, str, dict[str, Any]]:
    """Return a stable provider-item hash and a safe persisted source snapshot."""

    raw_data = item.get("data") or {}
    if not isinstance(raw_data, dict):
        raise ValueError("feed item data must be an object")
    raw_documents = item.get("documents") or []
    if not isinstance(raw_documents, list) or any(
        not isinstance(document, dict) for document in raw_documents
    ):
        raise ValueError("feed item documents must be a list of objects")
    documents = sorted(
        (
            {
                "name": str(document.get("name") or "document")[:255],
                "url": str(document.get("url") or "")[:1024],
                "content_type": str(
                    document.get("content_type") or "application/octet-stream"
                )[:128],
            }
            for document in raw_documents
        ),
        key=lambda document: (
            document["url"],
            document["name"],
            document["content_type"],
        ),
    )
    provider_data = {
        **raw_data,
        **{
            key: value
            for key, value in item.items()
            if key
            not in {
                "id",
                "external_id",
                "title",
                "name",
                "deadline_at",
                "data",
                "documents",
            }
        },
    }
    source_revision = ""
    for key in ("amendment_id", "revision", "version", "updated_at"):
        candidate = item.get(key) or provider_data.get(key)
        if candidate not in (None, ""):
            source_revision = str(candidate)[:255]
            break
    snapshot = {
        "contract_version": TENDER_FEED_CONTRACT_VERSION,
        "source": source,
        "external_id": external_id,
        "title": title,
        "deadline_at": str(item.get("deadline_at") or ""),
        "data": provider_data,
        "documents": documents,
    }
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), source_revision, snapshot


def collect_tenders(db: Session, sources: list[str] | None = None) -> dict[str, Any]:
    sources = sources if sources is not None else [x.strip() for x in settings.tender_sources.split(",") if x.strip()]
    if not sources:
        return {
            "status": "source_configuration_required",
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "errors": [],
        }
    headers = {"Authorization": f"Bearer {settings.tender_source_token}"} if settings.tender_source_token else {}
    created = updated = unchanged = 0
    errors = []
    with httpx.Client(timeout=settings.tender_request_timeout_seconds, follow_redirects=False, headers=headers) as client:
        for source in sources:
            source_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            created_before = created
            updated_before = updated
            unchanged_before = unchanged
            items_seen = 0
            http_status: int | None = None
            receipt_status = "completed"
            error_type = ""
            source_savepoint = db.begin_nested()
            try:
                _safe_url(source)
                response = _safe_get(
                    client,
                    source,
                    require_https=bool(headers),
                )
                http_status = int(getattr(response, "status_code", 200))
                response.raise_for_status()
                body = response.json(); items = body.get("items", []) if isinstance(body, dict) else body
                if not isinstance(items, list): raise ValueError("feed must return a list or {items: [...]} object")
                items_seen = len(items)
                for item in items:
                    if not isinstance(item, dict): continue
                    external_id = str(item.get("external_id") or item.get("id") or "").strip()
                    title = str(item.get("title") or item.get("name") or "").strip()
                    if not external_id or not title: continue
                    version_hash, source_revision, provider_snapshot = _tender_feed_version(
                        source,
                        item,
                        external_id=external_id,
                        title=title,
                    )
                    row = db.scalar(
                        select(BusinessRecord).where(
                            BusinessRecord.record_type == "tender",
                            BusinessRecord.source == source,
                            BusinessRecord.external_id == external_id,
                        )
                    )
                    is_new = row is None
                    if row is not None and str(
                        (row.data or {}).get("latest_feed_version_hash") or ""
                    ) == version_hash:
                        unchanged += 1
                        continue
                    deadline = datetime.fromisoformat(str(item["deadline_at"]).replace("Z", "+00:00")).replace(tzinfo=None) if item.get("deadline_at") else None
                    data = provider_snapshot["data"]
                    data = {
                        **data,
                        "source_legal_risk_flags": list(data.get("legal_risk_flags") or []),
                        "external_id": external_id,
                        "source_url": str(data.get("source_url") or data.get("url") or source),
                        "title": title,
                        "deadline_at": deadline.isoformat() if deadline else "",
                        "scope_assessment": classify_tender_scope(title, data),
                    }
                    evaluation = evaluate_tender_viability(data)
                    previous_data = row.data if row is not None and isinstance(row.data, dict) else {}
                    stored_history = previous_data.get("feed_versions") or []
                    if not isinstance(stored_history, list):
                        raise ValueError("stored tender feed history is invalid")
                    history = list(stored_history)
                    history.append(
                        {
                            "version": len(history) + 1,
                            "version_hash": version_hash,
                            "source_revision": source_revision,
                            "observed_at": datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "snapshot": provider_snapshot,
                        }
                    )
                    version_data = {
                        "feed_contract_version": TENDER_FEED_CONTRACT_VERSION,
                        "latest_feed_version_hash": version_hash,
                        "latest_feed_source_revision": source_revision,
                        "feed_versions": history,
                    }
                    if row:
                        row.title = title
                        row.deadline_at = deadline
                        if row.status not in TERMINAL_TENDER_STATUSES:
                            row.status = screening_record_status(evaluation["status"])
                        row.data = {
                            **row.data,
                            **data,
                            "viability_evaluation": evaluation,
                            "score_breakdown": evaluation.get("score_breakdown", {}),
                            "recommendation": evaluation["decision"],
                            **version_data,
                        }
                        row.score = evaluation.get("score")
                        updated += 1
                    else:
                        row = BusinessRecord(
                            record_type="tender",
                            external_id=external_id,
                            title=title,
                            source=source,
                            deadline_at=deadline,
                            status=screening_record_status(evaluation["status"]),
                            data={
                                **data,
                                "viability_evaluation": evaluation,
                                "score_breakdown": evaluation.get("score_breakdown", {}),
                                "recommendation": evaluation["decision"],
                                **version_data,
                            },
                            score=evaluation.get("score"),
                        )
                        db.add(row); db.flush(); created += 1
                    for doc in provider_snapshot["documents"]:
                        url = doc["url"]
                        if url and not db.scalar(select(TenderDocument.id).where(TenderDocument.record_id == row.id, TenderDocument.source_url == url)):
                            db.add(TenderDocument(record_id=row.id, name=doc["name"], source_url=url, content_type=doc["content_type"]))
                    feed_identity_hash = hashlib.sha256(
                        f"{source}:{external_id}".encode("utf-8")
                    ).hexdigest()[:32]
                    event_bus.publish(
                        db,
                        "tender.discovered" if is_new else "tender.updated",
                        "tender",
                        str(row.id),
                        {
                            "external_id": external_id,
                            "score": row.score,
                            "viability_status": evaluation["status"],
                            "feed_version": len(history),
                            "feed_version_hash": version_hash,
                            "source_revision": source_revision,
                        },
                        idempotency_key=(
                            f"tender-feed:{feed_identity_hash}:{len(history)}:"
                            f"{version_hash}"
                        ),
                    )
            except Exception as exc:
                source_savepoint.rollback()
                created = created_before
                updated = updated_before
                unchanged = unchanged_before
                receipt_status = "failed"
                error_type = type(exc).__name__[:128]
                errors.append({
                    "source": _safe_source_label(source),
                    "error": "Tender source collection failed",
                    "error_type": error_type,
                })
            else:
                source_savepoint.commit()
            receipt = TenderSourceRun(
                source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
                source_label=_safe_source_label(source),
                status=receipt_status,
                http_status=http_status,
                items_seen=items_seen,
                created_count=created - created_before,
                updated_count=updated - updated_before,
                unchanged_count=unchanged - unchanged_before,
                error_type=error_type,
                started_at=source_started_at,
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add(receipt)
            db.flush()
            event_bus.publish(
                db,
                "tender.source_collection_completed",
                "tender_source_run",
                str(receipt.id),
                {
                    "source_ref": receipt.source_hash[:32],
                    "status": receipt.status,
                    "http_status": receipt.http_status,
                    "items_seen": receipt.items_seen,
                    "created": receipt.created_count,
                    "updated": receipt.updated_count,
                    "unchanged": receipt.unchanged_count,
                    "error_type": receipt.error_type,
                },
                idempotency_key=f"tender-source-run:{receipt.id}",
            )
    return {
        "status": "completed_with_errors" if errors else "completed",
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "errors": errors,
    }


def download_tender_document(db: Session, document: TenderDocument) -> dict[str, Any]:
    if not document.source_url: raise HTTPException(422, "Document source_url is empty")
    _safe_url(document.source_url)
    try:
        with httpx.Client(timeout=settings.tender_request_timeout_seconds, follow_redirects=False) as client:
            current_url = document.source_url
            content = bytearray()
            response_headers: dict[str, str] = {}
            for _ in range(6):
                _safe_url(current_url)
                with client.stream("GET", current_url) as response:
                    if getattr(response, "status_code", 200) in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise HTTPException(502, "Document source returned a redirect without Location")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    length = int(response.headers.get("content-length", 0) or 0)
                    if length > settings.max_document_bytes: raise HTTPException(413, "Document is too large")
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > settings.max_document_bytes: raise HTTPException(413, "Document is too large")
                    response_headers = dict(response.headers)
                    break
            else:
                raise HTTPException(502, "Document source has too many redirects")
    except httpx.HTTPError as exc:
        document.status = "download_failed"; db.commit()
        raise HTTPException(502, "Document download failed") from exc
    checksum = hashlib.sha256(content).hexdigest()
    analysis = document.analysis if isinstance(document.analysis, dict) else {}
    raw_versions = analysis.get("download_versions") or []
    if not isinstance(raw_versions, list) or any(
        not isinstance(version, dict) for version in raw_versions
    ):
        raise HTTPException(409, "Stored document download history is invalid")
    versions = list(raw_versions)
    existing_version = next(
        (
            version
            for version in versions
            if str(version.get("checksum") or "").lower() == checksum
        ),
        None,
    )
    version_created = existing_version is None
    version_number = (
        len(versions) + 1
        if existing_version is None
        else int(existing_version.get("version") or 0)
    )
    if version_number < 1:
        raise HTTPException(409, "Stored document download version is invalid")

    storage = Path(settings.document_storage_path)
    storage.mkdir(parents=True, exist_ok=True)
    clean_name = (
        re.sub(r"[^A-Za-zА-Яа-я0-9._-]+", "_", document.name).strip("._")
        or "document"
    )[:120]
    target = storage / (
        f"tender-{document.record_id}-doc-{document.id}-{checksum}-{clean_name}"
    )
    if existing_version is not None and str(
        existing_version.get("storage_path") or ""
    ) != str(target):
        raise HTTPException(409, "Stored document download path is invalid")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise HTTPException(409, "Stored document path is not a regular file")
    try:
        with target.open("xb") as output:
            output.write(content)
    except FileExistsError:
        if target.is_symlink() or not target.is_file():
            raise HTTPException(409, "Stored document path is not a regular file")
        stored_checksum = hashlib.sha256(target.read_bytes()).hexdigest()
        if stored_checksum != checksum:
            raise HTTPException(
                409,
                "Stored document bytes do not match the expected checksum",
            )

    content_type = str(
        response_headers.get("content-type") or document.content_type
    )[:128]
    if existing_version is None:
        versions.append(
            {
                "version": version_number,
                "checksum": checksum,
                "storage_path": str(target),
                "bytes": len(content),
                "content_type": content_type,
                "source_url": document.source_url,
                "downloaded_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
    document.analysis = {
        **analysis,
        "download_versions": versions,
        "latest_download_version": version_number,
        "latest_download_checksum": checksum,
    }
    document.storage_path = str(target)
    document.checksum = checksum
    document.content_type = content_type
    document.status = "downloaded"
    event_bus.publish(
        db,
        "tender.document_downloaded",
        "tender",
        str(document.record_id),
        {
            "document_id": document.id,
            "checksum": checksum,
            "bytes": len(content),
            "version": version_number,
            "version_created": version_created,
        },
        idempotency_key=f"tender-document:{document.id}:{checksum}",
    )
    return {
        "id": document.id,
        "status": document.status,
        "storage_path": document.storage_path,
        "checksum": document.checksum,
        "bytes": len(content),
        "version": version_number,
        "version_created": version_created,
    }
