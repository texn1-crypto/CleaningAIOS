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
from .chat import redact_sensitive_text
from .db import SessionLocal
from .orchestrator import run_next
from .models import AuditLog, MailTransportState, OutboundMessage, SenderMailbox, Suppression
from .platform import process_next_event
from .notifications import queue_owner_notification, send_next_owner_notification
from .inbound_mail import collect_inbound_replies
from .security import unsubscribe_token
from .social_runtime import generate_next_social_visual, publish_next_social_post
from .logging_config import configure_logging

configure_logging("worker")
log = logging.getLogger("cleaningai.worker")

SMTP_AUTH_REASON = "SMTP authentication failed; verify the mailbox application password"
SMTP_PROVIDER_BLOCK_REASON = "SMTP provider blocked the sender mailbox; verify provider status"
SMTP_RATE_LIMIT_REASON = "SMTP provider rate-limited the sender mailbox; delivery is cooling down"


def _safe_worker_error(exc: Exception) -> str:
    value = redact_sensitive_text(str(exc))
    for secret in (
        settings.smtp_password,
        settings.telegram_bot_token,
        settings.api_key,
    ):
        if secret:
            value = value.replace(secret, "<redacted>")
    detail = value.strip()[:1000]
    return f"{type(exc).__name__}: {detail or 'email delivery failed'}"


def _mailbox_key(mailbox_id: int | None) -> str:
    return str(mailbox_id) if mailbox_id is not None else "default"


def _set_transport_failure(
    db,
    *,
    mailbox_id: int | None,
    status: str,
    reason: str,
    now: datetime,
    retry_after: datetime | None = None,
) -> MailTransportState:
    key = _mailbox_key(mailbox_id)
    state = db.get(MailTransportState, key)
    if state is None:
        state = MailTransportState(mailbox_key=key)
        db.add(state)
    state.status = status
    state.reason = reason
    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    state.blocked_at = now
    state.retry_after = retry_after
    state.updated_at = now
    return state


def _transport_ready(db, *, mailbox_id: int | None, now: datetime) -> bool:
    """Return whether SMTP may be attempted without defeating the circuit breaker."""
    key = _mailbox_key(mailbox_id)
    state = db.get(MailTransportState, key)
    if state is None or state.status == "ready":
        return True
    if state.status == "rate_limited" and state.retry_after and state.retry_after <= now:
        previous_failures = int(state.consecutive_failures or 0)
        state.status = "ready"
        state.reason = ""
        state.retry_after = None
        state.updated_at = now
        db.add(
            AuditLog(
                actor="worker",
                action="outreach.smtp_cooldown_completed",
                resource_type="sender_mailbox",
                resource_id=key,
                details={"previous_failures": previous_failures},
            )
        )
        return True
    return False


def outreach_delivery_window(now: datetime) -> tuple[datetime | None, datetime]:
    """Return the current configured local delivery window and its next start."""
    if not 0 <= settings.outreach_daily_start_hour <= 23:
        raise ValueError("OUTREACH_DAILY_START_HOUR must be between 0 and 23")
    if not 1 <= settings.outreach_daily_end_hour <= 23:
        raise ValueError("OUTREACH_DAILY_END_HOUR must be between 1 and 23")
    if settings.outreach_daily_start_hour >= settings.outreach_daily_end_hour:
        raise ValueError("OUTREACH_DAILY_START_HOUR must be before OUTREACH_DAILY_END_HOUR")
    local_zone = ZoneInfo(settings.outreach_timezone)
    aware_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    local_now = aware_utc.astimezone(local_zone)
    today_start = local_now.replace(
        hour=settings.outreach_daily_start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    today_end = local_now.replace(
        hour=settings.outreach_daily_end_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_now < today_start:
        return None, today_start.astimezone(timezone.utc).replace(tzinfo=None)
    tomorrow_start = today_start + timedelta(days=1)
    if local_now >= today_end:
        return None, tomorrow_start.astimezone(timezone.utc).replace(tzinfo=None)
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
    mailbox_scope = (
        OutboundMessage.mailbox_id == row.mailbox_id
        if row.mailbox_id is not None
        else OutboundMessage.mailbox_id.is_(None)
    )
    safe_error = SMTP_AUTH_REASON
    db.execute(
        update(OutboundMessage)
        .where(
            mailbox_scope,
            OutboundMessage.status.in_(["queued", "waiting_configuration"]),
        )
        .values(
            status="waiting_configuration",
            error=safe_error,
        )
    )
    mailbox_key = _mailbox_key(row.mailbox_id)
    _set_transport_failure(
        db,
        mailbox_id=row.mailbox_id,
        status="credentials_required",
        reason=safe_error,
        now=now,
    )
    queue_owner_notification(
        db,
        idempotency_key=f"outreach-smtp-auth:{mailbox_key}:{now.date().isoformat()}:telegram",
        channel="telegram",
        resource_type="sender_mailbox",
        resource_id=mailbox_key,
        subject="📨 Рассылка приостановлена: SMTP",
        body=(
            "Почтовый сервер отклонил авторизацию. Очередь сохранена и не будет "
            "массово помечена ошибочной. Проверьте пароль приложения отправителя, "
            "затем нажмите кнопку повторной проверки в панели рассылок. До этого "
            "worker не будет повторять вход с неверным паролем."
        ),
        data={
            "mailbox_id": row.mailbox_id,
            "status": "credentials_required",
            "retry_at": None,
        },
        correlation_id=f"mailbox:{mailbox_key}",
    )
    db.add(
        AuditLog(
            actor="worker",
            action="outreach.smtp_auth_failed",
            resource_type="sender_mailbox",
            resource_id=mailbox_key,
            details={
                "status": "credentials_required",
                "retry_at": None,
            },
        )
    )


def _provider_failure_kind(exc: smtplib.SMTPResponseException) -> str | None:
    response = exc.smtp_error
    if isinstance(response, bytes):
        response = response.decode("utf-8", errors="replace")
    message = str(response).lower()
    blocked_markers = (
        "account is blocked",
        "account blocked",
        "account disabled",
        "sender blocked",
        "mailbox blocked",
        "spam detected",
        "spam policy",
        "rejected under suspicion of spam",
        "access denied",
    )
    rate_limit_markers = (
        "sending limit",
        "rate limit",
        "too many messages",
        "temporarily locked",
        "quota exceeded",
        "daily user sending quota",
        "4.7.28",
    )
    if any(marker in message for marker in blocked_markers):
        return "provider_blocked"
    if int(exc.smtp_code or 0) in {421, 454} or any(marker in message for marker in rate_limit_markers):
        return "rate_limited"
    return None


def _is_provider_mailbox_block(exc: smtplib.SMTPResponseException) -> bool:
    """Backward-compatible predicate for callers that only need handled/unhandled."""
    return _provider_failure_kind(exc) is not None


def _defer_mailbox_after_provider_block(
    db,
    row: OutboundMessage,
    *,
    now: datetime,
    failure_kind: str,
) -> None:
    if failure_kind not in {"provider_blocked", "rate_limited"}:
        raise ValueError("Unsupported SMTP provider failure kind")
    retry_at = (
        now + timedelta(hours=settings.outreach_rate_limit_cooldown_hours)
        if failure_kind == "rate_limited"
        else None
    )
    mailbox_scope = (
        OutboundMessage.mailbox_id == row.mailbox_id
        if row.mailbox_id is not None
        else OutboundMessage.mailbox_id.is_(None)
    )
    safe_error = SMTP_RATE_LIMIT_REASON if failure_kind == "rate_limited" else SMTP_PROVIDER_BLOCK_REASON
    values = {"status": "waiting_configuration", "error": safe_error}
    if retry_at is not None:
        values["scheduled_at"] = retry_at
    db.execute(
        update(OutboundMessage)
        .where(
            mailbox_scope,
            OutboundMessage.status.in_(["queued", "waiting_configuration"]),
        )
        .values(**values)
    )
    mailbox_key = _mailbox_key(row.mailbox_id)
    _set_transport_failure(
        db,
        mailbox_id=row.mailbox_id,
        status=failure_kind,
        reason=safe_error,
        now=now,
        retry_after=retry_at,
    )
    is_rate_limit = failure_kind == "rate_limited"
    queue_owner_notification(
        db,
        idempotency_key=f"outreach-smtp-{failure_kind}:{mailbox_key}:{now.date().isoformat()}:telegram",
        channel="telegram",
        resource_type="sender_mailbox",
        resource_id=mailbox_key,
        subject=(
            "📨 Рассылка временно охлаждается"
            if is_rate_limit
            else "📨 Рассылка приостановлена почтовым провайдером"
        ),
        body=(
            (
                "Провайдер временно ограничил частоту. Очередь сохранена; worker не будет "
                f"повторять отправку до {retry_at.isoformat()}. После паузы работа возобновится автоматически."
            )
            if is_rate_limit
            else (
                "Почтовый провайдер заблокировал отправителя. Очередь сохранена, повторная "
                "отправка полностью остановлена. После разблокировки нажмите кнопку повторной "
                "проверки в панели рассылок."
            )
        ),
        data={
            "mailbox_id": row.mailbox_id,
            "status": failure_kind,
            "retry_at": retry_at.isoformat() if retry_at else None,
        },
        severity="high" if is_rate_limit else "critical",
        correlation_id=f"mailbox:{mailbox_key}",
    )
    db.add(
        AuditLog(
            actor="worker",
            action=("outreach.smtp_rate_limited" if is_rate_limit else "outreach.smtp_provider_blocked"),
            resource_type="sender_mailbox",
            resource_id=mailbox_key,
            details={
                "status": failure_kind,
                "retry_at": retry_at.isoformat() if retry_at else None,
            },
        )
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
    if settings.outreach_min_interval_minutes < 0:
        raise ValueError("OUTREACH_MIN_INTERVAL_MINUTES must not be negative")
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
        candidate_mailbox_scope = (
            OutboundMessage.mailbox_id == candidate.mailbox_id
            if candidate.mailbox_id is not None
            else OutboundMessage.mailbox_id.is_(None)
        )
        last_sent_at = db.scalar(
            select(func.max(OutboundMessage.sent_at)).where(
                candidate_mailbox_scope,
                OutboundMessage.sent_at >= window_start,
                OutboundMessage.sent_at <= now,
            )
        )
        if (
            last_sent_at is not None
            and now - last_sent_at < timedelta(minutes=settings.outreach_min_interval_minutes)
        ):
            continue
        if not _transport_ready(db, mailbox_id=candidate.mailbox_id, now=now):
            state = db.get(MailTransportState, _mailbox_key(candidate.mailbox_id))
            candidate.status = "waiting_configuration"
            candidate.error = state.reason if state else "SMTP transport is quarantined"
            continue
        if candidate_mailbox and not candidate_mailbox.active:
            candidate.status = "waiting_configuration"
            candidate.error = "sender mailbox is inactive"
            continue
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
        row.status = "waiting_configuration"; row.error = "sender mailbox is inactive"; db.commit(); return True
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
    except smtplib.SMTPResponseException as exc:
        failure_kind = _provider_failure_kind(exc)
        if failure_kind:
            _defer_mailbox_after_provider_block(db, row, now=now, failure_kind=failure_kind)
            log.warning("SMTP provider blocked mailbox %s; queue deferred", row.mailbox_id or "default")
        else:
            row.status = "failed"
            row.error = f"SMTP delivery failed with response code {int(exc.smtp_code or 0)}"
            db.add(
                AuditLog(
                    actor="worker",
                    action="outreach.delivery_failed",
                    resource_type="outbound_message",
                    resource_id=str(row.id),
                    details={"error_type": type(exc).__name__, "smtp_code": int(exc.smtp_code or 0)},
                )
            )
            log.warning("SMTP delivery failed for message %s with response code %s", row.id, exc.smtp_code)
    except Exception as exc:
        safe_error = _safe_worker_error(exc)
        row.status = "failed"; row.error = safe_error
        db.add(
            AuditLog(
                actor="worker",
                action="outreach.delivery_failed",
                resource_type="outbound_message",
                resource_id=str(row.id),
                details={"error_type": type(exc).__name__, "error": safe_error},
            )
        )
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
                from .system_admin import record_component_recovery

                record_component_recovery(db, component="worker")
                db.commit()
            except Exception as exc:
                db.rollback(); log.exception("task processing failed")
                try:
                    from .system_admin import record_component_failure

                    record_component_failure(db, component="worker", error=exc)
                    db.commit()
                except Exception:
                    db.rollback()
                    log.exception("system administrator could not persist worker failure")
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
