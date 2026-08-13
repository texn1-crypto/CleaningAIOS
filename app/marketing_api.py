from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ai_router import marketing_channel_status, media_provider, provider_catalog
from .config import settings
from .db import SessionLocal
from .models import BusinessRecord, CompanyRequisite, ContentItem, MediaAsset, OwnerNotification
from .notifications import (
    NotificationNotDelivered,
    NotificationNotFound,
    acknowledge_owner_notification,
    queue_owner_notification,
)
from .orchestrator import audit
from .platform import approval_engine, event_bus
from .schemas import CompanyRequisiteCreate, ContentItemUpdate, MarketingExperimentCreate, MarketingExperimentLaunch, MarketingInvoiceCreate, MarketingProviderCreate, MediaAssetCreate, MediaAssetUpdate
from .security import Principal, principal, require_role


router = APIRouter(prefix="/api", tags=["marketing operating system"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_optional_url(value: str) -> str:
    if not value:
        return ""
    if value.startswith("/static/"):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(422, "Only safe HTTP(S) or /static/ URLs are supported")
    return value


def _masked(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def requisite_view(row: CompanyRequisite) -> dict:
    return {
        "id": row.id,
        "profile_name": row.profile_name,
        "legal_name": row.legal_name,
        "inn": _masked(row.inn),
        "kpp": _masked(row.kpp),
        "ogrn": _masked(row.ogrn),
        "settlement_account": _masked(row.settlement_account),
        "currency": row.currency,
        "bank_name": row.bank_name,
        "bank_inn": _masked(row.bank_inn),
        "bank_address": row.bank_address,
        "bic": _masked(row.bic),
        "correspondent_account": _masked(row.correspondent_account),
        "legal_address": row.legal_address,
        "active": row.active,
    }


@router.get("/ai/providers")
def ai_providers(actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    return {"policy": "least_privilege", "providers": provider_catalog(), "blanket_access": False}


@router.post("/company/requisites", status_code=201)
def create_requisites(payload: CompanyRequisiteCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "owner")
    data = payload.model_dump()
    data["currency"] = "RUB" if data["currency"] == "RUR" else data["currency"]
    row = CompanyRequisite(**data)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Requisites profile already exists")
    audit(db, actor.subject, "company_requisites.created", "company_requisites", str(row.id), {"profile_name": row.profile_name, "inn_suffix": row.inn[-4:]})
    db.commit(); db.refresh(row)
    return requisite_view(row)


@router.get("/company/requisites")
def list_requisites(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    return [requisite_view(row) for row in db.scalars(select(CompanyRequisite).order_by(CompanyRequisite.id)).all()]


@router.post("/marketing/providers", status_code=201)
def create_marketing_provider(payload: MarketingProviderCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    website = _safe_optional_url(payload.website)
    row = BusinessRecord(
        record_type="marketing_provider",
        title=payload.name,
        status=payload.status,
        source=payload.platform,
        data={"platform": payload.platform, "contact": payload.contact, "website": website, "capabilities": payload.capabilities, "notes": payload.notes},
    )
    db.add(row); db.flush()
    event_bus.publish(db, "marketing.provider_created", "marketing_provider", str(row.id), {"platform": payload.platform, "status": row.status})
    audit(db, actor.subject, "marketing.provider_created", "marketing_provider", str(row.id), {"platform": payload.platform})
    db.commit(); db.refresh(row)
    return {"id": row.id, "name": row.title, "status": row.status, "platform": payload.platform}


@router.get("/marketing/providers")
def marketing_providers(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "marketing_provider").order_by(BusinessRecord.id.desc())).all()
    return [{"id": row.id, "name": row.title, "status": row.status, **row.data, "connection": marketing_channel_status(row.data.get("platform", "other"))} for row in rows]


@router.post("/marketing/experiments", status_code=201)
def create_experiment(payload: MarketingExperimentCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    existing = db.scalar(select(BusinessRecord.id).where(BusinessRecord.record_type == "marketing_experiment", BusinessRecord.external_id == payload.utm_campaign))
    if existing:
        raise HTTPException(409, "UTM campaign is already used by another experiment")
    row = BusinessRecord(record_type="marketing_experiment", external_id=payload.utm_campaign, title=payload.title, status="draft", source=payload.channel, data=payload.model_dump())
    db.add(row); db.flush()
    event_bus.publish(db, "marketing.experiment_created", "marketing_experiment", str(row.id), {"channel": payload.channel, "budget_limit": payload.budget_limit})
    audit(db, actor.subject, "marketing.experiment_created", "marketing_experiment", str(row.id), {"channel": payload.channel})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status, "utm_campaign": payload.utm_campaign, "connection": marketing_channel_status(payload.channel)}


@router.get("/marketing/experiments")
def experiments(db: Session = Depends(get_db), _: Principal = Depends(principal)):
    rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "marketing_experiment").order_by(BusinessRecord.id.desc())).all()
    return [{"id": row.id, "title": row.title, "status": row.status, **row.data, "connection": marketing_channel_status(row.source)} for row in rows]


@router.get("/marketing/experiments/{experiment_id}/analytics")
def experiment_analytics(experiment_id: int, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    row = db.get(BusinessRecord, experiment_id)
    if not row or row.record_type != "marketing_experiment":
        raise HTTPException(404, "Marketing experiment not found")
    leads = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "lead")).all()
    attributed = [lead for lead in leads if (lead.data or {}).get("utm_campaign") == row.external_id]
    hot = [lead for lead in attributed if (lead.score or 0) >= settings.hot_lead_score or lead.status == "qualified"]
    spent = float((row.data or {}).get("spent", 0) or 0)
    return {
        "experiment_id": row.id,
        "leads": len(attributed),
        "qualified_leads": len(hot),
        "spent": spent,
        "cost_per_lead": round(spent / len(attributed), 2) if attributed else None,
        "cost_per_qualified_lead": round(spent / len(hot), 2) if hot else None,
        "evidence": [{"lead_id": lead.id, "score": lead.score, "status": lead.status} for lead in attributed],
    }


@router.post("/marketing/experiments/{experiment_id}/launch")
def launch_experiment(experiment_id: int, payload: MarketingExperimentLaunch, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    row = db.get(BusinessRecord, experiment_id)
    if not row or row.record_type != "marketing_experiment":
        raise HTTPException(404, "Marketing experiment not found")
    fixed = {
        "title": row.title,
        "channel": row.source,
        "budget_limit": float(row.data.get("budget_limit", 0) or 0),
        "utm_campaign": row.external_id,
    }
    if fixed["budget_limit"] > 0 and not approval_engine.authorized(db, "financial", payload.approval_id, "marketing_experiment", str(row.id), fixed):
        approval = approval_engine.request(db, "financial", "marketing_experiment", str(row.id), actor.subject, fixed, "Запуск рекламного теста с бюджетом требует подтверждения владельца")
        row.status = "approval"
        notification = queue_owner_notification(
            db,
            idempotency_key=f"marketing-experiment-approval:{approval.id}",
            channel="telegram",
            resource_type="marketing_experiment",
            resource_id=str(row.id),
            subject=f"🔐 Рекламный бюджет: {row.title}",
            body=f"Канал: {row.source}\nЛимит: {fixed['budget_limit']:.2f} RUB\nПодтверждение разрешает тест, но не выполняет оплату автоматически.",
            data={"approval_id": approval.id},
        )
        db.commit()
        return {"status": "waiting_approval", "approval_id": approval.id, "telegram_notification": notification.status}
    if not payload.external_campaign_id:
        db.commit()
        return {"status": "approved_waiting_manual_activation", "connection": marketing_channel_status(row.source), "required": "external_campaign_id"}
    row.status = "running"
    row.data = {**row.data, "external_campaign_id": payload.external_campaign_id, "started_at": now_utc().isoformat()}
    event_bus.publish(db, "marketing.experiment_started", "marketing_experiment", str(row.id), {"external_campaign_id": payload.external_campaign_id, "channel": row.source})
    audit(db, actor.subject, "marketing.experiment_started", "marketing_experiment", str(row.id), {"external_campaign_id": payload.external_campaign_id})
    db.commit()
    return {"status": row.status, "external_campaign_id": payload.external_campaign_id, "automatic_spend": False}


@router.post("/marketing/invoices", status_code=201)
def create_marketing_invoice(payload: MarketingInvoiceCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    provider = db.get(BusinessRecord, payload.provider_id)
    if not provider or provider.record_type != "marketing_provider":
        raise HTTPException(404, "Marketing provider not found")
    requisites = db.get(CompanyRequisite, payload.requisites_profile_id)
    if not requisites or not requisites.active:
        raise HTTPException(404, "Active requisites profile not found")
    document_url = _safe_optional_url(payload.document_url)
    row = BusinessRecord(
        record_type="marketing_invoice",
        external_id=f"{payload.provider_id}:{payload.invoice_number}",
        title=f"Счёт {payload.invoice_number} · {provider.title}",
        status="pending_approval",
        source=provider.source,
        deadline_at=payload.due_at,
        data={**payload.model_dump(mode="json"), "document_url": document_url, "provider_name": provider.title, "requisites_profile": requisites.profile_name},
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This provider invoice is already registered")
    approval_payload = {"invoice_number": payload.invoice_number, "provider_id": provider.id, "amount": payload.amount, "currency": payload.currency, "requisites_profile_id": requisites.id}
    approval = approval_engine.request(db, "financial", "marketing_invoice", str(row.id), actor.subject, approval_payload, "Рекламный счёт требует решения владельца; автоматическая оплата запрещена")
    notification = queue_owner_notification(
        db,
        idempotency_key=f"marketing-invoice-approval:{approval.id}",
        channel="telegram",
        resource_type="marketing_invoice",
        resource_id=str(row.id),
        subject=f"🧾 Рекламный счёт №{payload.invoice_number}",
        body=f"Поставщик: {provider.title}\nСумма: {payload.amount:.2f} {payload.currency}\nНазначение: {payload.description or 'не указано'}\nОдобрение не переводит деньги — оплату выполняет владелец вручную.",
        data={"approval_id": approval.id, "invoice_id": row.id},
    )
    event_bus.publish(db, "marketing.invoice_received", "marketing_invoice", str(row.id), {"provider_id": provider.id, "amount": payload.amount, "approval_id": approval.id})
    audit(db, actor.subject, "marketing.invoice_received", "marketing_invoice", str(row.id), {"provider_id": provider.id, "amount": payload.amount})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status, "approval_id": approval.id, "telegram_notification": "queued" if notification.status == "queued" else "credentials_required", "automatic_payment": False}


@router.get("/marketing/invoices")
def marketing_invoices(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "marketing_invoice").order_by(BusinessRecord.id.desc())).all()
    return [{"id": row.id, "title": row.title, "status": row.status, "deadline_at": row.deadline_at, "data": row.data, "automatic_payment": False} for row in rows]


@router.post("/marketing/media-assets", status_code=201)
def create_media_asset(payload: MediaAssetCreate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    if payload.content_item_id and not db.get(ContentItem, payload.content_item_id):
        raise HTTPException(404, "Content item not found")
    public_url = _safe_optional_url(payload.public_url)
    provider, generated_status = media_provider(payload.kind)
    row = MediaAsset(
        content_item_id=payload.content_item_id,
        kind=payload.kind,
        title=payload.title,
        provider=payload.provider or provider,
        prompt=payload.prompt,
        public_url=public_url,
        storage_path=payload.storage_path,
        alt_text=payload.alt_text,
        status=payload.status if public_url or payload.storage_path else generated_status,
        metadata_json=payload.metadata,
    )
    db.add(row); db.flush()
    event_bus.publish(db, "marketing.media_requested", "media_asset", str(row.id), {"kind": row.kind, "provider": row.provider, "status": row.status})
    audit(db, actor.subject, "marketing.media_requested", "media_asset", str(row.id), {"kind": row.kind, "provider": row.provider})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status, "provider": row.provider, "credentials_required": row.status in {"credentials_required", "adapter_required"}}


@router.patch("/marketing/media-assets/{asset_id}")
def update_media_asset(asset_id: int, payload: MediaAssetUpdate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    row = db.get(MediaAsset, asset_id)
    if not row:
        raise HTTPException(404, "Media asset not found")
    changes = payload.model_dump(exclude_unset=True)
    if "public_url" in changes and changes["public_url"] is not None:
        changes["public_url"] = _safe_optional_url(changes["public_url"])
    if "metadata" in changes:
        changes["metadata_json"] = {**row.metadata_json, **(changes.pop("metadata") or {})}
    for field, value in changes.items():
        setattr(row, field, value)
    if row.status == "published":
        if not row.public_url:
            raise HTTPException(422, "Published media requires public_url")
        row.published_at = row.published_at or now_utc()
    social_preview = None
    if row.kind == "image" and row.content_item_id:
        content_item = db.get(ContentItem, row.content_item_id)
        batch_id = int(((content_item.metrics if content_item else {}) or {}).get("batch_id") or 0)
        if batch_id:
            from .social_marketing import finalize_social_preview_batch

            social_preview = finalize_social_preview_batch(db, batch_id)
    event_bus.publish(db, "marketing.media_updated", "media_asset", str(row.id), {"status": row.status, "provider": row.provider})
    audit(db, actor.subject, "marketing.media_updated", "media_asset", str(row.id), {"status": row.status})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status, "provider": row.provider, "public_url": row.public_url, "social_preview": social_preview}


@router.get("/marketing/media-assets")
def media_assets(status: Optional[str] = None, db: Session = Depends(get_db), _: Principal = Depends(principal)):
    query = select(MediaAsset).order_by(MediaAsset.id.desc())
    if status:
        query = query.where(MediaAsset.status == status)
    rows = db.scalars(query.limit(500)).all()
    return [{"id": row.id, "content_item_id": row.content_item_id, "kind": row.kind, "title": row.title, "provider": row.provider, "status": row.status, "public_url": row.public_url, "alt_text": row.alt_text, "metadata": row.metadata_json} for row in rows]


@router.get("/marketing/social-batches/{batch_id}/preview")
def social_batch_preview(batch_id: int, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    batch = db.get(BusinessRecord, batch_id)
    if not batch or batch.record_type != "social_content_batch":
        raise HTTPException(404, "Social content batch not found")
    from .social_marketing import social_batch_preview as build_preview

    return build_preview(db, batch)


@router.get("/marketing/social-summary")
def social_summary(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "viewer")
    counts = {
        str(status): int(count)
        for status, count in db.execute(
            select(ContentItem.status, func.count(ContentItem.id))
            .where(ContentItem.channel.in_(["telegram", "vk", "odnoklassniki", "instagram"]))
            .group_by(ContentItem.status)
        ).all()
    }
    batches = db.scalars(
        select(BusinessRecord)
        .where(BusinessRecord.record_type == "social_content_batch")
        .order_by(BusinessRecord.id.desc())
        .limit(5)
    ).all()
    return {
        "content_statuses": counts,
        "integrations": {
            "telegram": "ready" if settings.telegram_bot_token and settings.telegram_social_chat_id else "credentials_required",
            "vk": "ready" if settings.vk_community_id and settings.vk_community_token else "credentials_required",
            "odnoklassniki": (
                "ready"
                if settings.odnoklassniki_group_id
                and settings.odnoklassniki_application_key
                and settings.odnoklassniki_access_token
                and settings.odnoklassniki_session_secret
                else "credentials_required"
            ),
            "instagram": "manual_legal_review_only",
            "images": (
                "ready"
                if settings.social_image_generation_enabled and settings.image_generation_api_key
                else "local_media_pool_ready"
            ),
        },
        "latest_batches": [
            {
                "id": row.id,
                "title": row.title,
                "status": row.status,
                "date": (row.data or {}).get("date"),
                "approval_id": (row.data or {}).get("visual_approval_id"),
                "source_urls": [
                    str(value.get("source_url") or "")
                    for value in (row.data or {}).get("source_evidence", [])
                    if isinstance(value, dict) and value.get("source_url")
                ],
            }
            for row in batches
        ],
        "publication_policy": "exact_owner_approved_image_caption_channel_schedule_only",
    }


@router.patch("/marketing/content/{content_id}")
def update_content(content_id: int, payload: ContentItemUpdate, db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "operator")
    row = db.get(ContentItem, content_id)
    if not row:
        raise HTTPException(404, "Content item not found")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("status") == "scheduled" and not (changes.get("scheduled_at") or row.scheduled_at):
        raise HTTPException(422, "Scheduled content requires scheduled_at")
    if "metrics" in changes:
        changes["metrics"] = {**row.metrics, **(changes["metrics"] or {})}
    for field, value in changes.items():
        setattr(row, field, value)
    if row.status == "published":
        row.published_at = row.published_at or now_utc()
    event_bus.publish(db, "marketing.content_updated", "content_item", str(row.id), {"status": row.status, "channel": row.channel})
    audit(db, actor.subject, "marketing.content_updated", "content_item", str(row.id), {"status": row.status})
    db.commit(); db.refresh(row)
    return {"id": row.id, "status": row.status, "published_at": row.published_at}


@router.get("/owner-notifications")
def owner_notifications(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    rows = db.scalars(select(OwnerNotification).order_by(OwnerNotification.id.desc()).limit(200)).all()
    return [{"id": row.id, "channel": row.channel, "resource_type": row.resource_type, "resource_id": row.resource_id, "subject": row.subject, "severity": row.severity, "correlation_id": row.correlation_id, "status": row.status, "attempts": row.attempts, "last_error": row.last_error, "sent_at": row.sent_at, "acknowledged_at": row.acknowledged_at, "acknowledged_by": row.acknowledged_by, "dead_lettered_at": row.dead_lettered_at} for row in rows]


@router.get("/owner-notifications/metrics")
def owner_notification_metrics(db: Session = Depends(get_db), actor: Principal = Depends(principal)):
    require_role(actor, "manager")
    rows = db.scalars(select(OwnerNotification)).all()
    statuses: dict[str, int] = {}
    severities: dict[str, int] = {}
    delivery_seconds: list[float] = []
    acknowledgement_required = 0
    acknowledged = 0
    for row in rows:
        statuses[row.status] = statuses.get(row.status, 0) + 1
        severities[row.severity] = severities.get(row.severity, 0) + 1
        if row.sent_at and row.created_at:
            delivery_seconds.append(max(0.0, (row.sent_at - row.created_at).total_seconds()))
        if row.severity in {"high", "critical"}:
            acknowledgement_required += 1
            acknowledged += int(row.acknowledged_at is not None)
    return {
        "total": len(rows),
        "statuses": statuses,
        "severities": severities,
        "acknowledgement_required": acknowledgement_required,
        "acknowledged": acknowledged,
        "acknowledgement_rate": round(
            acknowledged / acknowledgement_required, 4
        ) if acknowledgement_required else 1.0,
        "average_delivery_seconds": round(
            sum(delivery_seconds) / len(delivery_seconds), 3
        ) if delivery_seconds else None,
    }


@router.post("/owner-notifications/{notification_id}/acknowledge")
def acknowledge_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    actor: Principal = Depends(principal),
):
    require_role(actor, "owner")
    try:
        result = acknowledge_owner_notification(
            db,
            notification_id=notification_id,
            actor=actor.subject,
        )
    except NotificationNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except NotificationNotDelivered as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return result
