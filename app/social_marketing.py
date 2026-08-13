from __future__ import annotations

import hashlib
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BusinessRecord, ContentItem
from .notifications import queue_owner_notification
from .platform import approval_engine, event_bus


SOCIAL_CHANNELS = ("telegram", "vk", "odnoklassniki", "instagram")
MOSCOW = ZoneInfo("Europe/Moscow")

TOPICS = (
    (
        "Как поддерживать чистоту общих зон",
        "Регулярная уборка входных групп, лифтов и лестничных площадок снижает количество срочных заявок и помогает сохранять аккуратный вид объекта. CleaningAIOS формирует график работ под фактическую нагрузку и фиксирует результат по чек-листу.",
    ),
    (
        "Что входит в контроль качества клининга",
        "Качественный клининг — это измеримый процесс: понятный перечень зон, частота операций, ответственный сотрудник и контроль устранения замечаний. Такой подход позволяет управляющей компании видеть результат, а не только факт выхода персонала.",
    ),
    (
        "Уборка в сезон высокой нагрузки",
        "В дождь и снег входные группы требуют другого графика: чаще удалять влагу и песок, контролировать противоскользящие покрытия и быстро пополнять расходные материалы. Сезонный план помогает сохранить безопасность и внешний вид объекта.",
    ),
    (
        "Как рассчитать клининг объекта",
        "Стоимость зависит не только от площади. В расчёте учитываются типы зон, проходимость, график, требования к технике, расходным материалам и уровню контроля. Предварительный аудит помогает предложить реалистичный состав работ без скрытых операций.",
    ),
    (
        "Почему чек-лист важнее общего обещания",
        "Чек-лист переводит ожидания заказчика в проверяемые действия: что, где и когда должно быть выполнено. По нему проще принимать работу, разбирать замечания и улучшать процесс без споров о формулировках.",
    ),
    (
        "Чистота бизнес-центра и впечатление арендаторов",
        "Первое впечатление формируют входная группа, санузлы и зоны общего пользования. Стабильный график, дневной дежурный персонал и оперативная реакция на инциденты помогают поддерживать сервисный уровень в течение всего дня.",
    ),
)


def _slot_utc(day: datetime, hour: int) -> datetime:
    aware = day.replace(tzinfo=timezone.utc) if day.tzinfo is None else day
    local = datetime.combine(aware.astimezone(MOSCOW).date(), time(hour=hour), tzinfo=MOSCOW)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _adapt(channel: str, title: str, body: str) -> str:
    suffix = {
        "telegram": "\n\nХотите получить чек-лист для вашего объекта? Напишите нам в ответ.",
        "vk": "\n\nРасскажите о типе объекта — подготовим перечень работ для предварительной оценки. #клининг #управляющаякомпания",
        "odnoklassniki": "\n\nКакая зона на вашем объекте требует больше всего внимания? Поделитесь в комментариях.",
        "instagram": "\n\nСохраняйте памятку и задавайте вопросы в сообщениях. #клинингспб #уборка #чистыйдом",
    }[channel]
    return f"{title}\n\n{body}{suffix}"


def prepare_daily_social_plan(db: Session, *, day: datetime | None = None) -> dict:
    now = day or datetime.now(timezone.utc)
    aware_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    local_date = aware_now.astimezone(MOSCOW).date()
    batch_key = f"social-daily:{local_date.isoformat()}"
    existing = db.scalar(select(BusinessRecord).where(
        BusinessRecord.record_type == "social_content_batch",
        BusinessRecord.external_id == batch_key,
    ))
    if existing:
        return {
            "status": existing.status,
            "batch_id": existing.id,
            "approval_id": (existing.data or {}).get("approval_id"),
            "content_item_ids": (existing.data or {}).get("content_item_ids", []),
            "created": 0,
            "evidence": [{"type": "social_plan_reused", "batch_id": existing.id}],
        }

    base_index = local_date.toordinal() * 2
    selected = (TOPICS[base_index % len(TOPICS)], TOPICS[(base_index + 1) % len(TOPICS)])
    batch = BusinessRecord(
        record_type="social_content_batch",
        external_id=batch_key,
        title=f"Контент-план на {local_date.strftime('%d.%m.%Y')}",
        status="pending_approval",
        source="marketing_agent",
        data={"date": local_date.isoformat(), "channels": list(SOCIAL_CHANNELS), "posts_per_channel": 2},
    )
    db.add(batch)
    db.flush()
    items: list[ContentItem] = []
    for slot, ((title, body), hour) in enumerate(zip(selected, (10, 18)), 1):
        scheduled_at = _slot_utc(now, hour)
        for channel in SOCIAL_CHANNELS:
            item = ContentItem(
                channel=channel,
                title=title,
                body=_adapt(channel, title, body),
                status="approval",
                scheduled_at=scheduled_at,
                metrics={
                    "batch_id": batch.id,
                    "batch_key": batch_key,
                    "slot": slot,
                    "timezone": "Europe/Moscow",
                    "publication_status": "waiting_owner_approval",
                },
            )
            db.add(item)
            db.flush()
            items.append(item)
    content_digest = hashlib.sha256("\n".join(f"{item.channel}|{item.scheduled_at.isoformat()}|{item.title}|{item.body}" for item in items).encode()).hexdigest()
    fixed = {
        "date": local_date.isoformat(),
        "channels": list(SOCIAL_CHANNELS),
        "posts_per_channel": 2,
        "content_item_ids": [item.id for item in items],
        "content_digest": content_digest,
    }
    approval = approval_engine.request(
        db,
        "social_publication",
        "social_content_batch",
        str(batch.id),
        "marketing",
        fixed,
        "Daily social content requires owner approval before scheduling or publication",
    )
    batch.data = {**batch.data, **fixed, "approval_id": approval.id}
    queue_owner_notification(
        db,
        idempotency_key=f"social-content-approval:{batch_key}",
        channel="telegram",
        resource_type="social_content_batch",
        resource_id=str(batch.id),
        subject=f"📱 Контент-план на {local_date.strftime('%d.%m')}",
        body=(
            "Подготовлено по 2 поста для Telegram, VK, Одноклассников и Instagram. "
            "Одобрение разрешает только постановку в график; публикация без credentials не имитируется."
        ),
        data={"approval_id": approval.id, "batch_id": batch.id},
    )
    event_bus.publish(db, "marketing.social_plan_prepared", "social_content_batch", str(batch.id), fixed, idempotency_key=f"social-plan:{batch_key}")
    db.flush()
    return {
        "status": batch.status,
        "batch_id": batch.id,
        "approval_id": approval.id,
        "content_item_ids": [item.id for item in items],
        "created": len(items),
        "evidence": [{"type": "social_plan_created", "batch_id": batch.id, "content_count": len(items), "content_digest": content_digest}],
    }
