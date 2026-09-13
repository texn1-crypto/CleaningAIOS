from __future__ import annotations

import hashlib
import json

from sqlalchemy import select


def test_tender_provider_pagination_resumes_and_keeps_cursor_private(
    client,
    monkeypatch,
):
    from app import integrations
    from app.config import settings
    from app.db import SessionLocal
    from app.models import DomainEvent, TenderSourceCheckpoint, TenderSourceRun

    source = "https://paged-feed.example/tenders"
    opaque_cursor = "private-provider-cursor-should-not-be-exposed"
    next_url = f"{source}?cursor={opaque_cursor}"
    responses = {
        source: {
            "items": [
                {
                    "external_id": "paged-tender-1",
                    "title": "Уборка первой школы",
                    "data": {"expected_margin": 20, "company_fit": 80},
                }
            ],
            "page": {
                "contract_version": "tender-page-v1",
                "acknowledgement_id": "provider-ack-page-1",
                "has_more": True,
                "next_url": next_url,
                "declared_total": 2,
                "page_number": 1,
            },
        },
        next_url: {
            "items": [
                {
                    "external_id": "paged-tender-2",
                    "title": "Уборка второй школы",
                    "data": {"expected_margin": 22, "company_fit": 85},
                }
            ],
            "page": {
                "contract_version": "tender-page-v1",
                "acknowledgement_id": "provider-ack-page-2",
                "has_more": False,
                "next_url": "",
                "declared_total": 2,
                "page_number": 2,
            },
        },
    }
    requested: list[str] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            requested.append(url)
            return Response(responses[url])

    monkeypatch.setattr(integrations.httpx, "Client", Client)
    monkeypatch.setattr(
        integrations.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(settings, "tender_sources", source)
    monkeypatch.setattr(settings, "tender_source_token", "")

    first = client.post(
        "/api/tender-sources/collect", headers={"X-Role": "manager"}
    )
    assert first.status_code == 200
    assert first.json()["created"] == 1

    source_hash = hashlib.sha256(source.encode()).hexdigest()
    with SessionLocal() as db:
        checkpoint = db.get(TenderSourceCheckpoint, source_hash)
        assert checkpoint is not None
        assert checkpoint.next_url == next_url
        assert checkpoint.next_url_hash == hashlib.sha256(next_url.encode()).hexdigest()
        assert checkpoint.version == 1
        first_run = db.scalar(
            select(TenderSourceRun)
            .where(TenderSourceRun.source_hash == source_hash)
            .order_by(TenderSourceRun.id.desc())
        )
        assert first_run is not None
        assert first_run.completeness_status == "partial"
        assert first_run.page_number == 1
        assert first_run.declared_total == 2
        assert first_run.provider_acknowledgement_hash == hashlib.sha256(
            b"provider-ack-page-1"
        ).hexdigest()

    visible_checkpoint = client.get(
        "/api/tender-sources/checkpoints", headers={"X-Role": "manager"}
    )
    assert visible_checkpoint.status_code == 200
    public_first = next(
        row
        for row in visible_checkpoint.json()
        if row["source_ref"] == source_hash[:32]
    )
    assert public_first["has_pending_page"] is True
    assert opaque_cursor not in json.dumps(public_first)
    assert "provider-ack-page-1" not in json.dumps(public_first)
    assert client.get(
        "/api/tender-sources/checkpoints", headers={"X-Role": "viewer"}
    ).status_code == 403

    second = client.post(
        "/api/tender-sources/collect", headers={"X-Role": "manager"}
    )
    assert second.status_code == 200
    assert second.json()["created"] == 1
    assert requested == [source, next_url]

    with SessionLocal() as db:
        checkpoint = db.get(TenderSourceCheckpoint, source_hash)
        assert checkpoint is not None
        assert checkpoint.next_url == ""
        assert checkpoint.next_url_hash == ""
        assert checkpoint.version == 2
        assert checkpoint.last_completeness_status == "complete"
        runs = list(
            db.scalars(
                select(TenderSourceRun)
                .where(TenderSourceRun.source_hash == source_hash)
                .order_by(TenderSourceRun.id)
            ).all()
        )
        assert [row.completeness_status for row in runs] == ["partial", "complete"]
        assert runs[1].request_url_hash == hashlib.sha256(next_url.encode()).hexdigest()
        events = list(
            db.scalars(
                select(DomainEvent).where(
                    DomainEvent.aggregate_type == "tender_source_run",
                    DomainEvent.aggregate_id.in_([str(row.id) for row in runs]),
                )
            ).all()
        )
        serialized_events = json.dumps([event.payload for event in events])
        assert opaque_cursor not in serialized_events
        assert "provider-ack-page-1" not in serialized_events
        assert "provider-ack-page-2" not in serialized_events

    visible_runs = client.get(
        f"/api/tender-sources/runs?source_ref={source_hash[:32]}",
        headers={"X-Role": "manager"},
    )
    assert visible_runs.status_code == 200
    serialized_runs = json.dumps(visible_runs.json())
    assert opaque_cursor not in serialized_runs
    assert "provider-ack-page-1" not in serialized_runs


def test_tender_provider_pagination_fails_closed_on_cross_origin_next_url(
    client,
    monkeypatch,
):
    from app import integrations
    from app.config import settings
    from app.db import SessionLocal
    from app.models import BusinessRecord, TenderSourceCheckpoint, TenderSourceRun

    source = "https://invalid-page.example/tenders"
    body = {
        "items": [
            {
                "external_id": "must-not-persist-from-invalid-page",
                "title": "Недоверенный тендер",
            }
        ],
        "page": {
            "contract_version": "tender-page-v1",
            "acknowledgement_id": "invalid-page-ack",
            "has_more": True,
            "next_url": "https://attacker.example/steal-cursor",
            "declared_total": 1,
            "page_number": 1,
        },
    }

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def json(self):
            return body

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            return Response()

    monkeypatch.setattr(integrations.httpx, "Client", Client)
    monkeypatch.setattr(
        integrations.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(settings, "tender_sources", source)
    monkeypatch.setattr(settings, "tender_source_token", "")

    result = client.post(
        "/api/tender-sources/collect", headers={"X-Role": "manager"}
    )
    assert result.status_code == 200
    assert result.json() == {
        "status": "completed_with_errors",
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "errors": [
            {
                "source": source,
                "error": "Tender source collection failed",
                "error_type": "ValueError",
            }
        ],
    }

    source_hash = hashlib.sha256(source.encode()).hexdigest()
    with SessionLocal() as db:
        assert db.get(TenderSourceCheckpoint, source_hash) is None
        assert db.scalar(
            select(BusinessRecord.id).where(
                BusinessRecord.external_id == "must-not-persist-from-invalid-page"
            )
        ) is None
        receipt = db.scalar(
            select(TenderSourceRun)
            .where(TenderSourceRun.source_hash == source_hash)
            .order_by(TenderSourceRun.id.desc())
        )
        assert receipt is not None
        assert receipt.status == "failed"
        assert receipt.completeness_status == "unknown"
        assert receipt.next_url_hash == ""
        assert receipt.provider_acknowledgement_hash == ""
