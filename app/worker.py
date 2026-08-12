import logging
import os
import base64
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal
from .orchestrator import run_next
from .models import OutboundMessage, SenderMailbox, Suppression
from .platform import process_next_event
from .notifications import send_next_owner_notification
from .security import unsubscribe_token

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cleaningai.worker")


def send_next_email(db) -> bool:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sent_minute = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.sent_at >= now - timedelta(minutes=1))) or 0
    sent_day = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.sent_at >= now - timedelta(days=1))) or 0
    if sent_minute >= settings.outreach_per_minute or sent_day >= settings.outreach_per_day:
        return False
    row = db.scalar(select(OutboundMessage).where(OutboundMessage.status.in_(["queued", "waiting_configuration"]), OutboundMessage.scheduled_at <= now).order_by(OutboundMessage.id).with_for_update(skip_locked=True))
    if not row:
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
    if mailbox:
        mailbox_minute = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.mailbox_id == mailbox.id, OutboundMessage.sent_at >= now - timedelta(minutes=1))) or 0
        mailbox_day = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.mailbox_id == mailbox.id, OutboundMessage.sent_at >= now - timedelta(days=1))) or 0
        if mailbox_minute >= mailbox.per_minute or mailbox_day >= mailbox.per_day:
            return False
    unsubscribe_query = urlencode({"email": row.recipient, "token": unsubscribe_token(row.recipient)})
    unsubscribe = f"{settings.public_base_url.rstrip('/')}/api/outreach/unsubscribe?{unsubscribe_query}"
    message = EmailMessage(); message["From"] = from_email; message["To"] = row.recipient; message["Subject"] = row.subject
    message.set_content(f"{row.body}\n\nОтписаться: {unsubscribe}")
    try:
        for attachment in row.attachments or []:
            raw = base64.b64decode(str(attachment.get("content_base64", "")), validate=True)
            if len(raw) > settings.max_attachment_bytes: raise ValueError("attachment is too large")
            content_type = str(attachment.get("content_type", "application/octet-stream"))
            maintype, subtype = content_type.split("/", 1) if "/" in content_type else ("application", "octet-stream")
            message.add_attachment(raw, maintype=maintype, subtype=subtype, filename=str(attachment.get("filename") or "attachment"))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
            smtp.starttls(); smtp.login(smtp_username, smtp_password); smtp.send_message(message)
        row.status = "sent"; row.sent_at = now
        if mailbox: mailbox.sent_today += 1; mailbox.last_sent_at = now
    except Exception as exc:
        row.status = "failed"; row.error = str(exc)
        log.exception("email delivery failed for message %s", row.id)
    db.commit(); return True


def main() -> None:
    log.info("background worker started")
    while True:
        with SessionLocal() as db:
            try:
                task = run_next(db)
                if task: log.info("processed task %s", task.id)
                event = process_next_event(db)
                if event: log.info("published event %s", event.id)
                send_next_email(db)
                send_next_owner_notification(db)
            except Exception:
                db.rollback(); log.exception("task processing failed")
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
