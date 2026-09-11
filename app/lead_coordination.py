from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import BusinessRecord, Task
from .task_state import record_task_created


LEAD_SCOUT_PROFILES: dict[str, dict[str, Any]] = {
    "management_lead_scout": {
        "segment": "management_companies",
        "customer_profile": "УК, ТСЖ, ТСН и ЖСК с объектами жилой недвижимости",
        "source_focus": [
            "официальные реестры и сайты управляющих организаций",
            "публичные каталоги ЖКХ и карточки организаций",
            "официальные страницы с контактами",
        ],
        "traffic_channel": "management_directories",
    },
    "commercial_lead_scout": {
        "segment": "commercial_properties",
        "customer_profile": (
            "бизнес-центры, склады, производства, торговые объекты, клиники, гостиницы, школы и офисы, "
            "которым могут требоваться клининговые услуги"
        ),
        "source_focus": [
            "корпоративные сайты и контактные страницы",
            "публичные отраслевые каталоги и карты",
            "новости об открытии, переезде или расширении объектов",
        ],
        "traffic_channel": "commercial_web",
    },
    "tender_lead_scout": {
        "segment": "cleaning_procurement",
        "customer_profile": "заказчики открытых закупок, тендеров и запросов предложений на клининговые услуги",
        "source_focus": [
            "официальные закупочные площадки и ЕИС",
            "тендерные извещения и планы закупок",
            "публичные страницы закупок коммерческих компаний",
        ],
        "traffic_channel": "tenders",
    },
    "social_lead_scout": {
        "segment": "public_business_social",
        "customer_profile": (
            "организации с публичными бизнес-страницами и сообществами, где опубликована потребность "
            "в уборке, подрядчике, обслуживании объекта или открытии новой площадки"
        ),
        "source_focus": [
            "публичные бизнес-страницы VK и Одноклассников",
            "публичные Telegram-каналы и сообщества организаций",
            "публичные объявления и отраслевые сообщества",
        ],
        "traffic_channel": "public_social",
    },
}


def focused_scout_payload(agent_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    profile = LEAD_SCOUT_PROFILES.get(agent_type)
    if profile is None:
        raise ValueError(f"Unknown lead scout profile: {agent_type}")
    return {
        **payload,
        **profile,
        "scout_role": agent_type,
        "automatic_outreach": False,
    }


def coordinate_lead_scouts(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Queue one bounded, idempotent wave of specialized public-source scouts."""

    current = datetime.now(timezone.utc).replace(tzinfo=None)
    wave_key = str(payload.get("wave_key") or current.isoformat(timespec="minutes"))[:64]
    regions = payload.get("regions") or []
    if isinstance(regions, str):
        regions = [regions]
    regions = [str(region)[:100] for region in regions if str(region).strip()][:20]
    try:
        max_results = max(1, min(int(payload.get("max_results") or 20), 50))
    except (TypeError, ValueError):
        max_results = 20
    tasks: list[Task] = []
    reused: list[int] = []
    for agent_type, profile in LEAD_SCOUT_PROFILES.items():
        title = f"Lead scout · {agent_type} · {wave_key}"
        existing = db.scalar(select(Task).where(Task.title == title))
        if existing is not None:
            reused.append(existing.id)
            continue
        task = Task(
            title=title,
            agent_type=agent_type,
            status="queued",
            priority="high",
            run_after=current,
            max_attempts=3,
            payload={
                **profile,
                "action": "discover_public_business_leads",
                "scout_role": agent_type,
                "regions": regions,
                "max_results": max_results,
                "source": "lead_coordinator",
                "wave_key": wave_key,
                "automatic_outreach": False,
            },
        )
        db.add(task)
        db.flush()
        record_task_created(db, task, actor="lead_coordinator", reason="specialized_lead_scout_wave")
        tasks.append(task)
    return {
        "status": "coordinated",
        "wave_key": wave_key,
        "specialized_agents": list(LEAD_SCOUT_PROFILES),
        "tasks_created": [task.id for task in tasks],
        "tasks_reused": reused,
        "central_registry": {
            "lead_records": int(
                db.scalar(select(func.count(BusinessRecord.id)).where(BusinessRecord.record_type == "lead")) or 0
            ),
            "lead_reports": int(
                db.scalar(
                    select(func.count(BusinessRecord.id)).where(
                        BusinessRecord.record_type == "lead_discovery_report"
                    )
                )
                or 0
            ),
        },
        "coverage": {
            "profiles": len(LEAD_SCOUT_PROFILES),
            "regions": regions,
            "public_sources_only": True,
            "private_accounts_and_restricted_sources": "excluded",
            "claim_of_full_internet_coverage": False,
        },
        "external_messages_sent": False,
        "evidence": [
            {
                "type": "lead_scout_coordination",
                "wave_key": wave_key,
                "created_task_ids": [task.id for task in tasks],
                "reused_task_ids": reused,
            }
        ],
    }
