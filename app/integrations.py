from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import BusinessRecord, TenderDocument
from .platform import event_bus
from .tender_intelligence import TERMINAL_TENDER_STATUSES, classify_tender_scope, evaluate_tender_viability, screening_record_status


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
BLOCKED_HOST_SUFFIXES = (".internal", ".invalid", ".lan", ".local", ".localhost", ".test")


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


def _safe_get(client: httpx.Client, url: str) -> httpx.Response:
    current_url = url
    for _ in range(6):
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


def collect_tenders(db: Session, sources: list[str] | None = None) -> dict[str, Any]:
    sources = sources if sources is not None else [x.strip() for x in settings.tender_sources.split(",") if x.strip()]
    if not sources:
        return {"status": "source_configuration_required", "created": 0, "updated": 0, "errors": []}
    headers = {"Authorization": f"Bearer {settings.tender_source_token}"} if settings.tender_source_token else {}
    created = updated = 0
    errors = []
    with httpx.Client(timeout=settings.tender_request_timeout_seconds, follow_redirects=False, headers=headers) as client:
        for source in sources:
            try:
                _safe_url(source)
                response = _safe_get(client, source); response.raise_for_status()
                body = response.json(); items = body.get("items", []) if isinstance(body, dict) else body
                if not isinstance(items, list): raise ValueError("feed must return a list or {items: [...]} object")
                for item in items:
                    if not isinstance(item, dict): continue
                    external_id = str(item.get("external_id") or item.get("id") or "").strip()
                    title = str(item.get("title") or item.get("name") or "").strip()
                    if not external_id or not title: continue
                    row = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == "tender", BusinessRecord.external_id == external_id))
                    is_new = row is None
                    deadline = datetime.fromisoformat(str(item["deadline_at"]).replace("Z", "+00:00")).replace(tzinfo=None) if item.get("deadline_at") else None
                    data = item.get("data", {}) | {key: value for key, value in item.items() if key not in {"id", "external_id", "title", "name", "deadline_at", "data", "documents"}}
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
                            },
                            score=evaluation.get("score"),
                        )
                        db.add(row); db.flush(); created += 1
                    for doc in item.get("documents", []):
                        url = str(doc.get("url") or "")
                        if url and not db.scalar(select(TenderDocument.id).where(TenderDocument.record_id == row.id, TenderDocument.source_url == url)):
                            db.add(TenderDocument(record_id=row.id, name=str(doc.get("name") or "document"), source_url=url, content_type=str(doc.get("content_type") or "application/octet-stream")))
                    event_bus.publish(db, "tender.discovered" if is_new else "tender.updated", "tender", str(row.id), {"external_id": external_id, "score": row.score, "viability_status": evaluation["status"]}, idempotency_key=f"tender-feed:{external_id}:{hashlib.sha256(repr(item).encode()).hexdigest()[:16]}")
            except Exception as exc:
                errors.append({
                    "source": source,
                    "error": "Tender source collection failed",
                    "error_type": type(exc).__name__[:128],
                })
    return {"status": "completed_with_errors" if errors else "completed", "created": created, "updated": updated, "errors": errors}


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
    storage = Path(settings.document_storage_path); storage.mkdir(parents=True, exist_ok=True)
    clean_name = re.sub(r"[^A-Za-zА-Яа-я0-9._-]+", "_", document.name).strip("._") or "document"
    target = storage / f"tender-{document.record_id}-doc-{document.id}-{clean_name}"
    target.write_bytes(content)
    document.storage_path = str(target); document.checksum = hashlib.sha256(content).hexdigest(); document.content_type = response_headers.get("content-type", document.content_type); document.status = "downloaded"
    event_bus.publish(db, "tender.document_downloaded", "tender", str(document.record_id), {"document_id": document.id, "checksum": document.checksum, "bytes": len(content)}, idempotency_key=f"tender-document:{document.id}:{document.checksum}")
    return {"id": document.id, "status": document.status, "storage_path": document.storage_path, "checksum": document.checksum, "bytes": len(content)}
