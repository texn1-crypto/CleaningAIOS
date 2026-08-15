from __future__ import annotations

import logging
import os
import base64
import hashlib
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select, update

from .config import settings
from .db import SessionLocal
from .orchestrator import run_next
from .models import OutboundMessage, SenderMailbox, Suppression
from .platform import process_next_event
from .notifications import queue_owner_notification, send_next_owner_notification
from .inbound_mail import collect_inbound_replies
from .security import unsubscribe_token
from .social_runtime import generate_next_social_visual, publish_next_social_post

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cleaningai.worker")

SMTP_AUTH_RETRY_DELAY = timedelta(minutes=15)


def outreach_delivery_window(now: datetime) -> tuple[datetime | None, datetime]:
    """Return the current 09:00-local delivery window and its next start.

    Email delivery is closed before the configured local start hour.  After the
    hour it stays open until local midnight; the next window always begins at
    the configured hour on the following local calendar day.
    """
    if not 0 <= settings.outreach_daily_start_hour <= 23:
        raise ValueError("OUTREACH_DAILY_START_HOUR must be between 0 and 23")
    local_zone = ZoneInfo(settings.outreach_timezone)
    aware_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    local_now = aware_utc.astimezone(local_zone)
    today_start = local_now.replace(
        hour=settings.outreach_daily_start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_now < today_start:
        return None, today_start.astimezone(timezone.utc).replace(tzinfo=None)
    tomorrow_start = today_start + timedelta(days=1)
    return (
        today_start.astimezone(timezone.utc).replace(tzinfo=None),
        tomorrow_start.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _queue_outreach_progress(
    db,
    row: OutboundMessage,
    *,
    window_start: datetime,
    next_window_start: datetime,
) -> None:
    daily_sent = int(
        db.scalar(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.sent_at >= window_start,
                OutboundMessage.sent_at <= row.sent_at,
            )
        )
        or 0
    )

    campaign_sent = int(
        db.scalar(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.campaign_key == row.campaign_key,
                OutboundMessage.status == "sent",
            )
        )
        or 0
    )
    campaign_total = int(
        db.scalar(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.campaign_key == row.campaign_key,
            )
        )
        or 0
    )
    local_zone = ZoneInfo(settings.outreach_timezone)
    next_local = next_window_start.replace(tzinfo=timezone.utc).astimezone(local_zone)
    lines = [
        f"Сегодня: {daily_sent}/{settings.outreach_per_day}",
        f"Всего по файлу: {campaign_sent}/{campaign_total}",
    ]
    if campaign_sent >= campaign_total:
        lines.append("Рассылка по файлу завершена.")
    elif daily_sent >= settings.outreach_per_day:
        lines.append(f"Следующая партия: {next_local:%d.%m.%Y %H:%M} ({settings.outreach_timezone}).")
    else:
        lines.append("Следующее допустимое письмо будет отправлено автоматически.")
    queue_owner_notification(
        db,
        idempotency_key=f"outreach-progress:{row.id}:telegram",
        channel="telegram",
        resource_type="outreach_campaign",
        resource_id=row.campaign_key,
        subject=f"📨 Рассылка: {daily_sent}/{settings.outreach_per_day}",
        body="\n".join(lines),
        data={
            "campaign_key": row.campaign_key,
            "outbound_message_id": row.id,
            "daily_sent": daily_sent,
            "daily_limit": settings.outreach_per_day,
            "campaign_sent": campaign_sent,
            "campaign_total": campaign_total,
            "next_window_start": next_window_start.isoformat(),
        },
        correlation_id=f"campaign:{row.campaign_key}",
    )


def _defer_mailbox_after_auth_failure(
    db,
    row: OutboundMessage,
    *,
    now: datetime,
) -> None:
    """Circuit-break a mailbox after an SMTP authentication failure.

    Authentication errors apply to the sender mailbox rather than one
    recipient. Deferring the whole mailbox prevents a bad application password
    from turning every queued recipient into an immediate permanent failure.
    """
    retry_at = now + SMTP_AUTH_RETRY_DELAY
    mailbox_scope = (
        OutboundMessage.mailbox_id == row.mailbox_id
        if row.mailbox_id is not None
        else OutboundMessage.mailbox_id.is_(None)
    )
    safe_error = "SMTP authentication failed; verify the mailbox application password"
    db.execute(
        update(OutboundMessage)
        .where(
            mailbox_scope,
            OutboundMessage.status.in_(["queued", "waiting_configuration"]),
            OutboundMessage.scheduled_at < retry_at,
        )
        .values(
            status="waiting_configuration",
            scheduled_at=retry_at,
            error=safe_error,
        )
    )
    mailbox_key = str(row.mailbox_id or "default")
    queue_owner_notification(
        db,
        idempotency_key=f"outreach-smtp-auth:{mailbox_key}:{now.date().isoformat()}:telegram",
        channel="telegram",
        resource_type="sender_mailbox",
        resource_id=mailbox_key,
        subject="📨 Рассылка приостановлена: SMTP",
        body=(
            "Почтовый сервер отклонил авторизацию. Очередь сохранена и не будет "
            "массово помечена ошибочной. Проверьте пароль приложения отправителя; "
            f"следующая безопасная проверка после {retry_at.isoformat()}."
        ),
        data={
            "mailbox_id": row.mailbox_id,
            "status": "credentials_required",
            "retry_at": retry_at.isoformat(),
        },
        correlation_id=f"mailbox:{mailbox_key}",
    )


def send_next_email(db, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    window_start, next_window_start = outreach_delivery_window(now)
    if window_start is None:
        return False
    sent_minute = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.sent_at >= now - timedelta(minutes=1), OutboundMessage.sent_at <= now)) or 0
    sent_day = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.sent_at >= window_start, OutboundMessage.sent_at <= now)) or 0
    if sent_minute >= settings.outreach_per_minute or sent_day >= settings.outreach_per_day:
        return False
    candidates = db.scalars(
        select(OutboundMessage)
        .where(
            OutboundMessage.status.in_(["queued", "waiting_configuration"]),
            OutboundMessage.scheduled_at <= now,
        )
        .order_by(case((OutboundMessage.status == "queued", 0), else_=1), OutboundMessage.id)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    row = None
    for candidate in candidates:
        candidate_mailbox = db.get(SenderMailbox, candidate.mailbox_id) if candidate.mailbox_id else None
        if candidate_mailbox:
            candidate_password = os.environ.get(candidate_mailbox.secret_ref, "") if candidate_mailbox.secret_ref else ""
            if not all([candidate_mailbox.smtp_host, candidate_mailbox.username or candidate_mailbox.address, candidate_password, candidate_mailbox.address]):
                candidate.status = "waiting_configuration"
                candidate.error = "SMTP credentials are not configured"
                continue
        elif not all([settings.smtp_host, settings.smtp_username, settings.smtp_password, settings.smtp_from_email]):
            candidate.status = "waiting_configuration"
            candidate.error = "SMTP credentials are not configured"
            continue
        if candidate_mailbox and candidate_mailbox.active:
            mailbox_minute = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.mailbox_id == candidate_mailbox.id, OutboundMessage.sent_at >= now - timedelta(minutes=1), OutboundMessage.sent_at <= now)) or 0
            mailbox_day = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.mailbox_id == candidate_mailbox.id, OutboundMessage.sent_at >= window_start, OutboundMessage.sent_at <= now)) or 0
            if mailbox_minute >= candidate_mailbox.per_minute or mailbox_day >= candidate_mailbox.per_day:
                continue
        row = candidate
        break
    if row is None:
        if candidates:
            db.commit()
        return False
    if db.get(Suppression, row.recipient):
        row.status = "suppressed"; db.commit(); return True
    mailbox = db.get(SenderMailbox, row.mailbox_id) if row.mailbox_id else None
    smtp_host = mailbox.smtp_host if mailbox else settings.smtp_host
    smtp_port = mailbox.smtp_port if mailbox else settings.smtp_port
    smtp_username = mailbox.username if mailbox else settings.smtp_username
    smtp_password = os.environ.get(mailbox.secret_ref, "") if mailbox and mailbox.secret_ref else settings.smtp_password
    from_email = mailbox.address if mailbox else settings.smtp_from_email
    if mailbox and not mailbox.active:
        row.status = "failed"; row.error = "mailbox is inactive"; db.commit(); return True
    if not all([smtp_host, smtp_username, smtp_password, from_email]):
        row.status = "waiting_configuration"; row.error = "SMTP credentials are not configured"; db.commit(); return True
    unsubscribe_query = urlencode({"email": row.recipient, "token": unsubscribe_token(row.recipient)})
    unsubscribe = f"{settings.public_base_url.rstrip('/')}/api/outreach/unsubscribe?{unsubscribe_query}"
    message = EmailMessage(); message["From"] = from_email; message["To"] = row.recipient; message["Subject"] = row.subject
    message.set_content(f"{row.body}\n\nОтписаться: {unsubscribe}")
    try:
        for attachment in row.attachments or []:
            storage_path = str(attachment.get("storage_path") or "")
            if storage_path:
                root = Path(settings.document_storage_path).resolve()
                path = Path(storage_path).resolve()
                try:
                    path.relative_to(root)
                except ValueError as exc:
                    raise ValueError("attachment path is outside document storage") from exc
                raw = path.read_bytes()
                expected = str(attachment.get("sha256") or "")
                if expected and hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("attachment checksum mismatch")
            else:
                raw = base64.b64decode(str(attachment.get("content_base64", "")), validate=True)
            if len(raw) > settings.max_attachment_bytes: raise ValueError("attachment is too large")
            content_type = str(attachment.get("content_type", "application/octet-stream"))
            maintype, subtype = content_type.split("/", 1) if "/" in content_type else ("application", "octet-stream")
            message.add_attachment(raw, maintype=maintype, subtype=subtype, filename=str(attachment.get("filename") or "attachment"))
        smtp_factory = smtplib.SMTP_SSL if smtp_port == 465 else smtplib.SMTP
        with smtp_factory(smtp_host, smtp_port, timeout=20) as smtp:
            if smtp_port != 465:
                smtp.starttls()
            smtp.login(smtp_username, smtp_password); smtp.send_message(message)
        row.status = "sent"; row.sent_at = now
        if mailbox: mailbox.sent_today += 1; mailbox.last_sent_at = now
        db.flush()
        _queue_outreach_progress(
            db,
            row,
            window_start=window_start,
            next_window_start=next_window_start,
        )
    except smtplib.SMTPAuthenticationError:
        _defer_mailbox_after_auth_failure(db, row, now=now)
        log.warning("SMTP authentication failed for mailbox %s; queue deferred", row.mailbox_id or "default")
    except Exception as exc:
        row.status = "failed"; row.error = str(exc)
        log.exception("email delivery failed for message %s", row.id)
    db.commit(); return True


def main() -> None:
    log.info("background worker started")
    next_inbound_poll = 0.0
    while True:
        with SessionLocal() as db:
            try:
                task = run_next(db)
                if task: log.info("processed task %s", task.id)
                event = process_next_event(db)
                if event: log.info("published event %s", event.id)
                send_next_email(db)
                generate_next_social_visual(db)
                publish_next_social_post(db)
                send_next_owner_notification(db)
                if time.monotonic() >= next_inbound_poll:
                    result = collect_inbound_replies(db)
                    if result["received"] or result["failed"]:
                        log.info("inbound mail poll: received=%s failed=%s", result["received"], result["failed"])
                    next_inbound_poll = time.monotonic() + settings.inbound_mail_poll_seconds
            except Exception:
                db.rollback(); log.exception("task processing failed")
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
