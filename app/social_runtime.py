from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import ApprovalRequest, ContentItem, MediaAsset
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
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _https_endpoint(base: str, path: str) -> str:
    endpoint = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
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


def _store_verified_image(asset: MediaAsset, raw: bytes, *, provider: str, metadata: dict) -> None:
    if not raw or len(raw) > settings.max_attachment_bytes:
        raise ValueError("Generated image is empty or exceeds the configured size limit")
    extension = _image_extension(raw)
    digest = hashlib.sha256(raw).hexdigest()
    root = Path(settings.document_storage_path).resolve()
    directory = root / "social-media"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{asset.id}-{digest[:16]}.{extension}"
    temporary = directory / f".{asset.id}-{digest[:16]}.tmp"
    temporary.write_bytes(raw)
    temporary.replace(destination)
    asset.provider = provider
    asset.storage_path = str(destination)
    asset.public_url = f"/api/public/social-media/{asset.id}/{digest}.{extension}"
    asset.status = "ready"
    asset.metadata_json = {
        **metadata,
        "sha256": digest,
        "generation_verified": True,
        "generation_status": "ready_for_owner_preview",
        "generated_at": now_utc().isoformat(),
    }


def _use_local_media_pool(asset: MediaAsset, metadata: dict) -> None:
    slot = int(metadata.get("slot") or 1)
    batch_id = int(metadata.get("batch_id") or 0)
    relative_path = LOCAL_SOCIAL_MEDIA_POOL[(batch_id + slot - 1) % len(LOCAL_SOCIAL_MEDIA_POOL)]
    source = (Path(__file__).resolve().parent / "static" / relative_path).resolve()
    raw = source.read_bytes()
    _store_verified_image(asset, raw, provider="local_media_pool", metadata={
        **metadata,
        "media_pool_source": relative_path,
        "rights_basis": "original_project_asset",
        "model": None,
    })


def generate_next_social_visual(db: Session) -> bool:
    asset = db.scalar(
        select(MediaAsset)
        .where(
            # ``imagegen`` was used by the original deterministic social-plan
            # workflow. Keep consuming those persisted jobs while all newly
            # created jobs use the canonical ``openai_images`` provider.
            MediaAsset.provider.in_(["imagegen", "openai_images", "local_media_pool"]),
            MediaAsset.status.in_(["queued", "credentials_required"]),
        )
        .order_by(MediaAsset.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if asset is None:
        return False
    metadata = dict(asset.metadata_json or {})
    if asset.provider == "local_media_pool" or not (
        settings.social_image_generation_enabled and settings.image_generation_api_key
    ):
        try:
            _use_local_media_pool(asset, metadata)
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
        except (OSError, ValueError) as exc:
            asset.status = "generation_failed"
            asset.metadata_json = {**metadata, "generation_status": "generation_failed", "error_type": type(exc).__name__}
        db.commit()
        return True

    try:
        endpoint = _https_endpoint(settings.image_generation_base_url, "/images/generations")
        with httpx.Client(timeout=90) as client:
            response = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {settings.image_generation_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.image_generation_model,
                    "prompt": asset.prompt,
                    "size": "1024x1024",
                    "quality": settings.image_generation_quality,
                    "output_format": "png",
                },
            )
            response.raise_for_status()
        encoded = str(response.json()["data"][0]["b64_json"])
        raw = base64.b64decode(encoded, validate=True)
        _store_verified_image(asset, raw, provider="openai_images", metadata={
            **metadata,
            "model": settings.image_generation_model,
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
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        asset.status = "credentials_required" if status_code in {401, 403} else "generation_failed"
        asset.metadata_json = {
            **metadata,
            "generation_status": asset.status,
            "error_type": type(exc).__name__,
        }
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


def publish_next_social_post(db: Session, *, now: datetime | None = None) -> bool:
    current = now or now_utc()
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
    if item.channel not in {"telegram", "vk"}:
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

    image_url = urljoin(settings.public_base_url.rstrip("/") + "/", asset.public_url.lstrip("/"))
    try:
        if item.channel == "telegram":
            endpoint = _https_endpoint("https://api.telegram.org", f"/bot{settings.telegram_bot_token}/sendPhoto")
            with httpx.Client(timeout=30) as client:
                response = client.post(
                    endpoint,
                    json={
                        "chat_id": settings.telegram_social_chat_id,
                        "photo": image_url,
                        "caption": _telegram_caption(item),
                    },
                )
                response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise ValueError("Telegram rejected the social post")
            external_post_id = str(int(payload["result"]["message_id"]))
            provider = "telegram_bot_api"
        else:
            external_post_id = _publish_vk(item, asset)
            provider = "vk_official_api"
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
        item.metrics = {**(item.metrics or {}), "publication_status": item.status, "error_type": type(exc).__name__}
    db.commit()
    return True
