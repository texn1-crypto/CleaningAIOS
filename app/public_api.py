from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import AuditLog, BusinessRecord, ContactEvent, ContentItem, InboxMessage, MediaAsset, Task
from .notifications import queue_owner_notification
from .orchestrator import audit
from .platform import event_bus
from .schemas import PublicLeadCreate
from .task_state import record_task_created


router = APIRouter(prefix="/api/public", tags=["public website"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _phone_digits(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _safe_social_url(value: str) -> str:
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password else ""


@router.get("/social-media/{asset_id}/{filename}", include_in_schema=False)
def public_social_media(asset_id: int, filename: str, db: Session = Depends(get_db)):
    asset = db.get(MediaAsset, asset_id)
    metadata = (asset.metadata_json if asset else {}) or {}
    digest = str(metadata.get("sha256") or "")
    expected_names = {f"{digest}.png", f"{digest}.jpg"}
    if (
        not asset
        or asset.kind != "image"
        or asset.status not in {"ready", "published"}
        or len(digest) != 64
        or filename not in expected_names
        or not asset.storage_path
    ):
        raise HTTPException(404, "Social media image not found")
    root = (Path(settings.document_storage_path).resolve() / "social-media").resolve()
    path = Path(asset.storage_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(404, "Social media image not found") from exc
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise HTTPException(404, "Social media image not found")
    return FileResponse(path, media_type="image/png" if filename.endswith(".png") else "image/jpeg")


def _lead_score(payload: PublicLeadCreate) -> int:
    score = 20
    score += {"mcd": 20, "business_center": 20, "commercial": 15, "general": 8, "other": 3}[payload.service]
    score += {"today": 25, "week": 18, "month": 8, "planning": 2}[payload.urgency]
    if payload.object_area:
        score += 15 if payload.object_area >= 3000 else 10 if payload.object_area >= 1000 else 5
    if payload.budget:
        score += 20 if payload.budget >= 300_000 else 12 if payload.budget >= 100_000 else 5
    if payload.company:
        score += 5
    return min(100, score)


def _fingerprint(request: Request) -> str:
    client_host = request.client.host if request.client else "unknown"
    secret = settings.public_lead_rate_secret or settings.api_key
    return hmac.new(secret.encode(), client_host.encode(), hashlib.sha256).hexdigest()[:32]


@router.get("/site")
def public_site(db: Session = Depends(get_db)):
    news = db.scalars(
        select(ContentItem)
        .where(ContentItem.channel == "website", ContentItem.status == "published")
        .order_by(ContentItem.published_at.desc(), ContentItem.id.desc())
        .limit(12)
    ).all()
    media = db.scalars(
        select(MediaAsset)
        .where(MediaAsset.status.in_(["ready", "published"]), MediaAsset.public_url != "")
        .order_by(MediaAsset.id.desc())
        .limit(24)
    ).all()
    return {
        "company": {
            "name": settings.company_name,
            "phone": settings.company_phone,
            "email": settings.company_email,
            "service_area": settings.company_service_area,
            "social": {
                "telegram": _safe_social_url(settings.social_telegram_url),
                "vk": _safe_social_url(settings.social_vk_url),
                "odnoklassniki": _safe_social_url(settings.social_odnoklassniki_url),
                "instagram": _safe_social_url(settings.social_instagram_url),
            },
        },
        "lead_form": {
            "enabled": settings.public_leads_enabled,
            "status": "ready" if settings.public_leads_enabled else "legal_profile_required",
        },
        "news": [
            {
                "id": row.id,
                "title": row.title,
                "body": row.body,
                "published_at": row.published_at or row.created_at,
                "cover_url": (row.metrics or {}).get("cover_url", ""),
            }
            for row in news
        ],
        "media": [
            {"id": row.id, "kind": row.kind, "title": row.title, "url": row.public_url, "alt": row.alt_text}
            for row in media
        ],
    }


@router.post("/leads", status_code=status.HTTP_201_CREATED)
def create_public_lead(payload: PublicLeadCreate, request: Request, db: Session = Depends(get_db)):
    if payload.website:
        return {"accepted": True, "status": "received"}
    if not settings.public_leads_enabled:
        raise HTTPException(503, "Public lead form requires COMPANY_LEGAL_NAME and a privacy contact email")
    if not payload.consent:
        raise HTTPException(422, "Consent to personal-data processing is required")
    phone = _phone_digits(payload.phone)
    email = str(payload.email).lower() if payload.email else ""
    if not email and len(phone) not in {10, 11}:
        raise HTTPException(422, "Provide a valid email or Russian phone number")

    contact_key = email or phone
    contact_digest = hashlib.sha256(contact_key.encode()).hexdigest()
    fingerprint = _fingerprint(request)
    actor = f"website:{fingerprint}"
    cutoff = now_utc() - timedelta(hours=1)
    attempts = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.actor == actor,
            AuditLog.action == "public_lead.accepted",
            AuditLog.created_at >= cutoff,
        )
    ) or 0
    if attempts >= settings.public_lead_rate_limit_per_hour:
        raise HTTPException(429, "Too many requests; try again later")

    score = _lead_score(payload)
    external_id = f"website:{contact_digest}"
    lead = db.scalar(select(BusinessRecord).where(BusinessRecord.record_type == "lead", BusinessRecord.external_id == external_id))
    lead_data = {
        "name": payload.name,
        "company": payload.company,
        "phone": f"+{phone}" if phone else "",
        "email": email,
        "service": payload.service,
        "object_area": payload.object_area,
        "budget": payload.budget,
        "urgency": payload.urgency,
        "message": payload.message,
        "consent": True,
        "consent_version": "website-privacy-v1",
        "consent_at": now_utc().isoformat(),
        "utm_source": payload.utm_source,
        "utm_medium": payload.utm_medium,
        "utm_campaign": payload.utm_campaign,
    }
    created = lead is None
    if lead is None:
        lead = BusinessRecord(
            record_type="lead",
            external_id=external_id,
            title=payload.company or payload.name,
            status="qualified" if score >= settings.hot_lead_score else "new",
            score=score,
            source=payload.utm_source or "website",
            data=lead_data,
        )
        db.add(lead)
        db.flush()
    else:
        lead.title = payload.company or payload.name
        lead.data = {**lead.data, **lead_data}
        lead.score = max(float(lead.score or 0), score)
        if lead.score >= settings.hot_lead_score and lead.status == "new":
            lead.status = "qualified"

    contact = ContactEvent(
        record_id=lead.id,
        channel="web",
        direction="inbound",
        subject=f"Заявка: {payload.service}",
        body=payload.message,
        outcome="hot_lead" if score >= settings.hot_lead_score else "received",
    )
    db.add(contact)
    db.flush()
    inbox = InboxMessage(
        channel="web",
        external_id=f"website-lead:{lead.id}:{contact.id}",
        sender=email or (f"+{phone}" if phone else payload.name),
        recipient=settings.company_email,
        subject=f"Заявка с сайта: {payload.service}",
        body=payload.message,
        record_id=lead.id,
        data={"score": score, "utm_campaign": payload.utm_campaign, "consent": True},
    )
    db.add(inbox)
    db.flush()
    event_bus.publish(
        db,
        "lead.created" if created else "lead.contact_received",
        "lead",
        str(lead.id),
        {"source": lead.source, "score": lead.score, "hot": bool(lead.score >= settings.hot_lead_score)},
        idempotency_key=f"website-contact:{contact.id}",
    )
    audit(db, actor, "public_lead.accepted", "lead", str(lead.id), {"source": lead.source, "score": score, "created": created})

    notification = None
    if lead.score >= settings.hot_lead_score:
        title = f"Hot website lead #{lead.id}"
        if not db.scalar(select(Task.id).where(Task.title == title, Task.status.in_(["open", "queued", "running"]))):
            task = Task(title=title, agent_type="sales", priority="high", payload={"record_id": lead.id, "reason": "hot_website_lead"})
            db.add(task)
            db.flush()
            record_task_created(db, task, actor="public_lead_intake", reason="hot_lead_detected")
        notification = queue_owner_notification(
            db,
            idempotency_key=f"hot-lead-email:{lead.id}",
            channel="email",
            resource_type="lead",
            resource_id=str(lead.id),
            subject=f"Горячий лид с сайта: {lead.title}",
            body=(
                f"Лид #{lead.id}\nКомпания/имя: {lead.title}\nТелефон: {lead_data['phone']}\n"
                f"Email: {email}\nУслуга: {payload.service}\nСрочность: {payload.urgency}\n"
                f"Бюджет: {payload.budget or 'не указан'}\nСообщение: {payload.message or '—'}"
            ),
            data={"lead_id": lead.id, "score": lead.score},
        )
    db.commit()
    return {
        "accepted": True,
        "lead_id": lead.id,
        "status": "qualified" if lead.score >= settings.hot_lead_score else "received",
        "owner_notification": (
            "queued" if notification and notification.status == "queued" else
            "credentials_required" if notification else "not_required"
        ),
    }
