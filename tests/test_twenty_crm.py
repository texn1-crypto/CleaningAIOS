from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import scheduler, twenty_crm
from app.db import Base
from app.models import AuditLog, BusinessRecord, OwnerNotification, Task


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _lead(title: str = "УК Северный Дом") -> BusinessRecord:
    return BusinessRecord(
        record_type="lead",
        external_id=f"test:{title}",
        title=title,
        status="owner_review",
        owner="sales",
        source="test",
        data={
            "website": "https://severny-dom.example/contacts",
            "contact_scope": "organization",
            "last_verified_at": "2045-06-05T11:00:00",
            "public_emails": ["info@severny-dom.example"],
            "public_phones": ["+78120000000"],
            "outreach_consent": "not_verified",
            "automatic_outreach": False,
        },
    )


def _configure(monkeypatch, *, api_key: str = "twenty-test-secret") -> None:
    monkeypatch.setattr(twenty_crm.settings, "twenty_enabled", True)
    monkeypatch.setattr(twenty_crm.settings, "twenty_base_url", "https://twenty.example")
    monkeypatch.setattr(twenty_crm.settings, "twenty_api_key", api_key)
    monkeypatch.setattr(twenty_crm.settings, "twenty_sync_batch_size", 25)


def test_missing_twenty_key_is_persisted_and_notified_once(monkeypatch):
    _configure(monkeypatch, api_key="")
    session_factory = _session_factory()
    with session_factory() as db:
        first = twenty_crm.sync_verified_leads(db, now=datetime(2045, 6, 5, 12, 0))
        repeated = twenty_crm.sync_verified_leads(db, now=datetime(2045, 6, 5, 13, 0))

        state = db.scalar(
            select(BusinessRecord).where(
                BusinessRecord.record_type == twenty_crm.TWENTY_STATE_RECORD_TYPE
            )
        )
        assert first["status"] == "credentials_required"
        assert repeated["status"] == "credentials_required"
        assert state is not None
        assert state.status == "credentials_required"
        assert state.data["secret_stored_in_database"] is False
        assert db.scalar(select(func.count()).select_from(OwnerNotification)) == 1


def test_verified_lead_is_created_updated_and_then_skipped_idempotently(monkeypatch):
    _configure(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/graphql":
            return httpx.Response(200, json={"data": {"companies": {"edges": []}}})
        return httpx.Response(200, json={"data": {"id": "twenty-company-1"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    session_factory = _session_factory()
    with session_factory() as db:
        lead = _lead()
        db.add(lead)
        db.flush()

        created = twenty_crm.sync_verified_leads(
            db,
            client=client,
            now=datetime(2045, 6, 5, 12, 0),
        )
        repeated = twenty_crm.sync_verified_leads(
            db,
            client=client,
            now=datetime(2045, 6, 5, 12, 5),
        )
        lead.title = "УК Северный Дом — СПб"
        updated = twenty_crm.sync_verified_leads(
            db,
            client=client,
            now=datetime(2045, 6, 5, 12, 10),
        )

        assert created["status"] == "ready"
        assert created["synced"] == 1
        assert repeated["skipped"] == 1
        assert updated["updated"] == 1
        assert [request.method for request in requests] == ["POST", "POST", "PATCH"]
        assert requests[0].url.path == "/graphql"
        assert requests[1].url.path == "/rest/companies"
        assert requests[2].url.path == "/rest/companies/twenty-company-1"
        payload = json.loads(requests[1].content)
        assert payload == {
            "name": "УК Северный Дом",
            "domainName": {
                "primaryLinkLabel": "severny-dom.example",
                "primaryLinkUrl": "https://severny-dom.example/contacts",
            },
        }
        assert "public_emails" not in payload
        assert "public_phones" not in payload
        assert lead.data["twenty_sync"]["status"] == "synced"
        assert lead.data["twenty_sync"]["remote_id"] == "twenty-company-1"
        db.flush()
        assert db.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.action == "twenty_crm.lead_projected"
            )
        ) == 2


def test_twenty_auth_failure_is_redacted_and_stops_batch(monkeypatch):
    secret = "twenty-secret-that-must-not-persist"
    _configure(monkeypatch, api_key=secret)
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, json={"error": f"invalid key {secret}"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    session_factory = _session_factory()
    with session_factory() as db:
        first = _lead("УК Первая")
        second = _lead("УК Вторая")
        second.external_id = "test:second"
        db.add_all([first, second])
        db.flush()

        result = twenty_crm.sync_verified_leads(
            db,
            client=client,
            now=datetime(2045, 6, 5, 12, 0),
        )

        serialized = json.dumps(
            {
                "result": result,
                "first": first.data,
                "second": second.data,
                "notifications": [row.data for row in db.scalars(select(OwnerNotification)).all()],
            },
            ensure_ascii=False,
        )
        assert result["status"] == "credentials_required"
        assert result["failure_category"] == "credentials_required"
        assert requests == 1
        assert secret not in serialized


def test_timeout_after_create_reconciles_before_retrying_post(monkeypatch):
    _configure(monkeypatch)
    company_exists = False
    create_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal company_exists, create_calls
        if request.url.path == "/graphql":
            edges = []
            if company_exists:
                edges = [
                    {
                        "node": {
                            "id": "remote-after-timeout",
                            "name": "УК Северный Дом",
                            "domainName": {
                                "primaryLinkUrl": "https://severny-dom.example/contacts"
                            },
                        }
                    }
                ]
            return httpx.Response(200, json={"data": {"companies": {"edges": edges}}})
        if request.method == "POST":
            create_calls += 1
            company_exists = True
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"data": {"id": "remote-after-timeout"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    session_factory = _session_factory()
    with session_factory() as db:
        lead = _lead()
        db.add(lead)
        db.flush()

        first = twenty_crm.sync_verified_leads(
            db, client=client, now=datetime(2045, 6, 5, 12, 0)
        )
        second = twenty_crm.sync_verified_leads(
            db, client=client, now=datetime(2045, 6, 5, 12, 15)
        )

        assert first["status"] == "unavailable"
        assert second["status"] == "ready"
        assert second["updated"] == 1
        assert create_calls == 1
        assert lead.data["twenty_sync"]["remote_id"] == "remote-after-timeout"


def test_unavailable_lead_is_rotated_behind_unattempted_lead(monkeypatch):
    _configure(monkeypatch)
    queried_names: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/graphql":
            body = json.loads(request.content)
            name = body["variables"]["filter"]["name"]["eq"]
            queried_names.append(name)
            if name == "УК Первая":
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(200, json={"data": {"companies": {"edges": []}}})
        return httpx.Response(200, json={"data": {"id": "second-company"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    session_factory = _session_factory()
    with session_factory() as db:
        first = _lead("УК Первая")
        second = _lead("УК Вторая")
        second.external_id = "test:second-rotation"
        db.add_all([first, second])
        db.flush()

        twenty_crm.sync_verified_leads(
            db, client=client, now=datetime(2045, 6, 5, 12, 0)
        )
        result = twenty_crm.sync_verified_leads(
            db, client=client, now=datetime(2045, 6, 5, 12, 15)
        )

        assert queried_names[:2] == ["УК Первая", "УК Вторая"]
        assert result["synced"] == 1
        assert second.data["twenty_sync"]["status"] == "synced"


def test_twenty_rejects_insecure_remote_http_and_does_not_follow_redirect(monkeypatch):
    _configure(monkeypatch)
    with pytest.raises(twenty_crm.TwentyCRMConfigurationError):
        twenty_crm._validated_base_url("http://evil.example:3020")

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/token"})

    session_factory = _session_factory()
    with session_factory() as db:
        db.add(_lead())
        db.flush()
        result = twenty_crm.sync_verified_leads(
            db,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            now=datetime(2045, 6, 5, 12, 0),
        )

        assert result["status"] == "unavailable"
        assert len(calls) == 1
        assert calls[0].startswith("https://twenty.example/")


def test_invalid_remote_id_and_concurrent_cycle_fail_closed(monkeypatch):
    _configure(monkeypatch)
    with pytest.raises(twenty_crm.TwentyCRMProviderError) as exc:
        twenty_crm._remote_id({"id": "../admin"})
    assert exc.value.category == "invalid_response"

    monkeypatch.setattr(twenty_crm, "_acquire_sync_lock", lambda db: False)
    session_factory = _session_factory()
    with session_factory() as db:
        result = twenty_crm.sync_verified_leads(db)
        assert result["status"] == "already_running"
        assert result["attempted"] == 0


def test_personal_qualified_lead_without_verified_organization_is_never_projected(monkeypatch):
    _configure(monkeypatch)
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise AssertionError("personal lead must not reach Twenty")

    personal = BusinessRecord(
        record_type="lead",
        external_id="public-form:personal",
        title="Иван Иванов",
        status="qualified",
        source="public_website",
        data={
            "name": "Иван Иванов",
            "email": "private@example.com",
            "consent_version": "website-privacy-v1",
        },
    )
    session_factory = _session_factory()
    with session_factory() as db:
        db.add(personal)
        db.flush()
        result = twenty_crm.sync_verified_leads(
            db,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            now=datetime(2045, 6, 5, 12, 0),
        )

        assert result["status"] == "ready"
        assert result["eligible"] == 0
        assert result["attempted"] == 0
        assert requests == 0
        assert "twenty_sync" not in personal.data


def test_name_only_company_is_not_reconciled_to_unrelated_twenty_record(monkeypatch):
    _configure(monkeypatch)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/rest/companies"
        return httpx.Response(200, json={"data": {"id": "new-company"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    remote_id, operation = twenty_crm._request_company(
        client,
        base_url="https://twenty.example",
        lead_id=7,
        fingerprint="a" * 64,
        payload={"name": "Одноимённая компания"},
        remote_id="",
    )

    assert remote_id == "new-company"
    assert operation == "created"
    assert paths == ["/rest/companies"]


def test_scheduler_queues_one_twenty_projection_per_window(monkeypatch):
    session_factory = _session_factory()
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler.settings, "twenty_enabled", True)
    monkeypatch.setattr(scheduler.settings, "twenty_api_key", "test-key")
    monkeypatch.setattr(scheduler.settings, "twenty_sync_interval_minutes", 15)
    monkeypatch.setattr(scheduler.settings, "tender_sources", "")
    monkeypatch.setattr(scheduler.settings, "perplexity_api_key", "")
    monkeypatch.setattr(scheduler.settings, "evolution_research_queries", "")

    scheduler.schedule_cycle()
    scheduler.schedule_cycle()

    with session_factory() as db:
        tasks = db.scalars(
            select(Task).where(
                Task.agent_type == "sales",
                Task.payload["action"].as_string() == "sync_twenty_verified_leads",
            )
        ).all()
        assert len(tasks) == 1
        assert tasks[0].payload["projection_only"] is True
        assert tasks[0].payload["automatic_outreach"] is False


def test_scheduler_does_not_queue_twenty_projection_without_credentials(monkeypatch):
    session_factory = _session_factory()
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler.settings, "twenty_enabled", True)
    monkeypatch.setattr(scheduler.settings, "twenty_api_key", "")
    monkeypatch.setattr(scheduler.settings, "tender_sources", "")
    monkeypatch.setattr(scheduler.settings, "perplexity_api_key", "")
    monkeypatch.setattr(scheduler.settings, "evolution_research_queries", "")

    scheduler.schedule_cycle()

    with session_factory() as db:
        assert not db.scalar(
            select(Task.id).where(
                Task.agent_type == "sales",
                Task.payload["action"].as_string() == "sync_twenty_verified_leads",
            )
        )


def test_scheduler_backs_off_after_twenty_credential_failure(monkeypatch):
    session_factory = _session_factory()
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler.settings, "twenty_enabled", True)
    monkeypatch.setattr(scheduler.settings, "twenty_api_key", "configured-but-rejected")
    monkeypatch.setattr(scheduler.settings, "tender_sources", "")
    monkeypatch.setattr(scheduler.settings, "perplexity_api_key", "")
    monkeypatch.setattr(scheduler.settings, "evolution_research_queries", "")

    with session_factory() as db:
        db.add(
            Task(
                title="Recent Twenty credential failure",
                agent_type="sales",
                status="blocked",
                payload={"action": "sync_twenty_verified_leads"},
                result={
                    "status": "unavailable",
                    "credentials_required": ["TWENTY_API_KEY"],
                },
            )
        )
        db.commit()

    scheduler.schedule_cycle()

    with session_factory() as db:
        assert db.scalar(
            select(func.count(Task.id)).where(
                Task.payload["action"].as_string() == "sync_twenty_verified_leads"
            )
        ) == 1


def test_scheduler_backs_off_after_lead_provider_credential_failure(monkeypatch):
    session_factory = _session_factory()
    monkeypatch.setattr(scheduler, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler.settings, "twenty_enabled", False)
    monkeypatch.setattr(scheduler.settings, "tender_sources", "")
    monkeypatch.setattr(scheduler.settings, "perplexity_api_key", "configured-but-rejected")
    monkeypatch.setattr(scheduler.settings, "evolution_research_queries", "")

    with session_factory() as db:
        db.add(
            Task(
                title="Recent lead provider credential failure",
                agent_type="management_lead_scout",
                status="blocked",
                payload={"action": "discover_public_business_leads"},
                result={
                    "status": "unavailable",
                    "handoff_status": "credentials_required",
                },
            )
        )
        db.commit()

    scheduler.schedule_cycle()

    with session_factory() as db:
        assert not db.scalar(
            select(Task.id).where(
                Task.title.like("Lead intelligence coordination · %")
            )
        )


def test_integration_catalog_reports_twenty_without_secret(client, monkeypatch):
    monkeypatch.setattr(twenty_crm.settings, "twenty_enabled", True)
    monkeypatch.setattr(twenty_crm.settings, "twenty_api_key", "")

    result = client.get("/api/integrations").json()

    assert result["twenty_crm"] == {
        "status": "credentials_required",
        "mode": "verified_lead_projection",
        "system_of_record": "cleaningaios",
        "automatic_outreach": False,
    }

    runtime = client.get("/api/integrations/twenty").json()
    assert runtime["configuration_status"] == "credentials_required"
    assert runtime["system_of_record"] == "cleaningaios"
    assert runtime["automatic_outreach"] is False
