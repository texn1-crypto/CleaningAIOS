from __future__ import annotations

from datetime import date, datetime

from app.config import settings
from app.db import SessionLocal
from app.models import ApprovalRequest, BusinessRecord, ContentItem, MediaAsset, OutboundMessage


def test_contact_exports_neutralize_spreadsheet_formulas(tmp_path, monkeypatch):
    import csv

    from openpyxl import load_workbook

    from app import contact_directory

    monkeypatch.setattr(
        contact_directory.settings,
        "document_storage_path",
        str(tmp_path),
    )
    hostile = [
        "=HYPERLINK(\"https://attacker.example\")",
        "\t=1+1",
        "-42",
        "@SUM(1,1)",
        "  +cmd|' /C calc'!A0",
        "\r=1+1",
        "safe",
    ]
    csv_path = tmp_path / "formula-test.csv"
    xlsx_path = tmp_path / "formula-test.xlsx"
    contact_directory._write_csv(csv_path, [hostile])
    contact_directory._write_xlsx(
        xlsx_path,
        [hostile],
        week_start=date(2042, 5, 5),
    )
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        csv_values = list(csv.reader(stream))[1]
    assert all(value.startswith("'") for value in csv_values[:-1])
    assert csv_values[-1] == "safe"
    workbook = load_workbook(xlsx_path, read_only=True, data_only=False)
    try:
        cells = list(workbook["Новые контакты"][4])
        assert all(cell.data_type != "f" for cell in cells)
        assert cells[-1].value == "safe"
    finally:
        workbook.close()


def test_manual_completion_requires_owner(client):
    task = client.post(
        "/api/tasks",
        headers={"X-Role": "operator"},
        json={"title": "Manual completion boundary"},
    ).json()
    assert client.post(
        f"/api/tasks/{task['id']}/complete",
        headers={"X-Role": "operator"},
    ).status_code == 403
    assert client.post(f"/api/tasks/{task['id']}/complete").status_code == 200


def test_generic_record_crud_rejects_protected_statuses(client):
    assert client.post(
        "/api/records",
        json={
            "record_type": "marketing_invoice",
            "title": "Unapproved invoice",
            "status": "approved_for_manual_payment",
        },
    ).status_code == 409
    tender = client.post(
        "/api/records",
        json={
            "record_type": "tender",
            "title": "Protected transition",
            "deadline_at": "2030-01-01T12:00:00Z",
        },
    ).json()
    assert client.patch(
        f"/api/records/{tender['id']}",
        json={"status": "submitted"},
    ).status_code == 409


def test_bulk_campaign_action_derives_protected_policy(client):
    task = client.post(
        "/api/tasks",
        json={
            "title": "Protected bulk campaign",
            "agent_type": "sales",
            "payload": {
                "action": "execute_bulk_outreach_campaign",
                "campaign_key": "derived-protected-policy",
                "recipients": ["derived-policy@example.com"],
                "subject": "Protected subject",
                "body": "Protected body",
                "action_kind": "financial",
            },
        },
    ).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()
    assert result["status"] == "blocked"
    assert result["result"]["reason"] == "owner_approval_required"
    assert result["payload"]["action_kind"] == "bulk_outreach"


def test_legacy_outreach_requires_consent_exact_approval_and_inline_attachments(
    client,
    tmp_path,
):
    address = "secured-legacy-outreach@example.com"
    payload = {
        "campaign_key": "secured-legacy-outreach",
        "recipient": address,
        "subject": "Exact subject",
        "body": "Exact body",
    }
    assert client.post("/api/outreach/messages", json=payload).status_code == 422
    assert client.put(
        "/api/outreach/consents",
        json={
            "address": address,
            "source_url": "https://consent.example.test/secured-legacy",
            "evidence": "Documented commercial opt-in",
        },
    ).status_code == 200
    secret = tmp_path / "not-an-outreach-object.txt"
    secret.write_text("protected")
    path_attempt = client.post(
        "/api/outreach/messages",
        json={
            **payload,
            "attachments": [{
                "filename": "protected.txt",
                "storage_path": str(secret),
                "sha256": "0" * 64,
            }],
        },
    )
    assert path_attempt.status_code == 422
    requested = client.post("/api/outreach/messages", json=payload)
    assert requested.status_code == 201
    assert requested.json()["status"] == "waiting_approval"
    approval_id = requested.json()["approval_id"]
    assert client.post(
        f"/api/approvals/{approval_id}/approve",
        json={"note": "Exact content reviewed"},
    ).status_code == 200
    queued = client.post(
        "/api/outreach/messages",
        json={**payload, "approval_id": approval_id},
    )
    assert queued.status_code == 201
    assert queued.json()["id"]


def test_social_publication_requires_exact_batch_membership_and_digest():
    from app.social_marketing import finalize_social_preview_batch
    from app.social_runtime import _approved_item

    with SessionLocal() as db:
        batch = BusinessRecord(
            record_type="social_content_batch",
            external_id="security-exact-preview",
            title="Exact preview",
            status="draft",
            data={},
        )
        asset = MediaAsset(
            kind="image",
            title="Approved visual",
            status="ready",
            public_url="https://cleaning.example/approved.png",
            metadata_json={"sha256": "a" * 64, "generation_verified": True},
        )
        db.add_all([batch, asset])
        db.flush()
        approved_item = ContentItem(
            channel="website",
            title="Approved",
            body="Approved body",
            status="approval",
            scheduled_at=datetime(2042, 1, 1),
            metrics={"batch_id": batch.id, "visual_asset_id": asset.id},
        )
        db.add(approved_item)
        db.flush()
        batch.data = {
            "content_item_ids": [approved_item.id],
            "visual_asset_ids": [asset.id],
        }
        result = finalize_social_preview_batch(db, batch.id)
        approval = db.get(ApprovalRequest, result["approval_id"])
        approval.status = "approved"
        approved_item.metrics = {
            **approved_item.metrics,
            "approval_id": approval.id,
        }
        rogue = ContentItem(
            channel="website",
            title="Rogue",
            body="Rogue body",
            status="scheduled",
            scheduled_at=datetime(2042, 1, 1),
            metrics={
                "batch_id": batch.id,
                "approval_id": approval.id,
                "visual_asset_id": asset.id,
            },
        )
        db.add(rogue)
        db.flush()
        assert _approved_item(db, approved_item) is True
        assert _approved_item(db, rogue) is False
        approved_item.body = "Changed after approval"
        assert _approved_item(db, approved_item) is False
        db.rollback()


def test_repeat_public_lead_does_not_overwrite_authoritative_record(client, monkeypatch):
    monkeypatch.setattr(settings, "public_lead_rate_limit_per_hour", 10_000)
    original = {
        "name": "Ольга Владелец",
        "company": "УК Каноническая",
        "email": "immutable-public-lead@example.com",
        "service": "general",
        "urgency": "planning",
        "message": "Исходная заявка",
        "consent": True,
        "utm_source": "original-source",
    }
    first = client.post("/api/public/leads", json=original).json()
    repeated = client.post(
        "/api/public/leads",
        json={
            **original,
            "name": "Подмена",
            "company": "Подменённая компания",
            "service": "business_center",
            "urgency": "today",
            "budget": 900_000,
            "message": "Подменённая заявка",
            "utm_source": "attacker-source",
        },
    )
    assert repeated.status_code == 201
    with SessionLocal() as db:
        lead = db.get(BusinessRecord, first["lead_id"])
        assert lead.title == "УК Каноническая"
        assert lead.source == "original-source"
        assert lead.data["name"] == "Ольга Владелец"
        assert lead.data["message"] == "Исходная заявка"


def test_ready_social_asset_is_not_public(client, tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    raw = b"ready-private"
    digest = hashlib.sha256(raw).hexdigest()
    path = tmp_path / "social-media" / "ready.png"
    path.parent.mkdir()
    path.write_bytes(raw)
    with SessionLocal() as db:
        asset = MediaAsset(
            kind="image",
            title="Private preview",
            status="ready",
            storage_path=str(path),
            metadata_json={"sha256": digest},
        )
        db.add(asset)
        db.commit()
        url = f"/api/public/social-media/{asset.id}/{digest}.png"
        asset_id = asset.id
    assert client.get(url).status_code == 404
    assert all(
        item["id"] != asset_id
        for item in client.get("/api/public/site").json()["media"]
    )


def test_authenticated_tender_token_never_reaches_unconfigured_origin(monkeypatch):
    from app import integrations

    configured = "https://configured-feed.example/tenders"
    attacker = "https://attacker.example/collect"
    requested = []

    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer test-token"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            requested.append(url)
            raise AssertionError("unconfigured origin must not be requested")

    monkeypatch.setattr(integrations.httpx, "Client", Client)
    monkeypatch.setattr(settings, "tender_sources", configured)
    monkeypatch.setattr(settings, "tender_source_token", "test-token")
    with SessionLocal() as db:
        result = integrations.collect_tenders(db, sources=[attacker])
    assert result["status"] == "completed_with_errors"
    assert result["errors"][0]["error_type"] == "HTTPException"
    assert requested == []


def test_tender_transport_pins_public_dns_and_preserves_tls_identity(monkeypatch):
    import httpx

    from app import integrations

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(
        integrations.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )
    transport = integrations._DNSPinningTransport(httpx.MockTransport(handler))
    with httpx.Client(transport=transport) as pinned_client:
        response = pinned_client.get("https://feed.example/tenders?page=1")
    assert response.status_code == 200
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "feed.example"
    assert seen[0].extensions["sni_hostname"] == "feed.example"
    assert seen[0].headers["connection"] == "close"


def test_smtp_crash_window_is_not_automatically_retried(monkeypatch):
    from sqlalchemy import update

    from app import worker

    accepted = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            accepted.append(message["To"])
            raise SystemExit("simulated process exit after provider acceptance")

    monkeypatch.setattr(worker.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "user")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    monkeypatch.setattr(settings, "smtp_from_email", "sender@example.com")
    monkeypatch.setattr(settings, "outreach_min_interval_minutes", 0)
    now = datetime(2040, 1, 1, 12, 0)
    with SessionLocal() as db:
        db.execute(
            update(OutboundMessage)
            .where(OutboundMessage.status.in_(["queued", "waiting_configuration", "retry"]))
            .values(status="sent")
        )
        row = OutboundMessage(
            campaign_key="security-crash-window",
            recipient="crash-window@example.com",
            subject="Test",
            body="Body",
            scheduled_at=now,
        )
        db.add(row)
        db.commit()
        row_id = row.id
        try:
            worker.send_next_email(db, now=now)
        except SystemExit:
            pass
        else:
            raise AssertionError("simulated process exit must escape the worker")
    with SessionLocal() as db:
        assert db.get(OutboundMessage, row_id).status == "reconciliation_required"
        assert worker.send_next_email(db, now=now) is False
    assert accepted == ["crash-window@example.com"]
