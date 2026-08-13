from __future__ import annotations

import hashlib
from datetime import datetime, time, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import ApprovalRequest, BusinessRecord, ContentItem, MediaAsset
from .notifications import queue_owner_notification
from .platform import approval_engine, event_bus
from .social_news import CleaningNewsItem, editorialize_news, fetch_cleaning_news


SOCIAL_CHANNELS = ("telegram", "vk", "odnoklassniki", "instagram")
LEGAL_REVIEW_CHANNELS = {"instagram"}
MOSCOW = ZoneInfo("Europe/Moscow")
VISUAL_APPROVAL_VERSION = 1
SOCIAL_SETUP_VERSION = 1

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


def _social_platform_state(channel: str) -> dict:
    if channel == "vk":
        public_url = settings.social_vk_url
        credentials_present = bool(settings.vk_community_id and settings.vk_community_token)
        missing = [
            name
            for name, configured in (
                ("SOCIAL_VK_URL", bool(public_url)),
                ("VK_COMMUNITY_ID", bool(settings.vk_community_id)),
                ("VK_COMMUNITY_TOKEN", bool(settings.vk_community_token)),
            )
            if not configured
        ]
        owner_steps = [
            "Создать или выбрать официальное сообщество VK и пройти проверку телефона/CAPTCHA.",
            "Подтвердить владельца сообщества и передать публичную ссылку без пароля от аккаунта.",
        ]
    elif channel == "odnoklassniki":
        public_url = settings.social_odnoklassniki_url
        credentials_present = bool(
            settings.odnoklassniki_group_id
            and settings.odnoklassniki_application_key
            and settings.odnoklassniki_session_secret
        )
        missing = [
            name
            for name, configured in (
                ("SOCIAL_ODNOKLASSNIKI_URL", bool(public_url)),
                ("ODNOKLASSNIKI_GROUP_ID", bool(settings.odnoklassniki_group_id)),
                ("ODNOKLASSNIKI_APPLICATION_KEY", bool(settings.odnoklassniki_application_key)),
                ("ODNOKLASSNIKI_SESSION_SECRET", bool(settings.odnoklassniki_session_secret)),
            )
            if not configured
        ]
        owner_steps = [
            "Создать или выбрать официальную группу Одноклассников и пройти проверку телефона/CAPTCHA.",
            "Подтвердить владельца группы и передать публичную ссылку без пароля от аккаунта.",
        ]
    elif channel == "telegram":
        public_url = settings.social_telegram_url
        credentials_present = bool(settings.telegram_bot_token and settings.telegram_social_chat_id)
        missing = [
            name
            for name, configured in (
                ("SOCIAL_TELEGRAM_URL", bool(public_url)),
                ("TELEGRAM_SOCIAL_CHAT_ID", bool(settings.telegram_social_chat_id)),
            )
            if not configured
        ]
        owner_steps = ["Создать или выбрать официальный Telegram-канал и назначить бота администратором."]
    elif channel == "instagram":
        public_url = settings.social_instagram_url
        credentials_present = False
        missing = ["LEGAL_REVIEW", "MANUAL_PUBLICATION_ONLY"]
        owner_steps = ["Пройти юридическую проверку площадки и подтвердить только ручной режим публикации."]
    else:
        raise ValueError(f"Unsupported social channel: {channel}")

    adapter_ready = channel in {"telegram", "vk"}
    return {
        "channel": channel,
        "public_url": public_url,
        "public_url_configured": bool(public_url),
        "credentials_present": credentials_present,
        "missing_configuration": missing,
        "status": (
            "integration_configuration_required"
            if missing
            else "configured_owner_approval_required"
            if adapter_ready
            else "credentials_present_adapter_required"
        ),
        "owner_steps": owner_steps,
        "system_steps": [
            "Подготовить название, описание, контакты, аватар и обложку в фирменном стиле.",
            "Привязать публикации к общему контент-плану и визуальному owner approval.",
            "Провести тестовую публикацию только после проверки адаптера и отдельного подтверждения.",
        ],
        "automatic_publication_enabled": adapter_ready and not missing,
    }


def prepare_social_account_setup(db: Session, *, channels: list[str]) -> dict:
    requested = list(dict.fromkeys(str(channel).strip().lower() for channel in channels if str(channel).strip()))
    if not requested:
        requested = ["vk", "odnoklassniki"]
    unknown = [channel for channel in requested if channel not in SOCIAL_CHANNELS]
    if unknown:
        raise ValueError("Unsupported social channels: " + ", ".join(unknown))

    created = 0
    updated = 0
    platforms: list[dict] = []
    for channel in requested:
        state = _social_platform_state(channel)
        external_id = f"social-account-setup:{channel}:v{SOCIAL_SETUP_VERSION}"
        record = db.scalar(
            select(BusinessRecord).where(
                BusinessRecord.record_type == "social_account_setup",
                BusinessRecord.external_id == external_id,
            )
        )
        if record is None:
            record = BusinessRecord(
                record_type="social_account_setup",
                external_id=external_id,
                title=f"Оформление официальной страницы: {channel}",
                status=state["status"],
                source="marketing_agent",
                data={"setup_version": SOCIAL_SETUP_VERSION, **state},
            )
            db.add(record)
            created += 1
        else:
            record.status = state["status"]
            record.data = {**(record.data or {}), "setup_version": SOCIAL_SETUP_VERSION, **state}
            updated += 1
        db.flush()
        platforms.append({"record_id": record.id, **state})

    return {
        "status": "setup_in_progress",
        "action": "prepare_social_account_setup",
        "requested_channels": requested,
        "records_created": created,
        "records_updated": updated,
        "external_accounts_created": 0,
        "platforms": platforms,
        "publication_started": False,
        "owner_approval_preserved": True,
        "message": (
            "Чек-листы оформления сохранены. Регистрация внешних аккаунтов, телефонная проверка и CAPTCHA "
            "должны быть выполнены владельцем; система не выдаёт их за завершённые действия."
        ),
        "evidence": [
            {
                "type": "social_account_setup_records",
                "record_ids": [platform["record_id"] for platform in platforms],
                "channels": requested,
                "external_accounts_created": 0,
            }
        ],
    }


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


def _visual_prompt(title: str, body: str) -> str:
    return (
        "Use case: ads-marketing\n"
        "Asset type: square social media post image for a professional cleaning company\n"
        f"Primary request: create an original realistic visual supporting the topic: {title}.\n"
        f"Editorial context: {body}\n"
        "Scene/backdrop: a clean, contemporary residential or business property in Saint Petersburg\n"
        "Subject: professional cleaning result, orderly common areas, credible operational detail\n"
        "Style/medium: photorealistic natural commercial photography, restrained and trustworthy\n"
        "Composition/framing: square composition, clear focal point, generous clean margins\n"
        "Lighting/mood: natural daylight, calm, premium but not luxurious\n"
        "Color palette: white, cool gray, muted blue accents\n"
        "Constraints: no people identifiable by face; no personal data; no bank details; no logos; no trademarks; no text; no watermark\n"
        "Avoid: staged stock-photo look, exaggerated shine, unsafe cleaning practices"
    )


def _absolute_public_url(value: str) -> str:
    if value.startswith("/"):
        return urljoin(settings.public_base_url.rstrip("/") + "/", value.lstrip("/"))
    return value


def _ready_asset(asset: MediaAsset) -> bool:
    metadata = asset.metadata_json or {}
    if asset.status != "ready" or not asset.public_url or not (
        metadata.get("visually_reviewed") or metadata.get("generation_verified")
    ):
        return False
    digest = str(metadata.get("sha256") or "")
    if len(digest) != 64:
        return False
    parsed = urlparse(_absolute_public_url(asset.public_url))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _batch_items(db: Session, batch: BusinessRecord) -> list[ContentItem]:
    item_ids = [int(value) for value in (batch.data or {}).get("content_item_ids", [])]
    if not item_ids:
        return []
    return list(db.scalars(select(ContentItem).where(ContentItem.id.in_(item_ids)).order_by(ContentItem.id)).all())


def _batch_assets(db: Session, batch: BusinessRecord) -> list[MediaAsset]:
    asset_ids = [int(value) for value in (batch.data or {}).get("visual_asset_ids", [])]
    if not asset_ids:
        return []
    return list(db.scalars(select(MediaAsset).where(MediaAsset.id.in_(asset_ids)).order_by(MediaAsset.id)).all())


def _preview_digest(items: list[ContentItem], assets: dict[int, MediaAsset]) -> str:
    lines: list[str] = []
    for item in items:
        asset_id = int((item.metrics or {}).get("visual_asset_id") or 0)
        asset = assets[asset_id]
        lines.append(
            "|".join(
                (
                    str(item.id),
                    item.channel,
                    item.scheduled_at.isoformat() if item.scheduled_at else "",
                    item.title,
                    item.body,
                    str(asset.id),
                    asset.public_url,
                    str((asset.metadata_json or {}).get("sha256") or ""),
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def social_batch_preview(db: Session, batch: BusinessRecord) -> dict:
    items = _batch_items(db, batch)
    assets = {asset.id: asset for asset in _batch_assets(db, batch)}
    posts: list[dict] = []
    for item in items:
        asset_id = int((item.metrics or {}).get("visual_asset_id") or 0)
        asset = assets.get(asset_id)
        posts.append(
            {
                "content_item_id": item.id,
                "channel": item.channel,
                "scheduled_at": item.scheduled_at.isoformat() if item.scheduled_at else None,
                "title": item.title,
                "body": item.body,
                "visual_asset_id": asset_id or None,
                "image_url": _absolute_public_url(asset.public_url) if asset and asset.public_url else "",
                "alt_text": asset.alt_text if asset else "",
                "visual_ready": bool(asset and _ready_asset(asset)),
            }
        )
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "approval_id": (batch.data or {}).get("visual_approval_id"),
        "preview_digest": (batch.data or {}).get("preview_digest", ""),
        "all_visuals_ready": bool(posts) and all(post["visual_ready"] for post in posts),
        "posts": posts,
    }


def validate_social_approval(db: Session, approval: ApprovalRequest) -> tuple[BusinessRecord, list[ContentItem]]:
    batch = db.get(BusinessRecord, int(approval.resource_id))
    if not batch or batch.record_type != "social_content_batch":
        raise ValueError("Social content batch not found")
    data = batch.data or {}
    if data.get("visual_approval_version") != VISUAL_APPROVAL_VERSION:
        raise ValueError("Visual preview approval is required")
    if int(data.get("visual_approval_id") or 0) != approval.id:
        raise ValueError("Approval does not match the latest visual preview")
    items = _batch_items(db, batch)
    assets = {asset.id: asset for asset in _batch_assets(db, batch)}
    if len(items) != len(data.get("content_item_ids", [])) or not assets:
        raise ValueError("Social preview is incomplete")
    if any(int((item.metrics or {}).get("visual_asset_id") or 0) not in assets for item in items):
        raise ValueError("A post is missing its visual")
    if not all(_ready_asset(asset) for asset in assets.values()):
        raise ValueError("Every visual must be ready and visually reviewed")
    digest = _preview_digest(items, assets)
    if digest != data.get("preview_digest") or digest != approval.payload.get("preview_digest"):
        raise ValueError("Post text or visual changed after the preview was prepared")
    return batch, items


def finalize_social_preview_batch(db: Session, batch_id: int) -> dict:
    batch = db.get(BusinessRecord, batch_id)
    if not batch or batch.record_type != "social_content_batch":
        raise ValueError("Social content batch not found")
    items = _batch_items(db, batch)
    assets = {asset.id: asset for asset in _batch_assets(db, batch)}
    if not items or not assets or any(int((item.metrics or {}).get("visual_asset_id") or 0) not in assets for item in items):
        return {"status": "visuals_pending", "batch_id": batch.id, "approval_id": None}
    if not all(_ready_asset(asset) for asset in assets.values()):
        return {"status": "visuals_pending", "batch_id": batch.id, "approval_id": None}

    digest = _preview_digest(items, assets)
    current_approval_id = int((batch.data or {}).get("visual_approval_id") or 0)
    current = db.get(ApprovalRequest, current_approval_id) if current_approval_id else None
    if current and current.status == "pending" and current.payload.get("preview_digest") == digest:
        return {"status": "pending_approval", "batch_id": batch.id, "approval_id": current.id, "preview_digest": digest}

    channels = sorted({item.channel for item in items})
    fixed = {
        "date": (batch.data or {}).get("date"),
        "channels": channels,
        "posts_per_channel": max((sum(item.channel == channel for item in items) for channel in channels), default=0),
        "content_item_ids": [item.id for item in items],
        "visual_asset_ids": sorted(assets),
        "preview_digest": digest,
        "visual_approval_version": VISUAL_APPROVAL_VERSION,
    }
    approval = approval_engine.request(
        db,
        "social_publication",
        "social_content_batch",
        str(batch.id),
        "marketing",
        fixed,
        "Owner must review every final image, exact caption, channel and schedule before publication",
    )
    batch.status = "pending_visual_approval"
    batch.data = {**(batch.data or {}), **fixed, "visual_approval_id": approval.id, "approval_id": approval.id}
    for item in items:
        item.status = "approval"
        item.metrics = {**(item.metrics or {}), "publication_status": "waiting_visual_owner_approval", "approval_id": approval.id}
    preview = social_batch_preview(db, batch)
    notification = queue_owner_notification(
        db,
        idempotency_key=f"social-visual-approval:{batch.external_id}:{digest}",
        channel="telegram",
        resource_type="social_content_batch",
        resource_id=str(batch.id),
        subject=f"📱 Визуальное согласование · {(batch.data or {}).get('date', '')}",
        body=(
            "Ниже показаны финальные изображения, точные тексты, площадки и время. "
            "Кнопка одобряет только этот неизменяемый набор. Любая правка потребует нового согласования. "
            "Instagram не публикуется автоматически и остаётся на юридической проверке."
        ),
        data={"approval_id": approval.id, "batch_id": batch.id, "preview_digest": digest, "preview_posts": preview["posts"]},
    )
    event_bus.publish(
        db,
        "marketing.social_preview_ready",
        "social_content_batch",
        str(batch.id),
        {**fixed, "approval_id": approval.id, "owner_notification_id": notification.id},
        idempotency_key=f"social-preview:{batch.external_id}:{digest}",
    )
    db.flush()
    return {"status": batch.status, "batch_id": batch.id, "approval_id": approval.id, "preview_digest": digest, "owner_notification_id": notification.id}


def _ensure_visual_workflow(db: Session, batch: BusinessRecord, items: list[ContentItem]) -> list[MediaAsset]:
    data = batch.data or {}
    existing = _batch_assets(db, batch)
    if data.get("visual_approval_version") == VISUAL_APPROVAL_VERSION and existing:
        return existing

    prior_approval_id = data.get("approval_id")
    prior_approval = db.get(ApprovalRequest, int(prior_approval_id)) if prior_approval_id else None
    if prior_approval and prior_approval.status == "pending":
        prior_approval.status = "rejected"
        prior_approval.decided_by = "system:visual-approval-upgrade"
        prior_approval.decision_note = "Superseded: every social post now requires a final visual preview"
        prior_approval.decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
    assets: list[MediaAsset] = []
    for slot in (1, 2):
        slot_items = [item for item in items if int((item.metrics or {}).get("slot") or 0) == slot]
        if not slot_items:
            continue
        source = slot_items[0]
        source_url = str((source.metrics or {}).get("source_url") or "")
        asset = MediaAsset(
            content_item_id=source.id,
            kind="image",
            title=f"Визуал {slot}: {source.title}"[:255],
            provider="openai_images",
            prompt=_visual_prompt(source.title, source.body.split("\n\n", 2)[1] if "\n\n" in source.body else source.body),
            alt_text=f"Иллюстрация к публикации «{source.title}»"[:500],
            status="queued",
            metadata_json={
                "batch_id": batch.id,
                "slot": slot,
                "channels": sorted(item.channel for item in slot_items),
                "visual_review_required": True,
                "source_url": source_url,
            },
        )
        db.add(asset)
        db.flush()
        assets.append(asset)
        for item in slot_items:
            item.status = "visual_pending"
            item.metrics = {
                **(item.metrics or {}),
                "visual_asset_id": asset.id,
                "publication_status": "visual_generation_pending",
                "automatic_publication_allowed": item.channel not in LEGAL_REVIEW_CHANNELS,
            }
    batch.status = "visuals_pending"
    batch.data = {
        **data,
        "visual_approval_version": VISUAL_APPROVAL_VERSION,
        "visual_asset_ids": [asset.id for asset in assets],
        "visual_approval_id": None,
        "approval_id": None,
        "superseded_approval_id": prior_approval_id,
    }
    event_bus.publish(
        db,
        "marketing.social_visuals_requested",
        "social_content_batch",
        str(batch.id),
        {"visual_asset_ids": [asset.id for asset in assets], "content_item_ids": [item.id for item in items]},
        idempotency_key=f"social-visual-request:{batch.external_id}:v{VISUAL_APPROVAL_VERSION}",
    )
    db.flush()
    return assets


def prepare_daily_social_plan(db: Session, *, day: datetime | None = None) -> dict:
    now = day or datetime.now(timezone.utc)
    aware_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    local_date = aware_now.astimezone(MOSCOW).date()
    batch_key = f"social-daily:{local_date.isoformat()}"
    existing = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "social_content_batch",
            BusinessRecord.external_id == batch_key,
        )
    )
    if existing:
        items = _batch_items(db, existing)
        assets = _ensure_visual_workflow(db, existing, items)
        return {
            "status": existing.status,
            "batch_id": existing.id,
            "approval_id": (existing.data or {}).get("visual_approval_id"),
            "content_item_ids": [item.id for item in items],
            "media_asset_ids": [asset.id for asset in assets],
            "created": 0,
            "visuals_created": len(assets),
            "evidence": [{"type": "social_plan_reused", "batch_id": existing.id}],
        }

    base_index = local_date.toordinal() * 2
    selected = (TOPICS[base_index % len(TOPICS)], TOPICS[(base_index + 1) % len(TOPICS)])
    batch = BusinessRecord(
        record_type="social_content_batch",
        external_id=batch_key,
        title=f"Контент-план на {local_date.strftime('%d.%m.%Y')}",
        status="visuals_pending",
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
                status="visual_pending",
                scheduled_at=scheduled_at,
                metrics={
                    "batch_id": batch.id,
                    "batch_key": batch_key,
                    "slot": slot,
                    "timezone": "Europe/Moscow",
                    "publication_status": "visual_generation_pending",
                    "automatic_publication_allowed": channel not in LEGAL_REVIEW_CHANNELS,
                },
            )
            db.add(item)
            db.flush()
            items.append(item)
    batch.data = {**batch.data, "content_item_ids": [item.id for item in items]}
    assets = _ensure_visual_workflow(db, batch, items)
    return {
        "status": batch.status,
        "batch_id": batch.id,
        "approval_id": None,
        "content_item_ids": [item.id for item in items],
        "media_asset_ids": [asset.id for asset in assets],
        "created": len(items),
        "visuals_created": len(assets),
        "evidence": [
            {
                "type": "social_plan_created",
                "batch_id": batch.id,
                "content_count": len(items),
                "visual_asset_ids": [asset.id for asset in assets],
                "approval_created": False,
            }
        ],
    }


def prepare_daily_cleaning_news_plan(
    db: Session,
    *,
    day: datetime | None = None,
    news_items: list[CleaningNewsItem] | None = None,
) -> dict:
    """Build a fact-bound daily social batch from current trade-news sources."""
    now = day or datetime.now(timezone.utc)
    aware_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    local_date = aware_now.astimezone(MOSCOW).date()
    batch_key = f"social-news-daily:{local_date.isoformat()}"
    existing = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "social_content_batch",
            BusinessRecord.external_id == batch_key,
        )
    )
    if existing:
        items = _batch_items(db, existing)
        assets = _ensure_visual_workflow(db, existing, items)
        return {
            "status": existing.status,
            "batch_id": existing.id,
            "approval_id": (existing.data or {}).get("visual_approval_id"),
            "content_item_ids": [item.id for item in items],
            "media_asset_ids": [asset.id for asset in assets],
            "created": 0,
            "visuals_created": len(assets),
            "evidence": [{"type": "cleaning_news_plan_reused", "batch_id": existing.id}],
        }

    candidates = news_items if news_items is not None else fetch_cleaning_news(now=aware_now.replace(tzinfo=None))
    used_urls = {
        str((item.metrics or {}).get("source_url"))
        for item in db.scalars(select(ContentItem).where(ContentItem.channel.in_(SOCIAL_CHANNELS))).all()
        if (item.metrics or {}).get("source_url")
    }
    selected = [item for item in candidates if item.source_url not in used_urls][:2]
    if not selected:
        return {
            "status": "news_unavailable",
            "reason": "No fresh, unused cleaning-industry news passed source validation",
            "created": 0,
            "visuals_created": 0,
            "credentials_required": False,
            "evidence": [{"type": "cleaning_news_sources_checked", "source_count": len(candidates)}],
        }

    batch = BusinessRecord(
        record_type="social_content_batch",
        external_id=batch_key,
        title=f"Новости клининга на {local_date.strftime('%d.%m.%Y')}",
        status="visuals_pending",
        source="marketing_news_agent",
        data={
            "date": local_date.isoformat(),
            "channels": list(SOCIAL_CHANNELS),
            "posts_per_channel": len(selected),
            "source_evidence": [item.evidence() for item in selected],
        },
    )
    db.add(batch)
    db.flush()
    items: list[ContentItem] = []
    hours = (10, 18)
    for slot, news in enumerate(selected, 1):
        title, summary, editor = editorialize_news(news)
        factual_body = f"{summary}\n\nИсточник: {news.source_name}\n{news.source_url}"
        for channel in SOCIAL_CHANNELS:
            item = ContentItem(
                channel=channel,
                title=title,
                body=_adapt(channel, title, factual_body),
                status="visual_pending",
                scheduled_at=_slot_utc(now, hours[slot - 1]),
                metrics={
                    "batch_id": batch.id,
                    "batch_key": batch_key,
                    "slot": slot,
                    "timezone": "Europe/Moscow",
                    "source_url": news.source_url,
                    "source_name": news.source_name,
                    "source_published_at": news.published_at.isoformat() if news.published_at else None,
                    "source_verified": True,
                    "editor": editor,
                    "publication_status": "visual_generation_pending",
                    "automatic_publication_allowed": channel not in LEGAL_REVIEW_CHANNELS,
                },
            )
            db.add(item)
            db.flush()
            items.append(item)
    batch.data = {**batch.data, "content_item_ids": [item.id for item in items]}
    assets = _ensure_visual_workflow(db, batch, items)
    return {
        "status": batch.status,
        "batch_id": batch.id,
        "approval_id": None,
        "content_item_ids": [item.id for item in items],
        "media_asset_ids": [asset.id for asset in assets],
        "created": len(items),
        "visuals_created": len(assets),
        "evidence": [{
            "type": "source_backed_cleaning_news_plan_created",
            "batch_id": batch.id,
            "source_urls": [item.source_url for item in selected],
            "visual_asset_ids": [asset.id for asset in assets],
        }],
    }
