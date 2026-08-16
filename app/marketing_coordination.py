from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .management_companies import audit_management_company_contacts
from .models import AuditLog, BusinessRecord, ContentItem, OutboundMessage, Task
from .task_state import record_task_created


ACTIVE_TASK_STATUSES = ("open", "queued", "running", "blocked")
FOLLOW_UP_LEAD_STATUSES = {"new", "qualified", "follow_up"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _has_inbound_consent(lead: BusinessRecord) -> bool:
    return bool((lead.data or {}).get("consent") is True)


def _contact_channels(lead: BusinessRecord) -> list[str]:
    data = lead.data or {}
    channels: list[str] = []
    if str(data.get("email") or "").strip():
        channels.append("email")
    if str(data.get("phone") or "").strip():
        channels.append("phone")
    if str(data.get("telegram_username") or "").strip():
        channels.append("telegram")
    return channels


def _follow_up_fingerprint(lead: BusinessRecord) -> str:
    data = lead.data or {}
    source = {
        "id": lead.id,
        "status": lead.status,
        "score": lead.score,
        "title": lead.title,
        "service": data.get("service"),
        "cleaning_kind": data.get("cleaning_kind"),
        "object_area": data.get("object_area"),
        "location": data.get("location"),
        "urgency": data.get("urgency"),
        "channels": _contact_channels(lead),
        "consent": _has_inbound_consent(lead),
    }
    return hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _ensure_task(
    db: Session,
    *,
    title: str,
    agent_type: str,
    payload: dict[str, Any],
    priority: str = "normal",
    reason: str,
) -> tuple[Task, bool]:
    existing = db.scalar(select(Task).where(Task.title == title).order_by(Task.id.desc()))
    if existing:
        return existing, False
    task = Task(
        title=title,
        agent_type=agent_type,
        status="queued",
        priority=priority,
        payload=payload,
        max_attempts=3,
    )
    db.add(task)
    db.flush()
    record_task_created(db, task, actor="orchestrator", reason=reason)
    return task, True


def prepare_lead_generation_strategy(db: Session) -> dict[str, Any]:
    """Prepare a DB-grounded demand plan without launching ads or contacting anyone."""
    leads = list(
        db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "lead")).all()
    )
    sources = Counter(str(row.source or "unknown") for row in leads)
    qualified = [
        row
        for row in leads
        if row.status in FOLLOW_UP_LEAD_STATUSES
        and (row.status == "qualified" or float(row.score or 0) >= settings.hot_lead_score)
    ]
    attributed = [row for row in leads if str((row.data or {}).get("utm_campaign") or "").strip()]
    experiments = list(
        db.scalars(
            select(BusinessRecord).where(BusinessRecord.record_type == "marketing_experiment")
        ).all()
    )
    content_total = int(db.scalar(select(func.count()).select_from(ContentItem)) or 0)
    published_content = int(
        db.scalar(
            select(func.count()).select_from(ContentItem).where(ContentItem.status == "published")
        )
        or 0
    )

    priorities: list[dict[str, Any]] = []
    if not leads:
        priorities.append(
            {
                "priority": "high",
                "action": "Проверить путь заявки на сайте и в Telegram, затем подготовить один оффер для БЦ/УК с измеримой UTM-меткой.",
                "metric": "first_qualified_lead",
            }
        )
    if leads and len(attributed) < len(leads):
        priorities.append(
            {
                "priority": "high",
                "action": "Закрыть пробел атрибуции: у каждой новой заявки сохранять источник, кампанию и канал.",
                "metric": "lead_attribution_coverage",
            }
        )
    if not experiments or not any(row.status == "running" for row in experiments):
        priorities.append(
            {
                "priority": "normal",
                "action": "Подготовить один малый маркетинговый эксперимент; запуск бюджета остаётся только после подтверждения владельца.",
                "metric": "qualified_leads_per_channel",
            }
        )
    priorities.append(
        {
            "priority": "normal",
            "action": "Продолжать полезный контент о клининге с призывом оставить входящую заявку; публикация зависит от подключённых каналов.",
            "metric": "inbound_leads_from_content",
        }
    )
    if qualified:
        priorities.append(
            {
                "priority": "high",
                "action": "Передать Sales квалифицированные входящие заявки и подготовить персональные follow-up черновики.",
                "metric": "qualified_lead_follow_up_coverage",
            }
        )

    return {
        "status": "strategy_prepared",
        "objective": "Увеличивать число квалифицированных входящих лидов и конверсию в следующий шаг продаж.",
        "funnel": {
            "leads": len(leads),
            "qualified": len(qualified),
            "attributed": len(attributed),
            "sources": dict(sorted(sources.items())),
        },
        "experiments": {
            "total": len(experiments),
            "running": sum(row.status == "running" for row in experiments),
        },
        "content": {"total": content_total, "published": published_content},
        "priorities": priorities,
        "automatic_external_execution": False,
        "owner_approval_preserved": True,
        "evidence": [
            {
                "type": "marketing_funnel_snapshot",
                "lead_count": len(leads),
                "qualified_count": len(qualified),
                "attributed_count": len(attributed),
            }
        ],
    }


def prepare_lead_follow_up(db: Session, *, record_id: int, fingerprint: str = "") -> dict[str, Any]:
    """Create a reply draft for an inbound, consented lead; never send it."""
    lead = db.get(BusinessRecord, record_id)
    if not lead or lead.record_type != "lead":
        return {
            "status": "not_found",
            "record_id": record_id,
            "automatic_send": False,
            "evidence": [{"type": "lead_lookup", "found": False}],
        }
    channels = _contact_channels(lead)
    if not _has_inbound_consent(lead) or not channels:
        return {
            "status": "consent_or_contact_required",
            "record_id": lead.id,
            "has_inbound_consent": _has_inbound_consent(lead),
            "contact_channels": channels,
            "automatic_send": False,
            "evidence": [
                {
                    "type": "sales_follow_up_safety_gate",
                    "record_id": lead.id,
                    "allowed_to_prepare": False,
                }
            ],
        }

    data = dict(lead.data or {})
    name = str(data.get("name") or lead.title or "").strip()
    service = str(data.get("service") or data.get("cleaning_kind") or "клинингу").strip()
    location = str(data.get("location") or "").strip()
    area = data.get("object_area")
    details = [f"услуга: {service}"]
    if area:
        details.append(f"площадь: {area} м²")
    if location:
        details.append(f"локация: {location}")
    greeting = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    body = (
        f"{greeting}\n\nСпасибо за обращение в CleaningAIOS. Мы получили вашу заявку "
        f"({'; '.join(details)}). Чтобы подготовить точный расчёт и план работ, "
        "предлагаем уточнить удобное время для короткого звонка или осмотра объекта.\n\n"
        "Подскажите, пожалуйста, когда вам удобно связаться?"
    )
    actual_fingerprint = fingerprint or _follow_up_fingerprint(lead)
    draft = {
        "subject": "Уточнение по вашей заявке на клининг",
        "body": body,
        "channels": channels,
        "prepared_at": utcnow().isoformat(),
    }
    lead.data = {
        **data,
        "sales_follow_up_draft": draft,
        "sales_follow_up_fingerprint": actual_fingerprint,
        "next_action": "human_review_then_reply",
    }
    db.flush()
    return {
        "status": "draft_prepared",
        "record_id": lead.id,
        "draft": draft,
        "automatic_send": False,
        "human_review_required": True,
        "evidence": [
            {
                "type": "sales_follow_up_draft",
                "record_id": lead.id,
                "contact_channels": channels,
                "consent_source": "inbound_request",
            }
        ],
    }


def run_marketing_sales_coordination(
    db: Session,
    *,
    scheduled_window_start: str | None = None,
    period_minutes: int = 30,
) -> dict[str, Any]:
    """Run one durable Marketing/Research/Sales coordination round."""
    now = utcnow()
    day = now.date().isoformat()
    leads = list(
        db.scalars(
            select(BusinessRecord)
            .where(BusinessRecord.record_type == "lead")
            .order_by(BusinessRecord.id)
        ).all()
    )
    qualified = [
        row
        for row in leads
        if row.status in FOLLOW_UP_LEAD_STATUSES
        and (row.status == "qualified" or float(row.score or 0) >= settings.hot_lead_score)
    ]
    contactable = [row for row in qualified if _has_inbound_consent(row) and _contact_channels(row)]
    attribution = Counter(str(row.source or "unknown") for row in leads)
    lead_statuses = Counter(str(row.status or "unknown") for row in leads)
    experiments = list(
        db.scalars(
            select(BusinessRecord).where(BusinessRecord.record_type == "marketing_experiment")
        ).all()
    )
    management_coverage = audit_management_company_contacts(db, enrich_verified_websites=False)
    outbound_before = int(db.scalar(select(func.count()).select_from(OutboundMessage)) or 0)

    actions: list[dict[str, Any]] = []
    strategy_task, strategy_created = _ensure_task(
        db,
        title=f"Marketing lead strategy · {day}",
        agent_type="marketing",
        priority="high",
        payload={
            "action": "prepare_lead_generation_strategy",
            "source": "marketing_sales_coordination",
            "advisory_only": True,
            "external_actions_require_owner_approval": True,
        },
        reason="marketing_sales_strategy_action",
    )
    actions.append(
        {
            "agent": "marketing",
            "task_id": strategy_task.id,
            "status": "queued" if strategy_created else strategy_task.status,
            "action": "Подготовить стратегию привлечения и измеримые гипотезы по реальной воронке.",
            "created_now": strategy_created,
        }
    )

    if management_coverage["organizations"]:
        research_task, research_created = _ensure_task(
            db,
            title=f"Research contact coverage · {day}",
            agent_type="research",
            priority="normal",
            payload={
                "collection": "management_company_contacts",
                "source": "marketing_sales_coordination",
                "batch_limit": 20,
                "enrich_verified_websites": True,
            },
            reason="marketing_sales_contact_audit",
        )
        actions.append(
            {
                "agent": "research",
                "task_id": research_task.id,
                "status": "queued" if research_created else research_task.status,
                "action": "Проверить базу УК/ТСЖ и найти контакты на уже подтверждённых официальных сайтах.",
                "created_now": research_created,
            }
        )
    if management_coverage["organizations_missing_email"]:
        actions.append(
            {
                "agent": "research",
                "task_id": None,
                "status": "configuration_required",
                "action": "Подключить официальный поисковый адаптер для организаций без подтверждённого email/сайта.",
                "credentials_required": ["YANDEX_SEARCH_API_KEY", "YANDEX_CLOUD_FOLDER_ID"],
                "created_now": False,
            }
        )

    sales_created = 0
    for lead in contactable[:20]:
        fingerprint = _follow_up_fingerprint(lead)
        if str((lead.data or {}).get("sales_follow_up_fingerprint") or "") == fingerprint:
            continue
        sales_task, created = _ensure_task(
            db,
            title=f"Sales follow-up draft · lead #{lead.id} · {fingerprint[:12]}",
            agent_type="sales",
            priority="high",
            payload={
                "action": "prepare_lead_follow_up",
                "record_id": lead.id,
                "fingerprint": fingerprint,
                "source": "marketing_sales_coordination",
                "external_send": False,
            },
            reason="marketing_sales_follow_up_draft",
        )
        if created:
            sales_created += 1
        actions.append(
            {
                "agent": "sales",
                "task_id": sales_task.id,
                "status": "queued" if created else sales_task.status,
                "action": f"Подготовить персональный follow-up для входящего лида #{lead.id}; не отправлять автоматически.",
                "created_now": created,
            }
        )

    active_tasks = list(
        db.scalars(
            select(Task).where(
                Task.agent_type.in_(["marketing", "research", "sales"]),
                Task.status.in_(ACTIVE_TASK_STATUSES),
            )
        ).all()
    )
    active_by_agent = Counter(row.agent_type for row in active_tasks)
    outbound_after = int(db.scalar(select(func.count()).select_from(OutboundMessage)) or 0)
    discussion = [
        {
            "agent": "sales",
            "message": (
                f"В CRM {len(leads)} лидов: квалифицированных {len(qualified)}, "
                f"входящих с согласием и контактом {len(contactable)}. "
                f"Новых follow-up задач поставлено {sales_created}."
            ),
        },
        {
            "agent": "marketing",
            "message": (
                f"Источники лидов: {dict(sorted(attribution.items())) or {'нет данных': 0}}; "
                f"маркетинговых экспериментов {len(experiments)}, активных "
                f"{sum(row.status == 'running' for row in experiments)}. Стратегия сверена с Sales-воронкой."
            ),
        },
        {
            "agent": "research",
            "message": (
                f"В базе УК/ТСЖ {management_coverage['organizations']} организаций, "
                f"без email {management_coverage['organizations_missing_email']}, "
                f"для безопасной коммуникации доступны {management_coverage['send_eligible_addresses']}."
            ),
        },
        {
            "agent": "orchestrator",
            "message": (
                "Согласованы владельцы и следующие шаги. Внешние письма, реклама и расходы "
                "не запускаются этим раундом; consent, suppression, лимиты и подтверждение владельца сохранены."
            ),
        },
    ]
    decisions = [
        "Marketing использует фактическую Sales-воронку и атрибуцию как вход для стратегии.",
        "Sales готовит персональные черновики только для входящих лидов с согласием и доступным контактом.",
        "Research не угадывает контакты: недостающие данные требуют официального источника или поискового адаптера.",
        "Автоматическая массовая или холодная отправка не выполняется.",
    ]
    result = {
        "outcome": "completed",
        "report_kind": "marketing_sales_coordination",
        "generated_at": now.isoformat(),
        "scheduled_window_start": scheduled_window_start,
        "period_minutes": max(5, min(int(period_minutes), 24 * 60)),
        "participants": ["marketing", "research", "sales", "orchestrator"],
        "discussion": discussion,
        "decisions": decisions,
        "actions": actions,
        "funnel": {
            "leads": len(leads),
            "qualified": len(qualified),
            "contactable_with_inbound_consent": len(contactable),
            "statuses": dict(sorted(lead_statuses.items())),
            "sources": dict(sorted(attribution.items())),
        },
        "active_tasks": dict(sorted(active_by_agent.items())),
        "research": {
            "organizations": management_coverage["organizations"],
            "organizations_missing_email": management_coverage["organizations_missing_email"],
            "send_eligible_addresses": management_coverage["send_eligible_addresses"],
        },
        "automatic_outreach": False,
        "outbound_messages_created": max(0, outbound_after - outbound_before),
        "owner_approval_preserved": True,
        "evidence": [
            {
                "type": "marketing_sales_database_snapshot",
                "lead_count": len(leads),
                "qualified_count": len(qualified),
                "active_task_count": len(active_tasks),
            },
            {
                "type": "coordination_safety_gate",
                "automatic_outreach": False,
                "outbound_messages_created": max(0, outbound_after - outbound_before),
            },
        ],
    }
    db.add(
        AuditLog(
            actor="orchestrator",
            action="agents.marketing_sales_coordinated",
            resource_type="coordination_round",
            resource_id=str(scheduled_window_start or now.isoformat()),
            details={
                "participants": result["participants"],
                "action_task_ids": [row["task_id"] for row in actions if row.get("task_id")],
                "qualified_leads": len(qualified),
                "automatic_outreach": False,
            },
        )
    )
    db.flush()
    return result
