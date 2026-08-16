from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import AuditLog, BusinessRecord, ContactEvent, InboxMessage, Task
from .notifications import queue_owner_notification
from .orchestrator import audit
from .platform import event_bus
from .schemas import LeadAutopilotCreate
from .task_state import record_task_created


SERVICE_LABELS = {
    "mcd": "ЖК / МКД",
    "business_center": "Бизнес-центр",
    "commercial": "Коммерческий объект",
    "general": "Генеральная уборка",
    "other": "Другой объект",
}
FREQUENCY_LABELS = {
    "once": "Разовая уборка",
    "weekly": "Еженедельно",
    "weekdays": "По будням",
    "daily": "Ежедневно",
    "custom": "Особый график",
}
URGENCY_LABELS = {
    "today": "Как можно скорее",
    "week": "В течение недели",
    "month": "В течение месяца",
    "planning": "Пока планируется",
}


class LeadRateLimitExceeded(RuntimeError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if len(digits) in {10, 11} else ""


def qualify_lead(payload: LeadAutopilotCreate) -> dict[str, Any]:
    score = 20
    score += {
        "mcd": 20,
        "business_center": 20,
        "commercial": 15,
        "general": 8,
        "other": 3,
    }[payload.service]
    score += {"today": 25, "week": 18, "month": 8, "planning": 2}[payload.urgency]
    score += 15 if payload.object_area >= 3000 else 10 if payload.object_area >= 1000 else 5
    score += {"daily": 10, "weekdays": 8, "weekly": 5, "once": 2, "custom": 3}[payload.frequency]
    if payload.company:
        score += 5
    score = min(100, score)
    return {
        "score": score,
        "status": "qualified" if score >= settings.hot_lead_score else "new",
        "priority": "high" if score >= settings.hot_lead_score else "normal",
        "hot": score >= settings.hot_lead_score,
    }


def _rounded_rubles(value: Decimal) -> int:
    return int(math.ceil(float(value) / 100.0) * 100)


def preliminary_estimate(payload: LeadAutopilotCreate) -> dict[str, Any]:
    minimum_rate = Decimal(str(settings.lead_estimate_min_rub_per_sqm or 0))
    maximum_rate = Decimal(str(settings.lead_estimate_max_rub_per_sqm or 0))
    minimum_order = Decimal(str(settings.lead_estimate_min_order_rub or 0))
    if minimum_rate <= 0 or maximum_rate < minimum_rate:
        return {
            "status": "pricing_configuration_required",
            "reason": "owner_approved_price_book_missing",
            "site_survey_required": True,
            "is_offer": False,
        }
    frequency_factor = {
        "once": Decimal("1"),
        "weekly": Decimal("4"),
        "weekdays": Decimal("22"),
        "daily": Decimal("30"),
    }.get(payload.frequency)
    if frequency_factor is None:
        return {
            "status": "schedule_required",
            "reason": "custom_frequency_requires_review",
            "site_survey_required": True,
            "is_offer": False,
        }
    area = Decimal(str(payload.object_area))
    estimate_min = max(minimum_order, area * minimum_rate * frequency_factor)
    estimate_max = max(minimum_order, area * maximum_rate * frequency_factor)
    return {
        "status": "preliminary",
        "currency": "RUB",
        "period": "visit" if payload.frequency == "once" else "month",
        "min_rub": _rounded_rubles(estimate_min),
        "max_rub": _rounded_rubles(estimate_max),
        "site_survey_required": True,
        "is_offer": False,
        "disclaimer": "Предварительный диапазон, не оферта; итог после обследования объекта.",
    }


def _notification_body(payload: LeadAutopilotCreate, lead: BusinessRecord, qualification: dict) -> str:
    contact = str(payload.email or "") or (f"+{normalize_phone(payload.phone)}" if payload.phone else "")
    telegram = f"@{payload.telegram_username}" if payload.telegram_username else "—"
    return (
        f"Лид #{lead.id}\n"
        f"Имя/компания: {lead.title}\n"
        f"Контакт: {contact}\n"
        f"Telegram: {telegram}\n"
        f"Объект: {SERVICE_LABELS[payload.service]}, {payload.object_area:g} м²\n"
        f"Локация: {payload.location}\n"
        f"График: {FREQUENCY_LABELS[payload.frequency]}\n"
        f"Старт: {URGENCY_LABELS[payload.urgency]}\n"
        f"Оценка лида: {qualification['score']}/100\n"
        f"Комментарий: {payload.message or '—'}"
    )


def intake_telegram_lead(
    db: Session,
    payload: LeadAutopilotCreate,
) -> dict[str, Any]:
    if not settings.public_leads_enabled:
        raise RuntimeError("Public lead intake requires COMPANY_LEGAL_NAME and a privacy contact email")
    if not payload.consent:
        raise ValueError("Consent to personal-data processing is required")
    phone = normalize_phone(payload.phone)
    email = str(payload.email or "").strip().lower()
    if not email and not phone:
        raise ValueError("Provide a valid email or Russian phone number")

    inbox_external_id = f"telegram-lead:{payload.conversation_id}"
    existing_inbox = db.scalar(
        select(InboxMessage).where(
            InboxMessage.channel == "telegram",
            InboxMessage.external_id == inbox_external_id,
        )
    )
    if existing_inbox and existing_inbox.record_id:
        existing_lead = db.get(BusinessRecord, existing_inbox.record_id)
        if existing_lead:
            return {
                "accepted": True,
                "lead_id": existing_lead.id,
                "status": existing_lead.status,
                "score": existing_lead.score,
                "estimate": (existing_lead.data or {}).get("preliminary_estimate") or {},
                "idempotent_replay": True,
            }

    pseudonymous_actor = f"telegram-lead:{payload.requester_key[:32]}"
    cutoff = now_utc() - timedelta(hours=1)
    attempts = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.actor == pseudonymous_actor,
            AuditLog.action == "lead_autopilot.accepted",
            AuditLog.created_at >= cutoff,
        )
    ) or 0
    if attempts >= settings.public_lead_rate_limit_per_hour:
        raise LeadRateLimitExceeded("Too many requests; try again later")

    contact_key = email or phone
    contact_digest = hashlib.sha256(contact_key.encode()).hexdigest()
    external_id = f"telegram:{contact_digest}"
    qualification = qualify_lead(payload)
    estimate = preliminary_estimate(payload)
    lead = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "lead",
            BusinessRecord.external_id == external_id,
        )
    )
    lead_data = {
        "name": payload.name,
        "company": payload.company,
        "phone": f"+{phone}" if phone else "",
        "email": email,
        "telegram_username": payload.telegram_username,
        "service": payload.service,
        "object_area": payload.object_area,
        "location": payload.location,
        "frequency": payload.frequency,
        "urgency": payload.urgency,
        "message": payload.message,
        "consent": True,
        "consent_version": "telegram-lead-v1",
        "consent_at": now_utc().isoformat(),
        "preliminary_estimate": estimate,
    }
    created = lead is None
    if lead is None:
        lead = BusinessRecord(
            record_type="lead",
            external_id=external_id,
            title=payload.company or payload.name,
            status=qualification["status"],
            score=qualification["score"],
            source="telegram_lead_autopilot",
            data=lead_data,
        )
        db.add(lead)
        db.flush()
    else:
        lead.title = payload.company or payload.name
        lead.data = {**(lead.data or {}), **lead_data}
        lead.score = max(float(lead.score or 0), qualification["score"])
        if lead.score >= settings.hot_lead_score and lead.status == "new":
            lead.status = "qualified"

    contact_summary = (
        f"{SERVICE_LABELS[payload.service]}; {payload.object_area:g} м²; "
        f"{payload.location}; {FREQUENCY_LABELS[payload.frequency]}; "
        f"{URGENCY_LABELS[payload.urgency]}. {payload.message}"
    ).strip()
    contact = ContactEvent(
        record_id=lead.id,
        channel="telegram",
        direction="inbound",
        subject=f"Заявка: {SERVICE_LABELS[payload.service]}",
        body=contact_summary[:4000],
        outcome="hot_lead" if qualification["hot"] else "received",
    )
    db.add(contact)
    db.flush()
    inbox = InboxMessage(
        channel="telegram",
        external_id=inbox_external_id,
        sender=email or (f"+{phone}" if phone else payload.name),
        recipient=settings.company_email,
        subject=f"Telegram-заявка: {SERVICE_LABELS[payload.service]}",
        body=contact_summary[:4000],
        record_id=lead.id,
        data={
            "score": qualification["score"],
            "consent": True,
            "telegram_username": payload.telegram_username,
            "estimate_status": estimate.get("status"),
        },
    )
    db.add(inbox)
    db.flush()

    event_bus.publish(
        db,
        "lead.created" if created else "lead.contact_received",
        "lead",
        str(lead.id),
        {
            "source": lead.source,
            "score": lead.score,
            "hot": qualification["hot"],
            "channel": "telegram",
        },
        idempotency_key=f"telegram-lead-contact:{payload.conversation_id}",
        actor="lead_autopilot",
    )
    audit(
        db,
        pseudonymous_actor,
        "lead_autopilot.accepted",
        "lead",
        str(lead.id),
        {
            "source": "telegram",
            "score": qualification["score"],
            "created": created,
            "estimate_status": estimate.get("status"),
        },
    )

    task_title = f"Telegram lead #{lead.id} · contact #{contact.id}"
    task = db.scalar(select(Task).where(Task.title == task_title))
    if task is None:
        task = Task(
            title=task_title,
            agent_type="sales",
            priority=qualification["priority"],
            payload={
                "record_id": lead.id,
                "reason": "telegram_lead_autopilot",
                "next_action": "contact_and_schedule_site_survey",
                "external_send": False,
            },
        )
        db.add(task)
        db.flush()
        record_task_created(db, task, actor="lead_autopilot", reason="qualified_telegram_lead")

    notification_body = _notification_body(payload, lead, qualification)
    telegram_notification = queue_owner_notification(
        db,
        idempotency_key=f"telegram-lead:{payload.conversation_id}:owner-telegram",
        channel="telegram",
        resource_type="lead",
        resource_id=str(lead.id),
        subject=("🔥 Горячая заявка" if qualification["hot"] else "Новая заявка") + " из Telegram",
        body=notification_body,
        data={"lead_id": lead.id, "task_id": task.id},
        severity="high" if qualification["hot"] else "normal",
    )
    email_notification = None
    if qualification["hot"]:
        email_notification = queue_owner_notification(
            db,
            idempotency_key=f"telegram-lead:{payload.conversation_id}:owner-email",
            channel="email",
            resource_type="lead",
            resource_id=str(lead.id),
            subject=f"Горячая Telegram-заявка: {lead.title}",
            body=notification_body,
            data={"lead_id": lead.id, "task_id": task.id, "reply_to": email},
            severity="high",
        )

    db.commit()
    return {
        "accepted": True,
        "lead_id": lead.id,
        "task_id": task.id,
        "status": lead.status,
        "score": lead.score,
        "hot": qualification["hot"],
        "estimate": estimate,
        "owner_notifications": {
            "telegram": telegram_notification.status,
            "email": email_notification.status if email_notification else "not_required",
        },
        "idempotent_replay": False,
    }
