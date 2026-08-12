import logging
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal
from .orchestrator import run_next
from .models import OutboundMessage, Suppression

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cleaningai.worker")


def send_next_email(db) -> bool:
    if not all([settings.smtp_host, settings.smtp_username, settings.smtp_password, settings.smtp_from_email]):
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sent_minute = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.sent_at >= now - timedelta(minutes=1))) or 0
    sent_day = db.scalar(select(func.count(OutboundMessage.id)).where(OutboundMessage.sent_at >= now - timedelta(days=1))) or 0
    if sent_minute >= settings.outreach_per_minute or sent_day >= settings.outreach_per_day:
        return False
    row = db.scalar(select(OutboundMessage).where(OutboundMessage.status == "queued", OutboundMessage.scheduled_at <= now).order_by(OutboundMessage.id).with_for_update(skip_locked=True))
    if not row:
        return False
    if db.get(Suppression, row.recipient):
        row.status = "suppressed"; db.commit(); return True
    unsubscribe = f"{settings.public_base_url.rstrip('/')}/api/outreach/unsubscribe?email={row.recipient}"
    message = EmailMessage(); message["From"] = settings.smtp_from_email; message["To"] = row.recipient; message["Subject"] = row.subject
    message.set_content(f"{row.body}\n\nОтписаться: {unsubscribe}")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            smtp.starttls(); smtp.login(settings.smtp_username, settings.smtp_password); smtp.send_message(message)
        row.status = "sent"; row.sent_at = now
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
                send_next_email(db)
            except Exception:
                db.rollback(); log.exception("task processing failed")
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
