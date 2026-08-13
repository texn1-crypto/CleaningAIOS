from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import InboxMessage, OwnerNotification, SenderMailbox


log = logging.getLogger("cleaningai.notifications")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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
        status="queued" if configured else "waiting_configuration",
    )
    db.add(row)
    db.flush()
    return row


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


def _send_telegram(row: OwnerNotification) -> None:
    if not all([row.recipient, settings.telegram_bot_token]):
        raise RuntimeError("Telegram owner credentials are not configured")
    payload: dict[str, Any] = {"chat_id": row.recipient, "text": f"{row.subject}\n\n{row.body}"}
    approval_id = row.data.get("approval_id")
    if approval_id:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "✅ Одобрить", "callback_data": f"approve:{approval_id}"},
                {"text": "❌ Отклонить", "callback_data": f"reject:{approval_id}"},
            ]]
        }
    preview_posts = row.data.get("preview_posts") if isinstance(row.data, dict) else None
    with httpx.Client(timeout=30) as client:
        if isinstance(preview_posts, list) and preview_posts:
            media: list[dict[str, str]] = []
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
                media.append({"type": "photo", "media": image_url, "caption": caption})
            response = client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMediaGroup",
                json={"chat_id": row.recipient, "media": media},
            )
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
            _send_telegram(row)
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
        row.last_error = str(exc)
        row.available_at = now + timedelta(minutes=5)
    except Exception as exc:
        row.attempts += 1
        row.last_error = str(exc)
        row.status = "failed" if row.attempts >= 5 else "retry"
        row.available_at = now + timedelta(seconds=min(300, 2 ** row.attempts))
        log.warning("owner notification %s failed: %s", row.id, type(exc).__name__)
    db.commit()
    return True
