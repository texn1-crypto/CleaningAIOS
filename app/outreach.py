from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from datetime import datetime, timezone
from itertools import cycle
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import OutboundMessage, OutreachConsent, SenderMailbox, Suppression


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def consent_evidence_hash(address: str, purpose: str, source_url: str, evidence: str) -> str:
    normalized = "\n".join((address.lower(), purpose, source_url, evidence.strip()))
    return hashlib.sha256(normalized.encode()).hexdigest()


def upsert_consent(
    db: Session,
    *,
    address: str,
    record_id: int | None,
    status: str,
    purpose: str,
    source_url: str,
    evidence: str,
    actor: str,
) -> OutreachConsent:
    normalized = address.lower()
    row = db.get(OutreachConsent, normalized)
    now = utcnow()
    digest = consent_evidence_hash(normalized, purpose, source_url, evidence)
    if row is None:
        row = OutreachConsent(
            address=normalized,
            record_id=record_id,
            evidence_hash=digest,
            verified_by=actor,
        )
        db.add(row)
    row.record_id = record_id
    row.status = status
    row.purpose = purpose
    row.source_url = source_url
    row.evidence_hash = digest
    row.verified_by = actor
    row.verified_at = now
    row.revoked_at = now if status == "revoked" else None
    if status == "revoked":
        db.merge(Suppression(address=normalized, reason="consent_revoked"))
    db.flush()
    return row


def verified_recipients(db: Session, recipients: list[str]) -> tuple[list[str], list[str]]:
    normalized = sorted(set(address.lower() for address in recipients))
    if not normalized:
        return [], []
    verified = set(
        db.scalars(
            select(OutreachConsent.address).where(
                OutreachConsent.address.in_(normalized),
                OutreachConsent.status == "verified",
                OutreachConsent.purpose == "commercial_outreach",
            )
        ).all()
    )
    return [address for address in normalized if address in verified], [address for address in normalized if address not in verified]


def validate_attachments(attachments: list[dict]) -> list[dict]:
    validated: list[dict] = []
    total = 0
    for item in attachments:
        filename = str(item.get("filename") or "attachment")[:255]
        content_type = str(item.get("content_type") or "application/octet-stream")[:128]
        encoded = str(item.get("content_base64") or "")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"Invalid base64 attachment: {filename}") from exc
        if not raw or len(raw) > settings.max_attachment_bytes:
            raise ValueError(f"Attachment exceeds the safe size limit: {filename}")
        total += len(raw)
        if total > settings.max_attachment_bytes:
            raise ValueError("Combined attachments exceed the safe size limit")
        validated.append({
            "filename": filename,
            "content_type": content_type,
            "content_base64": encoded,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        })
    return validated


def _safe_filename(value: str) -> str:
    name = Path(value).name
    stem = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "-", Path(name).stem).strip(" .-_")[:120] or "attachment"
    return f"{stem}{Path(name).suffix.lower()[:16]}"


def persist_campaign_attachments(campaign_key: str, attachments: list[dict]) -> list[dict]:
    """Store unique payloads once on the web/worker shared protected volume."""
    if attachments and all(item.get("storage_path") for item in attachments):
        root = Path(settings.document_storage_path).resolve()
        persisted: list[dict] = []
        total = 0
        for item in attachments:
            path = Path(str(item["storage_path"])).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError("Attachment path is outside document storage") from exc
            raw = path.read_bytes()
            total += len(raw)
            digest = hashlib.sha256(raw).hexdigest()
            if not raw or len(raw) > settings.max_attachment_bytes or total > settings.max_attachment_bytes:
                raise ValueError("Combined attachments exceed the safe size limit")
            if item.get("sha256") and item["sha256"] != digest:
                raise ValueError("Attachment checksum mismatch")
            persisted.append({
                "filename": _safe_filename(str(item.get("filename") or path.name)),
                "content_type": str(item.get("content_type") or "application/octet-stream")[:128],
                "sha256": digest,
                "size": len(raw),
                "storage_path": str(path),
            })
        return persisted
    checked = validate_attachments(attachments)
    if not checked:
        return []
    directory = Path(settings.document_storage_path) / "outreach"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    persisted: list[dict] = []
    campaign_digest = hashlib.sha256(campaign_key.encode()).hexdigest()[:16]
    for item in checked:
        filename = _safe_filename(item["filename"])
        path = directory / f"{campaign_digest}-{item['sha256'][:16]}-{filename}"
        if not path.exists():
            raw = base64.b64decode(item["content_base64"], validate=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(raw)
            except Exception:
                path.unlink(missing_ok=True)
                raise
        persisted.append({
            "filename": filename,
            "content_type": item["content_type"],
            "sha256": item["sha256"],
            "size": item["size"],
            "storage_path": str(path),
        })
    return persisted


def campaign_approval_payload(
    *,
    recipients: list[str],
    subject: str,
    body: str,
    mailbox_id: int | None,
    template_id: int | None,
    scheduled_at: datetime | None,
    attachments: list[dict],
    auto_balance_mailboxes: bool,
) -> dict:
    normalized = sorted(set(address.lower() for address in recipients))
    checked = validate_attachments(attachments)
    return {
        "recipient_count": len(normalized),
        "recipient_digest": hashlib.sha256("\n".join(normalized).encode()).hexdigest(),
        "subject": subject,
        "body_digest": hashlib.sha256(body.encode()).hexdigest(),
        "mailbox_id": mailbox_id,
        "template_id": template_id,
        "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        "attachments": [
            {"filename": item["filename"], "content_type": item["content_type"], "sha256": item["sha256"], "size": item["size"]}
            for item in checked
        ],
        "auto_balance_mailboxes": auto_balance_mailboxes,
    }


def _mailbox_pool(db: Session, mailbox_id: int | None, auto_balance: bool) -> list[SenderMailbox | None]:
    if mailbox_id:
        mailbox = db.get(SenderMailbox, mailbox_id)
        if not mailbox or not mailbox.active:
            raise LookupError("Sender mailbox not found or inactive")
        return [mailbox]
    if not auto_balance:
        return [None]
    mailboxes = db.scalars(
        select(SenderMailbox).where(SenderMailbox.active.is_(True)).order_by(SenderMailbox.id)
    ).all()
    mailboxes = [mailbox for mailbox in mailboxes if _mailbox_ready(mailbox)]
    if not mailboxes:
        return [None]
    load = {
        mailbox.id: int(
            db.scalar(
                select(func.count()).select_from(OutboundMessage).where(
                    OutboundMessage.mailbox_id == mailbox.id,
                    OutboundMessage.status.in_(["queued", "waiting_configuration", "retry"]),
                )
            )
            or 0
        )
        for mailbox in mailboxes
    }
    return sorted(mailboxes, key=lambda mailbox: (load[mailbox.id], mailbox.id))


def _mailbox_ready(mailbox: SenderMailbox | None) -> bool:
    if mailbox is None:
        return bool(settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.smtp_from_email)
    password = os.environ.get(mailbox.secret_ref, "") if mailbox.secret_ref else ""
    return bool(mailbox.active and mailbox.smtp_host and (mailbox.username or mailbox.address) and password and mailbox.address)


def queue_campaign(
    db: Session,
    *,
    campaign_key: str,
    recipients: list[str],
    subject: str,
    body: str,
    mailbox_id: int | None,
    template_id: int | None,
    scheduled_at: datetime | None,
    attachments: list[dict],
    auto_balance_mailboxes: bool,
) -> dict:
    persisted_attachments = persist_campaign_attachments(campaign_key, attachments)
    eligible, without_consent = verified_recipients(db, recipients)
    pool = _mailbox_pool(db, mailbox_id, auto_balance_mailboxes)
    rotation = cycle(pool)
    queued = suppressed = duplicate = waiting_configuration = 0
    mailbox_distribution: dict[str, int] = {}
    when = scheduled_at or utcnow()
    for address in eligible:
        if db.get(Suppression, address):
            suppressed += 1
            continue
        if db.scalar(
            select(OutboundMessage.id).where(
                OutboundMessage.campaign_key == campaign_key,
                OutboundMessage.recipient == address,
            )
        ):
            duplicate += 1
            continue
        mailbox = next(rotation)
        assigned_id = mailbox.id if mailbox else None
        ready = _mailbox_ready(mailbox)
        db.add(
            OutboundMessage(
                campaign_key=campaign_key,
                recipient=address,
                subject=subject,
                body=body,
                mailbox_id=assigned_id,
                template_id=template_id,
                scheduled_at=when,
                attachments=persisted_attachments,
                status="queued" if ready else "waiting_configuration",
            )
        )
        key = str(assigned_id or "default")
        mailbox_distribution[key] = mailbox_distribution.get(key, 0) + 1
        queued += 1
        waiting_configuration += int(not ready)
    db.flush()
    required_credentials: list[str] = []
    if waiting_configuration:
        required_credentials = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL"]
        if mailbox_id:
            explicit = db.get(SenderMailbox, mailbox_id)
            required_credentials = [explicit.secret_ref or f"SMTP_MAILBOX_{mailbox_id}_PASSWORD"] if explicit else required_credentials
    return {
        "status": (
            "credentials_required"
            if queued and waiting_configuration == queued
            else "queued"
            if queued
            else "blocked_no_eligible_recipients"
        ),
        "queued": queued,
        "waiting_configuration": waiting_configuration,
        "credentials_required": required_credentials,
        "suppressed": suppressed,
        "duplicate": duplicate,
        "without_verified_consent": len(without_consent),
        "mailbox_distribution": mailbox_distribution,
    }
