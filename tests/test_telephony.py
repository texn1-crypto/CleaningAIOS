import hashlib
import hmac
import json
from datetime import datetime

import httpx
import pytest

from app import telephony
from app.config import settings
from app.db import SessionLocal
from app.models import BusinessRecord, ContactEvent
from app.telephony import place_next_sales_call, queue_consented_sales_call


def _consented_lead(client, suffix: str) -> dict:
    response = client.post(
        "/api/records",
        json={
            "record_type": "lead",
            "title": f"Consented voice lead {suffix}",
            "data": {
                "phone": "+79990001234",
                "phone_contact_consent": True,
                "phone_contact_consent_source": "customer_callback_form",
                "phone_contact_consent_at": "2042-01-01T09:00:00Z",
            },
        },
    )
    assert response.status_code == 201
    return response.json()


def _approved_call(client, lead_id: int, suffix: str) -> dict:
    requested = client.post(
        "/api/telephony/calls",
        json={
            "lead_id": lead_id,
            "idempotency_key": f"voice-test-{suffix}",
            "purpose": "Confirm the requested cleaning consultation",
            "script": "Disclose that this is an AI-assisted call and ask whether now is convenient.",
            "scheduled_at": "2042-01-01T10:00:00Z",
        },
    )
    assert requested.status_code == 202
    repeated = client.post(
        "/api/telephony/calls",
        json={
            "lead_id": lead_id,
            "idempotency_key": f"voice-test-{suffix}",
            "purpose": "Confirm the requested cleaning consultation",
            "script": "Disclose that this is an AI-assisted call and ask whether now is convenient.",
            "scheduled_at": "2042-01-01T10:00:00Z",
        },
    )
    assert repeated.status_code == 202
    assert repeated.json()["duplicate"] is True
    task_id = requested.json()["task_id"]
    blocked = client.post(f"/api/tasks/{task_id}/run").json()
    assert blocked["status"] == "blocked"
    assert blocked["result"]["reason"] == "owner_approval_required"
    assert blocked["payload"]["action_kind"] == "voice_call"
    approval_id = blocked["result"]["approval_id"]
    approved = client.post(
        f"/api/approvals/{approval_id}/approve",
        json={"note": "Exact lead, purpose and script reviewed"},
    )
    assert approved.status_code == 200
    completed = client.post(f"/api/tasks/{task_id}/run")
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
    return completed.json()["result"]


def test_voice_call_requires_consent_owner_approval_and_signed_callback(
    client, monkeypatch
):
    lead = _consented_lead(client, "end-to-end")
    queued = _approved_call(client, lead["id"], "end-to-end")
    assert queued["recording_enabled"] is False

    outbound = {}

    def provider_handler(request: httpx.Request) -> httpx.Response:
        outbound["url"] = str(request.url)
        outbound["json"] = json.loads(request.content)
        outbound["headers"] = request.headers
        return httpx.Response(
            200,
            json={"provider_call_id": "provider-voice-1", "status": "ringing"},
        )

    transport = httpx.MockTransport(provider_handler)
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_gateway_url", "https://voice.example.test/calls")
    monkeypatch.setattr(settings, "telephony_api_token", "test-token")
    monkeypatch.setattr(settings, "telephony_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(settings, "telephony_timezone", "UTC")
    monkeypatch.setattr(settings, "telephony_daily_start_hour", 9)
    monkeypatch.setattr(settings, "telephony_daily_end_hour", 18)
    monkeypatch.setattr(settings, "telephony_calls_per_minute", 1)
    monkeypatch.setattr(settings, "telephony_calls_per_day", 20)
    monkeypatch.setattr(telephony.httpx, "Client", client_factory)

    with SessionLocal() as db:
        assert place_next_sales_call(db, now=datetime(2042, 1, 1, 12, 0)) is True
        call = db.get(BusinessRecord, queued["call_id"])
        assert call is not None
        assert call.status == "ringing"
        assert call.data["provider_call_id"] == "provider-voice-1"

    assert outbound["url"] == "https://voice.example.test/calls"
    assert outbound["json"]["to"] == "+79990001234"
    assert outbound["json"]["recording_enabled"] is False
    assert outbound["json"]["ai_disclosure_required"] is True
    assert outbound["headers"]["Idempotency-Key"] == "voice-test-end-to-end"

    callback = {
        "call_attempt_id": "voice-test-end-to-end",
        "provider_call_id": "provider-voice-1",
        "provider_event_id": "provider-event-1",
        "status": "completed",
        "duration_seconds": 75,
        "outcome_summary": "Customer requested a site survey.",
    }
    raw = json.dumps(callback, separators=(",", ":")).encode()
    signature = hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/api/telephony/callback",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Telephony-Signature": f"sha256={signature}",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    replay = client.post(
        "/api/telephony/callback",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Telephony-Signature": f"sha256={signature}",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True

    with SessionLocal() as db:
        contacts = (
            db.query(ContactEvent)
            .filter(
                ContactEvent.record_id == lead["id"],
                ContactEvent.channel == "phone",
            )
            .all()
        )
        assert len(contacts) == 1
        assert contacts[0].outcome == "completed"


def test_voice_queue_rejects_lead_without_phone_consent(client):
    lead = client.post(
        "/api/records",
        json={
            "record_type": "lead",
            "title": "Voice lead without consent",
            "data": {"phone": "+79990004321"},
        },
    ).json()
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="Verified phone consent"):
            queue_consented_sales_call(
                db,
                lead_id=lead["id"],
                idempotency_key="voice-test-no-consent",
                purpose="Unapproved call",
                script="This must never be queued.",
                scheduled_at=None,
                task_id=1,
                approval_id=1,
            )


def test_telephony_callback_rejects_invalid_signature(client, monkeypatch):
    monkeypatch.setattr(settings, "telephony_webhook_secret", "configured-secret")
    response = client.post(
        "/api/telephony/callback",
        json={
            "call_attempt_id": "voice-test-unknown",
            "provider_call_id": "provider-unknown",
            "provider_event_id": "event-unknown",
            "status": "failed",
        },
        headers={"X-Telephony-Signature": "sha256=" + "0" * 64},
    )
    assert response.status_code == 403


def test_voice_gateway_response_is_stopped_at_size_limit(client, monkeypatch):
    lead = _consented_lead(client, "oversized-provider")
    queued = _approved_call(client, lead["id"], "oversized-provider")
    yielded = []

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(3):
                yielded.append(index)
                yield b"x" * 700

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, stream=OversizedStream())
    )
    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(settings, "telephony_enabled", True)
    monkeypatch.setattr(settings, "telephony_gateway_url", "https://voice.example.test/calls")
    monkeypatch.setattr(settings, "telephony_api_token", "test-token")
    monkeypatch.setattr(settings, "telephony_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(settings, "telephony_timezone", "UTC")
    monkeypatch.setattr(settings, "telephony_daily_start_hour", 9)
    monkeypatch.setattr(settings, "telephony_daily_end_hour", 18)
    monkeypatch.setattr(settings, "telephony_max_response_bytes", 1_024)
    monkeypatch.setattr(telephony.httpx, "Client", client_factory)

    with SessionLocal() as db:
        assert place_next_sales_call(db, now=datetime(2042, 1, 1, 12, 0)) is True
        call = db.get(BusinessRecord, queued["call_id"])
        assert call is not None
        assert call.status == "reconciliation_required"
        assert call.data["failure_category"] == "provider_outcome_unknown"

    assert yielded == [0, 1]


def test_postgres_voice_rate_limit_uses_transaction_advisory_lock():
    executed = []

    class Dialect:
        name = "postgresql"

    class Binding:
        dialect = Dialect()

    class FakeSession:
        @staticmethod
        def get_bind():
            return Binding()

        @staticmethod
        def execute(statement, parameters):
            executed.append((str(statement), parameters))

    telephony._acquire_rate_limit_lock(FakeSession())

    assert executed == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": telephony.TELEPHONY_RATE_LIMIT_LOCK_KEY},
        )
    ]
