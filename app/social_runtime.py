from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .chat import redact_sensitive_text
from .config import settings
from .models import ApprovalRequest, ContentItem, MediaAsset
from .notifications import queue_owner_notification
from .orchestrator import audit
from .platform import event_bus
from .social_marketing import finalize_social_preview_batch


LOCAL_SOCIAL_MEDIA_POOL = (
    "services/business-center-lobby-v1.jpg",
    "services/residential-lobby-v1.jpg",
    "services/warehouse-machine-v1.jpg",
    "services/facade-territory-v1.jpg",
    "social/2026-08-13-business-center.png",
    "social/2026-08-13-checklist-quality.png",
    "social/2026-08-24-atrium-floorcare-v1.jpg",
    "social/2026-08-24-residential-entrance-v1.jpg",
    "social/2026-08-24-warehouse-scrubber-v1.jpg",
    "social/2026-08-24-refill-station-v1.jpg",
)

PRIVATE_IMAGE_PROMPT_PATTERN = re.compile(
    r"(?i)(?:[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})"
    r"|(?<!\d)(?:(?:\+?7|8)[\s()\-]*)?(?:\d[\s()\-]*){10}(?!\d)"
    r"|(?<!\d)\d{20}(?!\d)"
)


class DuplicateVisualError(ValueError):
    """A provider returned bytes that were already used by another visual."""


class LocalVisualPoolExhausted(ValueError):
    """Every original fallback visual has already been used."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _https_endpoint(base: str, path: str) -> str:
    # Concatenation preserves a provider base path (for example ``/v1``) and
    # keeps token-like path segments containing ``:`` inside the URL path.
    # ``urljoin`` would interpret ``bot123:secret`` as a new URI scheme.
    endpoint = f"{base.rstrip('/')}/{path.lstrip('/')}"
    return _public_https_url(endpoint)


def _public_https_url(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Provider endpoint must be public HTTPS")
    return endpoint


def _image_extension(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    raise ValueError("Image provider returned an unsupported file type")


def _used_visual_hashes(db: Session, *, exclude_asset_id: int) -> set[str]:
    values = db.scalars(
        select(MediaAsset.metadata_json).where(
            MediaAsset.kind == "image",
            MediaAsset.id != exclude_asset_id,
        )
    ).all()
    return {
        digest
        for value in values
        if (digest := str((value or {}).get("sha256") or ""))
    }


def _store_verified_image(
    db: Session,
    asset: MediaAsset,
    raw: bytes,
    *,
    provider: str,
    metadata: dict,
) -> None:
    if not raw or len(raw) > settings.max_attachment_bytes:
        raise ValueError("Generated image is empty or exceeds the configured size limit")
    extension = _image_extension(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if digest in _used_visual_hashes(db, exclude_asset_id=asset.id):
        raise DuplicateVisualError("The visual has already been used")
    root = Path(settings.document_storage_path).resolve()
    directory = root / "social-media"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{asset.id}-{digest[:16]}.{extension}"
    temporary = directory / f".{asset.id}-{digest[:16]}.tmp"
    temporary.write_bytes(raw)
    temporary.replace(destination)
    asset.provider = provider
    asset.storage_path = str(destination)
    asset.public_url = (
        ""
        if metadata.get("direct_request")
        else f"/api/public/social-media/{asset.id}/{digest}.{extension}"
    )
    asset.status = "ready"
    asset.metadata_json = {
        **metadata,
        "sha256": digest,
        "generation_verified": True,
        "generation_status": "ready_for_owner_preview",
        "generated_at": now_utc().isoformat(),
    }


def _use_local_media_pool(db: Session, asset: MediaAsset, metadata: dict) -> None:
    slot = int(metadata.get("slot") or 1)
    batch_id = int(metadata.get("batch_id") or 0)
    start = (batch_id + slot - 1) % len(LOCAL_SOCIAL_MEDIA_POOL)
    used_hashes = _used_visual_hashes(db, exclude_asset_id=asset.id)
    selected: tuple[str, bytes] | None = None
    for offset in range(len(LOCAL_SOCIAL_MEDIA_POOL)):
        relative_path = LOCAL_SOCIAL_MEDIA_POOL[(start + offset) % len(LOCAL_SOCIAL_MEDIA_POOL)]
        source = (Path(__file__).resolve().parent / "static" / relative_path).resolve()
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() not in used_hashes:
            selected = relative_path, raw
            break
    if selected is None:
        raise LocalVisualPoolExhausted("Every original fallback visual has already been used")
    relative_path, raw = selected
    _store_verified_image(db, asset, raw, provider="local_media_pool", metadata={
        **metadata,
        "media_pool_source": relative_path,
        "rights_basis": "original_project_asset",
        "unique_visual_enforced": True,
        "model": None,
    })


def queue_direct_image_request(db: Session, payload: dict) -> dict:
    """Create one auditable, idempotent image job for a Telegram request."""
    prompt = str(payload.get("prompt") or "").strip()[:4000]
    safe_prompt = redact_sensitive_text(prompt)
    if len(prompt) < 4:
        return {
            "status": "input_rejected",
            "reason": "Опишите, что должно быть изображено, минимум несколькими словами.",
            "evidence": [{"type": "image_prompt_rejected", "reason": "prompt_too_short"}],
        }
    if safe_prompt != prompt or PRIVATE_IMAGE_PROMPT_PATTERN.search(prompt):
        return {
            "status": "input_rejected",
            "reason": "Из запроса удалите email, телефоны, банковские реквизиты и любые секреты.",
            "evidence": [{"type": "image_prompt_rejected", "reason": "sensitive_data_detected"}],
        }
    if not settings.image_generation_configured:
        return {
            "status": "credentials_required",
            "reason": (
                "AI-генератор установлен, но не активирован: нужен отдельный OpenAI API key "
                "и IMAGE_GENERATION_ENABLED=true. Ключ нельзя отправлять сообщением в Telegram или чат."
            ),
            "credentials_required": ["IMAGE_GENERATION_API_KEY", "IMAGE_GENERATION_ENABLED"],
            "evidence": [{"type": "image_generation_configuration_check", "configured": False}],
        }

    request_key = str(payload.get("request_key") or "")[:255]
    if request_key:
        candidates = db.scalars(
            select(MediaAsset)
            .where(MediaAsset.kind == "image", MediaAsset.provider == "openai_images")
            .order_by(MediaAsset.id.desc())
            .limit(500)
        ).all()
        existing = next(
            (
                row
                for row in candidates
                if str((row.metadata_json or {}).get("request_key") or "") == request_key
            ),
            None,
        )
        if existing:
            return {
                "status": "ready" if existing.status == "ready" else "queued",
                "asset_id": existing.id,
                "record_id": existing.id,
                "idempotent_replay": True,
                "public_url": existing.public_url if existing.status == "ready" else "",
                "evidence": [{"type": "image_generation_reused", "media_asset_id": existing.id}],
            }

    asset = MediaAsset(
        kind="image",
        title=f"AI-изображение: {prompt[:220]}"[:255],
        provider="openai_images",
        prompt=prompt,
        alt_text="Изображение, созданное AI по запросу владельца"[:500],
        status="queued",
        metadata_json={
            "direct_request": True,
            "notify_owner": True,
            "request_key": request_key,
            "source": str(payload.get("source") or "telegram_natural_language")[:64],
            "external_publish": False,
        },
    )
    db.add(asset)
    db.flush()
    event_bus.publish(
        db,
        "marketing.image_generation_requested",
        "media_asset",
        str(asset.id),
        {"provider": asset.provider, "model": settings.image_generation_model},
        idempotency_key=f"direct-image-request:{request_key or asset.id}",
    )
    audit(
        db,
        "marketing",
        "marketing.image_generation_requested",
        "media_asset",
        str(asset.id),
        {"provider": asset.provider, "model": settings.image_generation_model},
    )
    return {
        "status": "queued",
        "asset_id": asset.id,
        "record_id": asset.id,
        "idempotent_replay": False,
        "external_publish": False,
        "evidence": [{"type": "image_generation_queued", "media_asset_id": asset.id}],
    }


def _queue_direct_image_notification(db: Session, asset: MediaAsset, *, succeeded: bool) -> None:
    metadata = dict(asset.metadata_json or {})
    if not metadata.get("direct_request") or not metadata.get("notify_owner"):
        return
    if succeeded:
        digest = str(metadata.get("sha256") or "")
        queue_owner_notification(
            db,
            idempotency_key=f"direct-image:{asset.id}:ready:{digest}",
            channel="telegram",
            resource_type="media_asset",
            resource_id=str(asset.id),
            subject=f"🎨 AI-изображение #{asset.id} готово",
            body="Файл создан и проверен. Он никуда не опубликован и доступен только как результат вашего запроса.",
            data={"media_asset_id": asset.id, "sha256": digest},
        )
        return
    queue_owner_notification(
        db,
        idempotency_key=f"direct-image:{asset.id}:failed:{asset.status}",
        channel="telegram",
        resource_type="media_asset",
        resource_id=str(asset.id),
        subject=f"⚠️ AI-изображение #{asset.id} не создано",
        body=(
            "Нужен новый IMAGE_GENERATION_API_KEY."
            if asset.status == "credentials_required"
            else "Провайдер или проверка файла завершились ошибкой. Ошибка записана в audit log."
        ),
        data={"generation_status": asset.status},
        severity="high" if asset.status == "credentials_required" else "normal",
    )


def generate_next_social_visual(db: Session) -> bool:
    asset = db.scalar(
        select(MediaAsset)
        .where(
            # ``imagegen`` was used by the original deterministic social-plan
            # workflow. Keep consuming those persisted jobs while all newly
            # created jobs use the canonical ``openai_images`` provider.
            MediaAsset.provider.in_(["imagegen", "openai_images", "local_media_pool"]),
            # Credential failures are terminal until an operator explicitly
            # fixes configuration and requeues the asset. This prevents a hot
            # retry loop against a rejected API key.
            MediaAsset.status == "queued",
        )
        .order_by(MediaAsset.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if asset is None:
        return False
    metadata = dict(asset.metadata_json or {})
    direct_request = bool(metadata.get("direct_request"))
    social_generation_configured = bool(
        settings.social_image_generation_enabled and settings.image_generation_api_key
    )
    if direct_request and not settings.image_generation_configured:
        asset.status = "credentials_required"
        asset.metadata_json = {
            **metadata,
            "generation_status": "credentials_required",
            "error_type": "ConfigurationRequired",
        }
        audit(
            db,
            "social_image_agent",
            "marketing.social_visual_failed",
            "media_asset",
            str(asset.id),
            {"generation_status": asset.status, "error_type": "ConfigurationRequired"},
        )
        _queue_direct_image_notification(db, asset, succeeded=False)
        db.commit()
        return True
    if asset.provider == "local_media_pool" or (not direct_request and not social_generation_configured):
        try:
            _use_local_media_pool(db, asset, metadata)
            batch_id = int(metadata.get("batch_id") or 0)
            if batch_id:
                finalize_social_preview_batch(db, batch_id)
            event_bus.publish(
                db,
                "marketing.social_visual_generated",
                "media_asset",
                str(asset.id),
                {"batch_id": batch_id, "provider": "local_media_pool", "sha256": asset.metadata_json["sha256"]},
                idempotency_key=f"social-visual-generated:{asset.id}:{asset.metadata_json['sha256']}",
            )
            audit(db, "social_image_agent", "marketing.social_visual_generated", "media_asset", str(asset.id), {"provider": "local_media_pool", "sha256": asset.metadata_json["sha256"]})
        except LocalVisualPoolExhausted as exc:
            asset.status = "credentials_required"
            asset.metadata_json = {
                **metadata,
                "generation_status": "credentials_required",
                "error_type": type(exc).__name__,
                "credentials_required": ["IMAGE_GENERATION_API_KEY", "SOCIAL_IMAGE_GENERATION_ENABLED"],
                "unique_visual_enforced": True,
            }
            batch_id = int(metadata.get("batch_id") or 0)
            audit(
                db,
                "social_image_agent",
                "marketing.social_visual_failed",
                "media_asset",
                str(asset.id),
                {"generation_status": asset.status, "error_type": type(exc).__name__},
            )
            queue_owner_notification(
                db,
                idempotency_key=f"social-visual-pool-exhausted:{batch_id or asset.id}",
                channel="telegram",
                resource_type="media_asset",
                resource_id=str(asset.id),
                subject="⚠️ Нужен новый источник уникальных изображений",
                body=(
                    "Все оригинальные fallback-фотографии уже использованы. Повтор не допущен; "
                    "для следующего визуала настройте IMAGE_GENERATION_API_KEY и "
                    "SOCIAL_IMAGE_GENERATION_ENABLED=true."
                ),
                data={"generation_status": asset.status, "batch_id": batch_id},
            )
        except (OSError, ValueError) as exc:
            asset.status = "generation_failed"
            asset.metadata_json = {**metadata, "generation_status": "generation_failed", "error_type": type(exc).__name__}
        db.commit()
        return True

    try:
        endpoint = _https_endpoint(settings.image_generation_base_url, "/images/generations")
        timeout = min(300, max(10, int(settings.image_generation_timeout_seconds)))
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {settings.image_generation_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.image_generation_model,
                    "prompt": asset.prompt,
                    "size": settings.image_generation_size,
                    "quality": settings.image_generation_quality,
                    "output_format": "png",
                },
            )
            response.raise_for_status()
        encoded = str(response.json()["data"][0]["b64_json"])
        raw = base64.b64decode(encoded, validate=True)
        provider_request_id = str(getattr(response, "headers", {}).get("x-request-id") or "")[:128]
        _store_verified_image(db, asset, raw, provider="openai_images", metadata={
            **metadata,
            "model": settings.image_generation_model,
            "provider_request_id": provider_request_id,
            "unique_visual_enforced": True,
        })
        digest = str(asset.metadata_json["sha256"])
        batch_id = int(metadata.get("batch_id") or 0)
        if batch_id:
            finalize_social_preview_batch(db, batch_id)
        event_bus.publish(
            db,
            "marketing.social_visual_generated",
            "media_asset",
            str(asset.id),
            {"batch_id": batch_id, "sha256": digest, "model": settings.image_generation_model},
            idempotency_key=f"social-visual-generated:{asset.id}:{digest}",
        )
        audit(db, "social_image_agent", "marketing.social_visual_generated", "media_asset", str(asset.id), {"sha256": digest})
        _queue_direct_image_notification(db, asset, succeeded=True)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        asset.status = "credentials_required" if status_code in {401, 403} else "generation_failed"
        asset.metadata_json = {
            **metadata,
            "generation_status": asset.status,
            "error_type": type(exc).__name__,
        }
        audit(
            db,
            "social_image_agent",
            "marketing.social_visual_failed",
            "media_asset",
            str(asset.id),
            {
                "generation_status": asset.status,
                "error_type": type(exc).__name__,
                "provider_status_code": status_code,
            },
        )
        _queue_direct_image_notification(db, asset, succeeded=False)
    db.commit()
    return True


def _approved_item(db: Session, item: ContentItem) -> bool:
    approval_id = int((item.metrics or {}).get("approval_id") or 0)
    approval = db.get(ApprovalRequest, approval_id) if approval_id else None
    return bool(
        approval
        and approval.status == "approved"
        and approval.action_kind == "social_publication"
        and approval.resource_type == "social_content_batch"
        and str((item.metrics or {}).get("batch_id") or "") == approval.resource_id
    )


def _item_asset(db: Session, item: ContentItem) -> MediaAsset | None:
    asset_id = int((item.metrics or {}).get("visual_asset_id") or 0)
    return db.get(MediaAsset, asset_id) if asset_id else None


def _telegram_caption(item: ContentItem) -> str:
    source_url = str((item.metrics or {}).get("source_url") or "")
    suffix = f"\n\nИсточник: {source_url}" if source_url and source_url not in item.body[-300:] else ""
    limit = max(0, 1024 - len(suffix))
    body = item.body if len(item.body) <= limit else item.body[: max(0, limit - 1)].rstrip() + "…"
    return body + suffix


def _verified_asset_bytes(asset: MediaAsset) -> bytes:
    if not asset.storage_path:
        raise ValueError("A locally verified image is required for this channel")
    root = Path(settings.document_storage_path).resolve()
    path = Path(asset.storage_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Social image is outside document storage") from exc
    raw = path.read_bytes()
    digest = str((asset.metadata_json or {}).get("sha256") or "")
    if len(digest) != 64 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Social image checksum mismatch")
    return raw


def _safe_provider_failure(exc: Exception) -> dict:
    details = {"error_type": type(exc).__name__}
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        details["provider_status_code"] = int(status_code)
    if response is not None:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}
        description = payload.get("description") if isinstance(payload, dict) else None
        if isinstance(description, str) and description:
            details["provider_error"] = redact_sensitive_text(description)[:300]
    return details


def _vk_call(client: httpx.Client, method: str, data: dict) -> dict:
    response = client.post(
        f"https://api.vk.com/method/{method}",
        data={**data, "access_token": settings.vk_community_token, "v": settings.vk_api_version},
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise ValueError(f"VK API rejected {method}")
    return payload["response"]


def _publish_vk(item: ContentItem, asset: MediaAsset) -> str:
    raw = _verified_asset_bytes(asset)
    group_id = int(settings.vk_community_id)
    with httpx.Client(timeout=30) as client:
        upload = _vk_call(client, "photos.getWallUploadServer", {"group_id": group_id})
        upload_url = _public_https_url(str(upload["upload_url"]))
        uploaded_response = client.post(upload_url, files={"photo": ("social.png", raw, "image/png")})
        uploaded_response.raise_for_status()
        uploaded = uploaded_response.json()
        saved = _vk_call(
            client,
            "photos.saveWallPhoto",
            {
                "group_id": group_id,
                "server": uploaded["server"],
                "photo": uploaded["photo"],
                "hash": uploaded["hash"],
            },
        )
        photo = saved[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        posted = _vk_call(
            client,
            "wall.post",
            {
                "owner_id": -abs(group_id),
                "from_group": 1,
                "message": item.body,
                "attachments": attachment,
                "guid": f"cleaningaios-content-{item.id}",
            },
        )
    return str(posted["post_id"])


def _ok_signature(data: dict[str, object]) -> str:
    signed = {
        str(key): str(value)
        for key, value in data.items()
        if key not in {"access_token", "session_key", "sig"}
    }
    canonical = "".join(f"{key}={signed[key]}" for key in sorted(signed))
    # Odnoklassniki's REST protocol requires this exact MD5 signature. It is
    # request interoperability only, never password hashing or a local security
    # decision, and the request is sent exclusively over HTTPS.
    return hashlib.md5(  # lgtm[py/weak-sensitive-data-hashing]
        (canonical + settings.odnoklassniki_session_secret).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


def _ok_call(client: httpx.Client, method: str, data: dict[str, object]) -> object:
    payload: dict[str, object] = {
        "method": method,
        "application_key": settings.odnoklassniki_application_key,
        "access_token": settings.odnoklassniki_access_token,
        "format": "json",
        **data,
    }
    payload["sig"] = _ok_signature(payload)
    response = client.post("https://api.ok.ru/fb.do", data=payload)
    response.raise_for_status()
    result = response.json()
    if isinstance(result, dict) and result.get("error_code"):
        try:
            code = int(result["error_code"])
        except (TypeError, ValueError):
            code = 0
        raise ValueError(f"Odnoklassniki API rejected {method} (code {code})")
    return result


def _publish_odnoklassniki(item: ContentItem, asset: MediaAsset) -> str:
    raw = _verified_asset_bytes(asset)
    extension = _image_extension(raw)
    content_type = "image/png" if extension == "png" else "image/jpeg"
    group_id = str(settings.odnoklassniki_group_id).strip()
    with httpx.Client(timeout=30) as client:
        upload = _ok_call(
            client,
            "photosV2.getUploadUrl",
            {"gid": group_id, "count": 1, "sizes": len(raw)},
        )
        if not isinstance(upload, dict):
            raise ValueError("Odnoklassniki did not return a photo upload URL")
        upload_url = _public_https_url(str(upload.get("upload_url") or ""))
        uploaded_response = client.post(
            upload_url,
            files={"pic1": (f"social.{extension}", raw, content_type)},
        )
        uploaded_response.raise_for_status()
        uploaded = uploaded_response.json()
        photos = uploaded.get("photos") if isinstance(uploaded, dict) else None
        first_photo = next(iter(photos.values()), None) if isinstance(photos, dict) else None
        token = first_photo.get("token") if isinstance(first_photo, dict) else None
        if not token:
            raise ValueError("Odnoklassniki did not return a photo token")
        attachment = json.dumps(
            {
                "media": [
                    {"type": "photo", "list": [{"id": str(token)}]},
                    {"type": "text", "text": item.body},
                ],
                "onBehalfOfGroup": "true",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        topic_id = _ok_call(
            client,
            "mediatopic.post",
            {"type": "GROUP_THEME", "gid": group_id, "attachment": attachment},
        )
    if not isinstance(topic_id, (str, int)) or not str(topic_id).strip():
        raise ValueError("Odnoklassniki did not return a topic id")
    return str(topic_id)


def _channel_credentials_ready(channel: str) -> bool:
    if channel == "telegram":
        return bool(settings.telegram_bot_token and settings.telegram_social_chat_id)
    if channel == "vk":
        return bool(settings.vk_community_id and settings.vk_community_token)
    if channel == "odnoklassniki":
        return bool(
            settings.odnoklassniki_group_id
            and settings.odnoklassniki_application_key
            and settings.odnoklassniki_access_token
            and settings.odnoklassniki_session_secret
        )
    return False


def _resume_approved_configured_post(db: Session, current: datetime) -> None:
    ready_channels = [
        channel
        for channel in ("telegram", "vk", "odnoklassniki")
        if _channel_credentials_ready(channel)
    ]
    if not ready_channels:
        return
    candidates = db.scalars(
        select(ContentItem)
        .where(
            ContentItem.status.in_(["adapter_required", "credentials_required"]),
            ContentItem.channel.in_(ready_channels),
            ContentItem.scheduled_at <= current,
        )
        .order_by(ContentItem.scheduled_at, ContentItem.id)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    item = next((candidate for candidate in candidates if _approved_item(db, candidate)), None)
    if item is None:
        return
    item.status = "scheduled"
    item.metrics = {
        **(item.metrics or {}),
        "publication_status": "automatically_resumed_after_integration_ready",
    }
    audit(
        db,
        "social_publisher_agent",
        "marketing.social_post_resumed",
        "content_item",
        str(item.id),
        {"channel": item.channel},
    )
    db.flush()


def publish_next_social_post(db: Session, *, now: datetime | None = None) -> bool:
    current = now or now_utc()
    _resume_approved_configured_post(db, current)
    item = db.scalar(
        select(ContentItem)
        .where(ContentItem.status == "scheduled", ContentItem.scheduled_at <= current)
        .order_by(ContentItem.scheduled_at, ContentItem.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if item is None:
        return False
    if item.channel == "instagram":
        item.status = "approval"
        item.metrics = {**(item.metrics or {}), "publication_status": "legal_review_required"}
        db.commit()
        return True
    if not _approved_item(db, item):
        item.status = "approval"
        item.metrics = {**(item.metrics or {}), "publication_status": "approval_invalid_or_expired"}
        db.commit()
        return True
    asset = _item_asset(db, item)
    if asset is None or asset.status != "ready" or not asset.public_url:
        item.status = "visual_pending"
        item.metrics = {**(item.metrics or {}), "publication_status": "approved_visual_unavailable"}
        db.commit()
        return True
    if item.channel not in {"telegram", "vk", "odnoklassniki"}:
        item.status = "adapter_required"
        item.metrics = {**(item.metrics or {}), "publication_status": f"{item.channel}_official_adapter_required"}
        db.commit()
        return True
    if item.channel == "telegram" and (not settings.telegram_bot_token or not settings.telegram_social_chat_id):
        item.status = "credentials_required"
        item.metrics = {**(item.metrics or {}), "publication_status": "telegram_channel_credentials_required"}
        db.commit()
        return True

    if item.channel == "vk" and (not settings.vk_community_id or not settings.vk_community_token):
        item.status = "credentials_required"
        item.metrics = {**(item.metrics or {}), "publication_status": "vk_community_credentials_required"}
        db.commit()
        return True

    if item.channel == "odnoklassniki" and not _channel_credentials_ready("odnoklassniki"):
        item.status = "credentials_required"
        item.metrics = {**(item.metrics or {}), "publication_status": "odnoklassniki_group_credentials_required"}
        db.commit()
        return True

    try:
        if item.channel == "telegram":
            endpoint = _https_endpoint("https://api.telegram.org", f"/bot{settings.telegram_bot_token}/sendPhoto")
            raw = _verified_asset_bytes(asset)
            extension = _image_extension(raw)
            content_type = "image/png" if extension == "png" else "image/jpeg"
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    endpoint,
                    data={
                        "chat_id": settings.telegram_social_chat_id,
                        "caption": _telegram_caption(item),
                    },
                    files={"photo": (f"social.{extension}", raw, content_type)},
                )
                response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise ValueError("Telegram rejected the social post")
            external_post_id = str(int(payload["result"]["message_id"]))
            provider = "telegram_bot_api"
        elif item.channel == "vk":
            external_post_id = _publish_vk(item, asset)
            provider = "vk_official_api"
        else:
            external_post_id = _publish_odnoklassniki(item, asset)
            provider = "odnoklassniki_official_api"
        item.status = "published"
        item.published_at = current
        item.metrics = {
            **(item.metrics or {}),
            "publication_status": "published",
            "external_post_id": external_post_id,
            "provider": provider,
            "published_image_sha256": (asset.metadata_json or {}).get("sha256"),
        }
        event_bus.publish(
            db,
            "marketing.social_post_published",
            "content_item",
            str(item.id),
            {"channel": item.channel, "external_post_id": external_post_id},
            idempotency_key=f"social-post-published:{item.id}:{external_post_id}",
        )
        audit(db, "social_publisher_agent", "marketing.social_post_published", "content_item", str(item.id), {"channel": item.channel, "external_post_id": external_post_id})
    except httpx.TimeoutException:
        item.status = "reconciliation_required"
        item.metrics = {**(item.metrics or {}), "publication_status": f"{item.channel}_result_ambiguous_manual_reconciliation_required"}
    except (httpx.HTTPError, KeyError, OSError, TypeError, ValueError) as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        item.status = "credentials_required" if status_code in {401, 403} else "publication_failed"
        item.metrics = {
            **(item.metrics or {}),
            "publication_status": item.status,
            **_safe_provider_failure(exc),
        }
    db.commit()
    return True
