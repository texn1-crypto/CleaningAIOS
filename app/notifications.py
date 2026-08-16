from __future__ import annotations

import logging
import hashlib
import json
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import (
    ApprovalRequest,
    AuditLog,
    DomainEvent,
    InboxMessage,
    MediaAsset,
    OwnerNotification,
    SenderMailbox,
)
from .telegram_control import approval_card, issue_alert_ack_token


log = logging.getLogger("cleaningai.notifications")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _safe_delivery_error(exc: Exception) -> str:
    value = str(exc)
    if settings.telegram_bot_token:
        value = value.replace(settings.telegram_bot_token, "<redacted>")
    value = re.sub(r"(https://api\.telegram\.org/bot)[^/\s]+", r"\1<redacted>", value)
    return value[:4000]

ALERT_SEVERITY = {
    "approval.requested": "high",
    "task.failed": "critical",
    "agent.incident_reported": "critical",
    "payment.overdue": "critical",
    "deadline.within.3.days": "high",
    "outreach.complaint": "critical",
}
ALERT_SUBJECT = {
    "approval.requested": "Требуется решение владельца",
    "task.failed": "Критический сбой задачи",
    "agent.incident_reported": "Инцидент агента",
    "payment.overdue": "Просрочен платёж",
    "deadline.within.3.days": "Приближается срок тендера",
    "outreach.complaint": "Жалоба на рассылку",
}


class NotificationNotFound(LookupError):
    pass


class NotificationNotDelivered(RuntimeError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mailbox_smtp(mailbox: SenderMailbox | None) -> tuple[str, int, str, str, str] | None:
    if mailbox is None or not mailbox.active:
        return None
    password = os.environ.get(mailbox.secret_ref, "") if mailbox.secret_ref else ""
    username = mailbox.username or mailbox.address
    if not all([mailbox.smtp_host, username, password, mailbox.address]):
        return None
    return mailbox.smtp_host, mailbox.smtp_port, username, password, mailbox.address


def _default_smtp() -> tuple[str, int, str, str, str] | None:
    if not all([settings.smtp_host, settings.smtp_username, settings.smtp_password, settings.smtp_from_email]):
        return None
    return (
        settings.smtp_host,
        settings.smtp_port,
        settings.smtp_username,
        settings.smtp_password,
        settings.smtp_from_email,
    )


def _notification_smtp(db: Session, data: dict[str, Any]) -> tuple[str, int, str, str, str] | None:
    """Prefer the originating mailbox so inbound forwarding needs no second SMTP account."""
    mailbox_id = data.get("mailbox_id") if isinstance(data, dict) else None
    if isinstance(mailbox_id, int):
        mailbox_smtp = _mailbox_smtp(db.get(SenderMailbox, mailbox_id))
        if mailbox_smtp:
            return mailbox_smtp
    return _default_smtp()


def _inbound_target_is_safe(db: Session, recipient: str, data: dict[str, Any]) -> bool:
    mailbox_id = data.get("mailbox_id") if isinstance(data, dict) else None
    if not isinstance(mailbox_id, int):
        return True
    mailbox = db.get(SenderMailbox, mailbox_id)
    return mailbox is None or mailbox.address.lower() != recipient.strip().lower()


def queue_owner_notification(
    db: Session,
    *,
    idempotency_key: str,
    channel: str,
    resource_type: str,
    resource_id: str,
    subject: str,
    body: str,
    data: dict[str, Any] | None = None,
    severity: str = "normal",
    correlation_id: str = "",
) -> OwnerNotification:
    existing = db.scalar(select(OwnerNotification).where(OwnerNotification.idempotency_key == idempotency_key))
    if existing:
        return existing
    recipient = settings.owner_notification_email if channel == "email" else settings.owner_telegram_id
    configured = bool(recipient)
    if channel == "email":
        configured = (
            configured
            and _inbound_target_is_safe(db, recipient, data or {})
            and bool(_notification_smtp(db, data or {}))
        )
    elif channel == "telegram":
        configured = configured and bool(settings.telegram_bot_token)
    row = OwnerNotification(
        idempotency_key=idempotency_key,
        channel=channel,
        recipient=recipient,
        resource_type=resource_type,
        resource_id=resource_id,
        subject=subject,
        body=body,
        data=data or {},
        severity=severity if severity in {"normal", "high", "critical"} else "normal",
        correlation_id=correlation_id,
        status="queued" if configured else "waiting_configuration",
    )
    db.add(row)
    db.flush()
    return row


def queue_critical_alert_for_event(
    db: Session,
    event: DomainEvent,
) -> OwnerNotification | None:
    """Project selected outbox events into one durable Telegram alert."""
    severity = ALERT_SEVERITY.get(event.event_type)
    if severity is None:
        return None
    approval_id = None
    if event.event_type == "approval.requested":
        candidate = (event.payload or {}).get("approval_id")
        if isinstance(candidate, int):
            approval = db.get(ApprovalRequest, candidate)
            if approval is None or approval.resource_type != "task":
                return None
            approval_id = approval.id
            severity = (
                "critical"
                if approval.action_kind in {"financial", "legal", "contract", "hr_final", "tender_submission"}
                else "high"
            )
    body = (
        f"Событие: {event.event_type}\n"
        f"Объект: {event.aggregate_type} #{event.aggregate_id}\n"
        f"Correlation ID: {event.correlation_id}\n"
        "Подтвердите получение; критическое бизнес-действие этим не выполняется."
    )
    data: dict[str, Any] = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "severity": severity,
        "correlation_id": event.correlation_id,
        "acknowledgement_required": True,
    }
    if approval_id is not None:
        data["approval_id"] = approval_id
    return queue_owner_notification(
        db,
        idempotency_key=f"critical-event:{event.event_id}:telegram",
        channel="telegram",
        resource_type=event.aggregate_type,
        resource_id=event.aggregate_id,
        subject=f"[{severity.upper()}] {ALERT_SUBJECT[event.event_type]}",
        body=body,
        data=data,
        severity=severity,
        correlation_id=event.correlation_id,
    )


def acknowledge_owner_notification(
    db: Session,
    *,
    notification_id: int,
    actor: str,
) -> dict[str, Any]:
    row = db.scalar(
        select(OwnerNotification)
        .where(OwnerNotification.id == notification_id)
        .with_for_update()
    )
    if row is None:
        raise NotificationNotFound("Owner notification not found")
    if row.acknowledged_at is not None:
        return {
            "id": row.id,
            "status": row.status,
            "acknowledged_at": row.acknowledged_at,
            "idempotent_replay": True,
        }
    if row.status != "sent":
        raise NotificationNotDelivered("Only a delivered notification can be acknowledged")
    row.acknowledged_at = now_utc()
    row.acknowledged_by = actor
    db.add(
        AuditLog(
            actor=actor,
            action="owner_notification.acknowledged",
            resource_type="owner_notification",
            resource_id=str(row.id),
            details={
                "severity": row.severity,
                "correlation_id": row.correlation_id,
            },
        )
    )
    from .platform import event_bus

    event_bus.publish(
        db,
        "owner_notification.acknowledged",
        "owner_notification",
        str(row.id),
        {"severity": row.severity},
        idempotency_key=f"owner-notification:{row.id}:acknowledged",
        correlation_id=row.correlation_id or None,
        actor=actor,
    )
    db.flush()
    return {
        "id": row.id,
        "status": row.status,
        "acknowledged_at": row.acknowledged_at,
        "idempotent_replay": False,
    }


def _send_email(db: Session, row: OwnerNotification) -> None:
    smtp_config = _notification_smtp(db, row.data or {})
    if not row.recipient or not smtp_config:
        raise RuntimeError("Owner email or SMTP credentials are not configured")
    if not _inbound_target_is_safe(db, row.recipient, row.data or {}):
        raise RuntimeError("Owner notification email must differ from the monitored mailbox")
    smtp_host, smtp_port, smtp_username, smtp_password, from_email = smtp_config
    message = EmailMessage()
    message["From"] = from_email
    message["To"] = row.recipient
    message["Subject"] = row.subject
    reply_to = str((row.data or {}).get("reply_to") or "").strip()
    if reply_to and "@" in reply_to and "\r" not in reply_to and "\n" not in reply_to:
        message["Reply-To"] = reply_to
    message.set_content(row.body)
    smtp_factory = smtplib.SMTP_SSL if smtp_port == 465 else smtplib.SMTP
    with smtp_factory(smtp_host, smtp_port, timeout=20) as smtp:
        if smtp_port != 465:
            smtp.starttls()
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def _verified_media_attachment(asset: MediaAsset) -> tuple[bytes, str, str]:
    if not asset.storage_path:
        raise RuntimeError("Generated image file is unavailable")
    root = Path(settings.document_storage_path).resolve()
    path = Path(asset.storage_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Generated image is outside document storage") from exc
    raw = path.read_bytes()
    digest = str((asset.metadata_json or {}).get("sha256") or "")
    if len(digest) != 64 or hashlib.sha256(raw).hexdigest() != digest:
        raise RuntimeError("Generated image checksum mismatch")
    suffix = path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise RuntimeError("Generated image has an unsupported file type")
    content_type = "image/png" if suffix == ".png" else "image/jpeg"
    return raw, f"ai-image-{asset.id}{suffix}", content_type


def _send_telegram(db: Session, row: OwnerNotification) -> None:
    if not all([row.recipient, settings.telegram_bot_token]):
        raise RuntimeError("Telegram owner credentials are not configured")
    payload: dict[str, Any] = {"chat_id": row.recipient, "text": f"{row.subject}\n\n{row.body}"}
    approval_id = row.data.get("approval_id")
    if approval_id:
        approval = db.get(ApprovalRequest, int(approval_id))
        callbacks = approval_card(approval).get("callbacks") if approval else None
        if not callbacks:
            raise RuntimeError("Approval callback tokens are unavailable")
        payload["reply_markup"] = {
            "inline_keyboard": [
                [
                    {"text": "✅ Одобрить", "callback_data": callbacks["approve"]},
                    {"text": "❌ Отклонить", "callback_data": callbacks["reject"]},
                ],
                [
                    {
                        "text": "✏️ Запросить изменения",
                        "callback_data": callbacks["request_changes"],
                    }
                ],
            ]
        }
    if (row.severity or "normal") in {"high", "critical"}:
        keyboard = payload.setdefault("reply_markup", {"inline_keyboard": []})[
            "inline_keyboard"
        ]
        keyboard.append(
            [
                {
                    "text": "✅ Принять оповещение",
                    "callback_data": issue_alert_ack_token(row),
                }
            ]
        )
    preview_posts = row.data.get("preview_posts") if isinstance(row.data, dict) else None
    media_asset_id = row.data.get("media_asset_id") if isinstance(row.data, dict) else None
    with httpx.Client(timeout=30) as client:
        if isinstance(media_asset_id, int):
            asset = db.get(MediaAsset, media_asset_id)
            if not asset or asset.status != "ready":
                raise RuntimeError("Generated image is not ready")
            raw, filename, content_type = _verified_media_attachment(asset)
            caption = f"{row.subject}\n\n{row.body}"
            if len(caption) > 1024:
                caption = caption[:1023] + "…"
            response = client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendPhoto",
                data={"chat_id": row.recipient, "caption": caption},
                files={"photo": (filename, raw, content_type)},
            )
            response.raise_for_status()
            return
        if isinstance(preview_posts, list) and preview_posts:
            media: list[dict[str, str]] = []
            files: list[tuple[str, tuple[str, bytes, str]]] = []
            attached: set[str] = set()
            for post in preview_posts[:10]:
                image_url = str(post.get("image_url") or "")
                if image_url.startswith("/static/"):
                    image_url = urljoin(settings.public_base_url.rstrip("/") + "/", image_url.lstrip("/"))
                parsed = urlparse(image_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise RuntimeError("Social preview image URL is not public")
                channel = str(post.get("channel") or "").upper()
                schedule = str(post.get("scheduled_at") or "")
                body = str(post.get("body") or "")
                caption = f"{channel} · {schedule} UTC\n\n{body}"
                if len(caption) > 1024:
                    raise RuntimeError("Social preview caption exceeds Telegram limit")
                media_value = image_url
                asset_id = int(post.get("visual_asset_id") or 0)
                asset = db.get(MediaAsset, asset_id) if asset_id else None
                if asset and asset.storage_path:
                    raw, filename, content_type = _verified_media_attachment(asset)
                    attach_name = f"asset_{asset.id}"
                    media_value = f"attach://{attach_name}"
                    if attach_name not in attached:
                        files.append((attach_name, (filename, raw, content_type)))
                        attached.add(attach_name)
                media.append({"type": "photo", "media": media_value, "caption": caption})
            endpoint = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMediaGroup"
            if files:
                response = client.post(
                    endpoint,
                    data={"chat_id": row.recipient, "media": json.dumps(media, ensure_ascii=False)},
                    files=files,
                )
            else:
                response = client.post(endpoint, json={"chat_id": row.recipient, "media": media})
            response.raise_for_status()
        response = client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage", json=payload)
        response.raise_for_status()


def send_next_owner_notification(db: Session) -> bool:
    now = now_utc()
    row = db.scalar(
        select(OwnerNotification)
        .where(
            OwnerNotification.status.in_(["queued", "retry", "waiting_configuration"]),
            OwnerNotification.available_at <= now,
        )
        .order_by(OwnerNotification.id)
        .with_for_update(skip_locked=True)
    )
    if not row:
        return False
    try:
        if row.channel == "email":
            _send_email(db, row)
        elif row.channel == "telegram":
            _send_telegram(db, row)
        else:
            raise RuntimeError(f"Unsupported owner notification channel: {row.channel}")
        row.status = "sent"
        row.sent_at = now
        row.last_error = ""
        if row.resource_type == "inbox_message" and str(row.resource_id).isdigit():
            inbox = db.get(InboxMessage, int(row.resource_id))
            if inbox:
                inbox.data = {
                    **(inbox.data or {}),
                    "forwarded_to_owner": True,
                    "owner_notification_id": row.id,
                    "forwarded_at": now.isoformat(),
                }
    except RuntimeError as exc:
        row.status = "waiting_configuration"
        row.last_error = _safe_delivery_error(exc)
        row.available_at = now + timedelta(minutes=5)
    except Exception as exc:
        row.attempts += 1
        row.last_error = _safe_delivery_error(exc)
        row.status = "dead_letter" if row.attempts >= 5 else "retry"
        row.dead_lettered_at = now if row.status == "dead_letter" else None
        row.available_at = now + timedelta(seconds=min(300, 2 ** row.attempts))
        log.warning("owner notification %s failed: %s", row.id, type(exc).__name__)
    db.commit()
    return True
