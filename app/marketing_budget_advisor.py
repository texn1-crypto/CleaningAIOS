from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .ai_router import marketing_channel_status
from .config import settings
from .models import ApprovalRequest, AuditLog, BusinessRecord, OwnerNotification
from .notifications import queue_owner_notification
from .platform import approval_engine, event_bus


HARD_DAILY_BUDGET_CAP_RUB = 2_000.0
OFFICIAL_CHANNEL_URLS = {
    "yandex_direct": "https://direct.yandex.ru/",
    "vk_ads": "https://ads.vk.com/",
    "telegram_ads": "https://ads.telegram.org/",
}
OFFICIAL_CHANNEL_DOMAINS = {
    "yandex_direct": "direct.yandex.ru",
    "vk_ads": "ads.vk.com",
    "telegram_ads": "ads.telegram.org",
}
PAID_CHANNEL_PRIORITY = ("yandex_direct", "vk_ads", "telegram_ads")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def verified_official_url(channel: str) -> str:
    """Return only a code-owned, HTTPS advertising console URL."""
    url = OFFICIAL_CHANNEL_URLS.get(channel, "")
    expected_domain = OFFICIAL_CHANNEL_DOMAINS.get(channel, "")
    parsed = urlparse(url)
    if (
        not url
        or parsed.scheme != "https"
        or parsed.hostname != expected_domain
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Advertising channel does not have a verified official URL")
    return url


def _channel_for_advice(experiments: list[BusinessRecord]) -> str:
    historical = Counter(
        row.source
        for row in experiments
        if row.source in OFFICIAL_CHANNEL_URLS and row.status in {"running", "completed"}
    )
    if historical:
        return sorted(historical, key=lambda item: (-historical[item], item))[0]
    for channel in PAID_CHANNEL_PRIORITY:
        if marketing_channel_status(channel)["credentials"] == "configured":
            return channel
    return "yandex_direct"


def _approval_for_advice(db: Session, advice_id: int) -> ApprovalRequest | None:
    return db.scalar(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.action_kind == "financial",
            ApprovalRequest.resource_type == "marketing_budget_advice",
            ApprovalRequest.resource_id == str(advice_id),
        )
        .order_by(ApprovalRequest.id.desc())
    )


def advice_view(db: Session, row: BusinessRecord) -> dict[str, Any]:
    approval = _approval_for_advice(db, row.id)
    return {
        "id": row.id,
        "day": row.external_id,
        "status": row.status,
        "approval_id": approval.id if approval else None,
        "approval_status": approval.status if approval else "missing",
        **(row.data or {}),
    }


def build_daily_marketing_budget_advice(
    db: Session,
    *,
    local_day: str | date | None = None,
    requested_daily_budget_rub: float | None = None,
    notify_owner: bool = True,
) -> dict[str, Any]:
    """Create one funnel-grounded, non-executing advertising recommendation per day."""
    day = (
        local_day.isoformat()
        if isinstance(local_day, date)
        else date.fromisoformat(str(local_day)).isoformat()
        if local_day
        else utcnow().date().isoformat()
    )
    external_id = f"daily:{day}"
    existing = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "marketing_budget_advice",
            BusinessRecord.external_id == external_id,
        )
    )
    if existing:
        return {**advice_view(db, existing), "created": False, "idempotent_replay": True}

    leads = list(
        db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "lead")).all()
    )
    experiments = list(
        db.scalars(
            select(BusinessRecord).where(BusinessRecord.record_type == "marketing_experiment")
        ).all()
    )
    qualified = [
        row
        for row in leads
        if row.status == "qualified" or float(row.score or 0) >= settings.hot_lead_score
    ]
    won = [row for row in leads if row.status == "won"]
    attributed = [row for row in leads if str((row.data or {}).get("utm_campaign") or "").strip()]
    channel = _channel_for_advice(experiments)
    official_url = verified_official_url(channel)
    requested = (
        settings.marketing_budget_daily_limit_rub
        if requested_daily_budget_rub is None
        else requested_daily_budget_rub
    )
    budget = round(max(0.0, min(float(requested), HARD_DAILY_BUDGET_CAP_RUB)), 2)
    target_cpl = 1_000.0
    expected_leads = round(budget / target_cpl, 2) if budget else 0.0
    qualification_rate = round(len(qualified) / len(leads), 4) if leads else 0.0
    expected_qualified = round(expected_leads * qualification_rate, 2)
    channel_connection = marketing_channel_status(channel)
    approval_payload = {
        "day": day,
        "channel": channel,
        "daily_budget_rub": budget,
        "official_url": official_url,
        "automatic_spend": False,
        "activation_mode": "manual_after_owner_approval",
    }
    data = {
        **approval_payload,
        "hypothesis": (
            "Тест официального рекламного кабинета с измеримой UTM-меткой увеличит "
            "число входящих заявок на клининг без автоматического расходования средств."
        ),
        "funnel_facts": {
            "leads": len(leads),
            "qualified": len(qualified),
            "won": len(won),
            "attributed": len(attributed),
            "qualification_rate": qualification_rate,
            "sources": dict(sorted(Counter(str(row.source or "unknown") for row in leads).items())),
        },
        "expected_kpi": {
            "primary": "qualified_leads",
            "target_cost_per_lead_rub_max": target_cpl,
            "estimated_leads_per_day": expected_leads,
            "estimated_qualified_leads_per_day": expected_qualified,
            "estimate_not_guarantee": True,
        },
        "official_domain_verified": True,
        "channel_credentials": channel_connection["credentials"],
        "payment_required_now": False,
        "owner_instructions": [
            "Проверьте гипотезу, KPI и сумму в Telegram и примите отдельное решение.",
            "После одобрения откройте официальный URL и вручную создайте кампанию.",
            "Оплату и включение рекламы выполните вручную в кабинете; CleaningAIOS деньги не списывает.",
        ],
        "created_at": utcnow().isoformat(),
    }
    row = BusinessRecord(
        record_type="marketing_budget_advice",
        external_id=external_id,
        title=f"Рекламная рекомендация · {day}",
        status="pending_approval",
        source="marketing_budget_advisor",
        data=data,
    )
    db.add(row)
    db.flush()
    approval = approval_engine.request(
        db,
        "financial",
        "marketing_budget_advice",
        str(row.id),
        "marketing_budget_advisor",
        approval_payload,
        "Одобрение разрешает только ручной рекламный тест в пределах 2 000 ₽/день; автоматическое списание запрещено.",
    )
    notification: OwnerNotification | None = None
    if notify_owner:
        notification = queue_owner_notification(
            db,
            idempotency_key=f"marketing-budget-advice:{external_id}:telegram",
            channel="telegram",
            resource_type="marketing_budget_advice",
            resource_id=str(row.id),
            subject=f"📊 Рекламный советник · {day}",
            body=(
                f"Канал: {channel}\nБюджет: {budget:.2f} RUB/день (лимит 2 000 RUB)\n"
                f"Воронка: лидов {len(leads)}, квалифицированных {len(qualified)}, сделок {len(won)}\n"
                f"Целевой CPL: не более {target_cpl:.2f} RUB\nОфициальный кабинет: {official_url}\n"
                "Одобрение не запускает рекламу и не списывает деньги: активация и оплата выполняются вручную."
            ),
            data={"approval_id": approval.id, "advice_id": row.id, "official_url": official_url},
            severity="high",
        )
    event_bus.publish(
        db,
        "marketing.budget_advice_created",
        "marketing_budget_advice",
        str(row.id),
        {"approval_id": approval.id, "daily_budget_rub": budget, "automatic_spend": False},
        idempotency_key=f"marketing-budget-advice:{external_id}:created",
        actor="marketing_budget_advisor",
    )
    db.add(
        AuditLog(
            actor="marketing_budget_advisor",
            action="marketing.budget_advice_created",
            resource_type="marketing_budget_advice",
            resource_id=str(row.id),
            details={
                "day": day,
                "channel": channel,
                "daily_budget_rub": budget,
                "automatic_spend": False,
                "approval_id": approval.id,
            },
        )
    )
    db.flush()
    return {
        **advice_view(db, row),
        "created": True,
        "idempotent_replay": False,
        "owner_notification": notification.status if notification else "disabled",
    }
