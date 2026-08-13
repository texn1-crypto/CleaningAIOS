from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import update

from app.config import settings
from app.db import SessionLocal
from app.models import ApprovalRequest, BusinessRecord, ContentItem, MediaAsset, OwnerNotification
from app.social_marketing import prepare_daily_cleaning_news_plan
from app.social_news import CleaningNewsItem, parse_cleaning_news_feed
from app.social_runtime import _https_endpoint, _ok_signature, _safe_provider_failure, generate_next_social_visual, publish_next_social_post


def test_cleaning_news_feed_keeps_only_fresh_relevant_https_items(monkeypatch):
    monkeypatch.setattr(settings, "cleaning_news_max_age_days", 14)
    raw = b"""<?xml version='1.0'?><rss><channel><title>Trade News</title>
      <item><title>New floor care standard</title><description>Cleaning teams improve hygiene</description>
        <link>https://example.org/floor-care</link><pubDate>Wed, 12 Aug 2026 10:00:00 GMT</pubDate></item>
      <item><title>Old cleaning story</title><description>Janitorial update</description>
        <link>https://example.org/old</link><pubDate>Wed, 01 Jul 2026 10:00:00 GMT</pubDate></item>
      <item><title>Cleaning on an unsafe link</title><description>Hygiene</description>
        <link>http://example.org/unsafe</link><pubDate>Wed, 12 Aug 2026 10:00:00 GMT</pubDate></item>
      <item><title>Unrelated finance story</title><description>Market prices</description>
        <link>https://example.org/finance</link><pubDate>Wed, 12 Aug 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    items = parse_cleaning_news_feed(raw, feed_url="https://example.org/rss", now=datetime(2026, 8, 13))
    assert [item.source_url for item in items] == ["https://example.org/floor-care"]
    assert items[0].source_name == "example.org"


def test_provider_endpoint_keeps_base_path_and_token_colon_in_path():
    assert _https_endpoint("https://api.example.test/v1", "/images/generations") == (
        "https://api.example.test/v1/images/generations"
    )
    assert _https_endpoint("https://api.telegram.org", "/bot123456:secret/sendPhoto") == (
        "https://api.telegram.org/bot123456:secret/sendPhoto"
    )


def test_news_agent_creates_source_bound_posts_and_image_jobs(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "")
    news = [
        CleaningNewsItem("Robotic scrubbers", "Facilities test safer floor cleaning.", "https://news.example/robot", "Trade source", datetime(2042, 2, 1)),
        CleaningNewsItem("Restroom hygiene", "A new hygiene guide was published.", "https://news.example/hygiene", "Trade source", datetime(2042, 2, 2)),
    ]
    with SessionLocal() as db:
        result = prepare_daily_cleaning_news_plan(db, day=datetime(2042, 2, 3, 7), news_items=news)
        db.commit()
        assert result["created"] == 8
        items = [db.get(ContentItem, value) for value in result["content_item_ids"]]
        assert {item.channel for item in items} == {"telegram", "vk", "odnoklassniki", "instagram"}
        assert all("Источник: Trade source" in item.body for item in items)
        assert {item.metrics["source_url"] for item in items} == {value.source_url for value in news}
        assert all(item.metrics["source_verified"] is True for item in items)
        assets = [db.get(MediaAsset, value) for value in result["media_asset_ids"]]
        assert len(assets) == 2
        assert all(asset.provider == "openai_images" and asset.status == "queued" for asset in assets)


def test_news_agent_does_not_invent_posts_when_sources_are_empty(client):
    with SessionLocal() as db:
        result = prepare_daily_cleaning_news_plan(db, day=datetime(2042, 2, 4, 7), news_items=[])
        assert result["status"] == "news_unavailable"
        assert result["created"] == 0


def test_image_agent_generates_checksum_bound_public_asset(client, monkeypatch, tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"verified-generated-image"
    digest = hashlib.sha256(raw).hexdigest()

    class Response:
        def raise_for_status(self): return None
        def json(self): return {"data": [{"b64_json": base64.b64encode(raw).decode()}]}

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def post(self, *args, **kwargs): return Response()

    monkeypatch.setattr("app.social_runtime.httpx.Client", Client)
    monkeypatch.setattr(settings, "social_image_generation_enabled", True)
    monkeypatch.setattr(settings, "image_generation_api_key", "test-key")
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    with SessionLocal() as db:
        db.execute(update(MediaAsset).where(MediaAsset.provider == "openai_images", MediaAsset.status == "queued").values(status="test_skipped"))
        asset = MediaAsset(kind="image", title="Generated", provider="openai_images", prompt="Safe prompt", status="queued", metadata_json={})
        db.add(asset); db.commit(); asset_id = asset.id
        assert generate_next_social_visual(db) is True
        db.refresh(asset)
        assert asset.status == "ready"
        assert asset.metadata_json["sha256"] == digest
        public_url = asset.public_url
    response = client.get(public_url)
    assert response.status_code == 200
    assert response.content == raw


def test_image_agent_uses_original_local_photo_pool_without_paid_key(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "social_image_generation_enabled", False)
    monkeypatch.setattr(settings, "image_generation_api_key", "")
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    with SessionLocal() as db:
        db.execute(update(MediaAsset).where(MediaAsset.provider == "openai_images", MediaAsset.status == "queued").values(status="test_skipped"))
        asset = MediaAsset(kind="image", title="Disabled", provider="openai_images", prompt="Prompt", status="queued", metadata_json={})
        db.add(asset); db.commit()
        assert generate_next_social_visual(db) is True
        db.refresh(asset)
        assert asset.status == "ready"
        assert asset.provider == "local_media_pool"
        assert asset.metadata_json["generation_status"] == "ready_for_owner_preview"
        assert asset.metadata_json["rights_basis"] == "original_project_asset"
        assert len(asset.metadata_json["sha256"]) == 64
        assert client.get(asset.public_url).status_code == 200


def test_image_agent_consumes_legacy_imagegen_job(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "social_image_generation_enabled", False)
    monkeypatch.setattr(settings, "image_generation_api_key", "")
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    with SessionLocal() as db:
        asset = MediaAsset(
            kind="image",
            title="Legacy visual",
            provider="imagegen",
            prompt="Legacy prompt",
            status="queued",
            metadata_json={"batch_id": 0, "slot": 1},
        )
        db.add(asset)
        db.commit()

        assert generate_next_social_visual(db) is True
        db.refresh(asset)
        assert asset.status == "ready"
        assert asset.provider == "local_media_pool"
        assert asset.metadata_json["generation_status"] == "ready_for_owner_preview"
        assert client.get(asset.public_url).status_code == 200


def test_telegram_publisher_sends_only_owner_approved_exact_post(client, monkeypatch, tmp_path):
    calls = []
    raw = b"\x89PNG\r\n\x1a\n" + b"telegram-publisher-image"
    digest = hashlib.sha256(raw).hexdigest()
    image_path = tmp_path / "telegram.png"
    image_path.write_bytes(raw)

    class Response:
        def raise_for_status(self): return None
        def json(self): return {"ok": True, "result": {"message_id": 812}}

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def post(self, url, **kwargs): calls.append((url, kwargs)); return Response()

    monkeypatch.setattr("app.social_runtime.httpx.Client", Client)
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_social_chat_id", "@cleaning_channel")
    monkeypatch.setattr(settings, "public_base_url", "https://cleaning.example")
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    with SessionLocal() as db:
        batch = BusinessRecord(record_type="social_content_batch", external_id="publisher-test-2042", title="Publisher", status="scheduled", data={})
        db.add(batch); db.flush()
        approval = ApprovalRequest(action_kind="social_publication", resource_type="social_content_batch", resource_id=str(batch.id), status="approved")
        db.add(approval); db.flush()
        asset = MediaAsset(
            kind="image",
            title="Approved image",
            provider="openai_images",
            public_url="/api/public/social-media/fake.png",
            storage_path=str(image_path),
            status="ready",
            metadata_json={"sha256": digest},
        )
        db.add(asset); db.flush()
        item = ContentItem(
            channel="telegram", title="News", body="Verified news text\n\nhttps://source.example/item", status="scheduled",
            scheduled_at=datetime(1999, 1, 1), metrics={"batch_id": batch.id, "approval_id": approval.id, "visual_asset_id": asset.id, "source_url": "https://source.example/item"},
        )
        db.add(item); db.commit(); item_id = item.id
        assert publish_next_social_post(db, now=datetime(2042, 1, 1)) is True
        db.refresh(item)
        assert item.status == "published"
        assert item.metrics["external_post_id"] == "812"
        assert len(calls) == 1
        assert calls[0][1]["data"]["caption"] == item.body
        assert calls[0][1]["files"]["photo"] == ("social.png", raw, "image/png")
        assert "json" not in calls[0][1]
        db.refresh(db.get(ContentItem, item_id))


def test_social_publisher_records_safe_provider_failure_details():
    secret = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://api.telegram.org/redacted/sendPhoto"),
        json={"description": f"Bad Request: token={secret}"},
    )
    exc = httpx.HTTPStatusError("provider rejected request", request=response.request, response=response)
    details = _safe_provider_failure(exc)
    assert details["provider_status_code"] == 400
    assert details["error_type"] == "HTTPStatusError"
    assert secret not in details["provider_error"]
    assert "[REDACTED]" in details["provider_error"]


def test_vk_publisher_uses_official_upload_and_wall_apis_after_approval(client, monkeypatch, tmp_path):
    calls = []
    raw = b"\x89PNG\r\n\x1a\n" + b"vk-image"
    digest = hashlib.sha256(raw).hexdigest()
    directory = tmp_path / "social-media"
    directory.mkdir()
    image_path = directory / "vk.png"
    image_path.write_bytes(raw)

    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): return None
        def json(self): return self.payload

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("photos.getWallUploadServer"):
                return Response({"response": {"upload_url": "https://upload.vk.example/photo"}})
            if url == "https://upload.vk.example/photo":
                return Response({"server": 1, "photo": "[]", "hash": "upload-hash"})
            if url.endswith("photos.saveWallPhoto"):
                return Response({"response": [{"owner_id": -123, "id": 456}]})
            if url.endswith("wall.post"):
                return Response({"response": {"post_id": 789}})
            raise AssertionError(url)

    monkeypatch.setattr("app.social_runtime.httpx.Client", Client)
    monkeypatch.setattr(settings, "vk_community_id", "123")
    monkeypatch.setattr(settings, "vk_community_token", "test-vk-token")
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    with SessionLocal() as db:
        batch = BusinessRecord(record_type="social_content_batch", external_id="vk-publisher-test-2042", title="VK Publisher", status="scheduled", data={})
        db.add(batch); db.flush()
        approval = ApprovalRequest(action_kind="social_publication", resource_type="social_content_batch", resource_id=str(batch.id), status="approved")
        db.add(approval); db.flush()
        asset = MediaAsset(kind="image", title="VK image", provider="openai_images", public_url="/vk.png", storage_path=str(image_path), status="ready", metadata_json={"sha256": digest})
        db.add(asset); db.flush()
        item = ContentItem(channel="vk", title="VK news", body="Exact approved VK text", status="scheduled", scheduled_at=datetime(1998, 1, 1), metrics={"batch_id": batch.id, "approval_id": approval.id, "visual_asset_id": asset.id})
        db.add(item); db.commit()
        assert publish_next_social_post(db, now=datetime(2042, 1, 1)) is True
        db.refresh(item)
        assert item.status == "published"
        assert item.metrics["external_post_id"] == "789"
        assert item.metrics["provider"] == "vk_official_api"
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == [
        "photos.getWallUploadServer", "photo", "photos.saveWallPhoto", "wall.post"
    ]
    wall_payload = calls[-1][1]["data"]
    assert wall_payload["message"] == "Exact approved VK text"
    assert wall_payload["guid"].startswith("cleaningaios-content-")


def test_odnoklassniki_publisher_resumes_approved_post_and_uses_official_api(client, monkeypatch, tmp_path):
    calls = []
    raw = b"\x89PNG\r\n\x1a\n" + b"odnoklassniki-image"
    digest = hashlib.sha256(raw).hexdigest()
    image_path = tmp_path / "odnoklassniki.png"
    image_path.write_bytes(raw)

    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): return None
        def json(self): return self.payload

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url == "https://api.ok.ru/fb.do":
                method = kwargs["data"]["method"]
                if method == "photosV2.getUploadUrl":
                    return Response({"upload_url": "https://upload.ok.example/photo", "photo_ids": ["future-photo"]})
                if method == "mediatopic.post":
                    return Response("topic-812")
            if url == "https://upload.ok.example/photo":
                return Response({"photos": {"future-photo": {"token": "approved-photo-token"}}})
            raise AssertionError(url)

    monkeypatch.setattr("app.social_runtime.httpx.Client", Client)
    monkeypatch.setattr(settings, "odnoklassniki_group_id", "812345")
    monkeypatch.setattr(settings, "odnoklassniki_application_key", "public-key")
    monkeypatch.setattr(settings, "odnoklassniki_access_token", "access-token")
    monkeypatch.setattr(settings, "odnoklassniki_session_secret", "session-secret")
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    with SessionLocal() as db:
        batch = BusinessRecord(record_type="social_content_batch", external_id="ok-publisher-test-2042", title="OK Publisher", status="scheduled", data={})
        db.add(batch); db.flush()
        approval = ApprovalRequest(action_kind="social_publication", resource_type="social_content_batch", resource_id=str(batch.id), status="approved")
        db.add(approval); db.flush()
        asset = MediaAsset(kind="image", title="OK image", provider="local_media_pool", public_url="/ok.png", storage_path=str(image_path), status="ready", metadata_json={"sha256": digest})
        db.add(asset); db.flush()
        item = ContentItem(channel="odnoklassniki", title="OK news", body="Точный одобренный текст ОК", status="adapter_required", scheduled_at=datetime(1998, 1, 1), metrics={"batch_id": batch.id, "approval_id": approval.id, "visual_asset_id": asset.id})
        db.add(item); db.commit()
        assert publish_next_social_post(db, now=datetime(2042, 1, 1)) is True
        db.refresh(item)
        assert item.status == "published"
        assert item.metrics["external_post_id"] == "topic-812"
        assert item.metrics["provider"] == "odnoklassniki_official_api"

    api_calls = [kwargs["data"] for url, kwargs in calls if url == "https://api.ok.ru/fb.do"]
    assert [payload["method"] for payload in api_calls] == ["photosV2.getUploadUrl", "mediatopic.post"]
    assert all(payload["sig"] == _ok_signature(payload) for payload in api_calls)
    assert calls[1][1]["files"]["pic1"] == ("social.png", raw, "image/png")
    attachment = json.loads(api_calls[-1]["attachment"])
    assert attachment["media"] == [
        {"type": "photo", "list": [{"id": "approved-photo-token"}]},
        {"type": "text", "text": "Точный одобренный текст ОК"},
    ]
    assert attachment["onBehalfOfGroup"] == "true"


def test_social_summary_does_not_expose_credentials(client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "super-secret-bot-token")
    monkeypatch.setattr(settings, "telegram_social_chat_id", "@channel")
    response = client.get("/api/marketing/social-summary", headers={"X-Role": "viewer"})
    assert response.status_code == 200
    assert response.json()["integrations"]["telegram"] == "ready"
    assert "super-secret" not in response.text


def test_owner_preview_uploads_checksum_verified_local_photo_and_redacts_token(monkeypatch, tmp_path):
    from app import notifications

    raw = b"\x89PNG\r\n\x1a\n" + b"owner-preview"
    digest = hashlib.sha256(raw).hexdigest()
    image_path = tmp_path / "preview.png"
    image_path.write_bytes(raw)
    approval = ApprovalRequest(
        id=991,
        action_kind="social_publication",
        resource_type="social_content_batch",
        resource_id="991",
        status="pending",
        decision_version=1,
        payload={},
    )
    asset = MediaAsset(id=992, kind="image", title="Preview", storage_path=str(image_path), status="ready", metadata_json={"sha256": digest})
    captured = []

    class Response:
        def raise_for_status(self): return None

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def post(self, url, **kwargs): captured.append((url, kwargs)); return Response()

    class Db:
        def get(self, model, identity):
            return approval if model is ApprovalRequest else asset if model is MediaAsset else None

    monkeypatch.setattr(notifications.httpx, "Client", Client)
    monkeypatch.setattr(settings, "telegram_bot_token", "991:super-secret-token")
    monkeypatch.setattr(settings, "telegram_callback_secret", "callback-secret")
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    row = OwnerNotification(
        idempotency_key="local-preview-upload-test",
        channel="telegram",
        recipient="999",
        subject="Preview",
        body="Review",
        data={"approval_id": approval.id, "preview_posts": [{
            "channel": "telegram", "scheduled_at": "2042-01-01T07:00:00", "body": "Exact text",
            "image_url": "https://example.test/preview.png", "visual_asset_id": asset.id,
        }]},
    )
    notifications._send_telegram(Db(), row)
    assert captured[0][1]["files"][0][0] == f"asset_{asset.id}"
    assert f"attach://asset_{asset.id}" in captured[0][1]["data"]["media"]
    error = notifications._safe_delivery_error(Exception("https://api.telegram.org/bot991:super-secret-token/sendMediaGroup"))
    assert "super-secret-token" not in error
    assert "<redacted>" in error
