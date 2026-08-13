import asyncio
from pathlib import Path

import pytest


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_task_and_agent_flow(client):
    created = client.post("/api/tasks", json={"title": "Проверить тендер", "agent_type": "tender"})
    assert created.status_code == 201
    task_id = created.json()["id"]
    result = client.post(f"/api/tasks/{task_id}/run")
    assert result.status_code == 200
    assert result.json()["status"] == "done"
    assert result.json()["result"]["submission_requires_owner_approval"] is True

    transitions = client.get(f"/api/tasks/{task_id}/transitions").json()
    assert [(row["from_status"], row["to_status"], row["reason"]) for row in transitions] == [
        ("", "open", "task_created"),
        ("open", "running", "agent_execution_started"),
        ("running", "done", "execution_evidence_verified"),
    ]
    assert client.post(f"/api/tasks/{task_id}/run").status_code == 409


def test_failed_task_retry_has_explicit_transition_history(client, monkeypatch):
    from app.agents import AGENTS

    class FailingAgent:
        name = "transition_test"

        def execute(self, db, payload):
            raise RuntimeError("controlled retry failure")

    monkeypatch.setitem(AGENTS, "transition_test", FailingAgent())
    task = client.post("/api/tasks", json={
        "title": "Transition retry test",
        "agent_type": "transition_test",
        "max_attempts": 2,
    }).json()
    first = client.post(f"/api/tasks/{task['id']}/run")
    assert first.status_code == 200
    assert first.json()["status"] == "queued"
    transitions = client.get(f"/api/tasks/{task['id']}/transitions").json()
    assert [(row["from_status"], row["to_status"]) for row in transitions] == [
        ("", "open"),
        ("open", "running"),
        ("running", "failed"),
        ("failed", "queued"),
    ]
    assert transitions[-1]["reason"] == "retry_scheduled"


def test_default_orchestrator_task_runs(client):
    task = client.post("/api/tasks", json={"title": "Общая операционная задача", "payload": {"message": "Проверить объект"}}).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()
    assert result["status"] == "done"
    assert result["result"]["coordinated"] is True


def test_owner_approval_gate(client):
    task = client.post("/api/tasks", json={"title": "Подать заявку", "agent_type": "tender", "payload": {"action_kind": "tender_submission"}}).json()
    blocked = client.post(f"/api/tasks/{task['id']}/run").json()
    assert blocked["status"] == "blocked"
    assert blocked["result"]["reason"] == "owner_approval_required"
    approval_id = blocked["result"]["approval_id"]
    approved = client.post(f"/api/approvals/{approval_id}/approve", json={"note": "Проверено владельцем"})
    assert approved.status_code == 200
    approval_history = client.get(f"/api/tasks/{task['id']}/transitions").json()
    assert approval_history[-1]["to_status"] == "queued"
    assert approval_history[-1]["reason"] == "owner_approval_granted"
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    assert completed["status"] == "done"


def test_company_brain_versions_knowledge(client):
    payload = {"namespace": "company", "key": "service_area", "value": {"city": "Москва"}, "confidence": 0.9}
    assert client.put("/api/brain", json=payload).json()["version"] == 1
    payload["value"] = {"city": "Москва", "radius_km": 50}
    assert client.put("/api/brain", json=payload).json()["version"] == 2
    snapshot = client.get("/api/brain?namespace=company").json()
    assert snapshot["company.service_area"]["value"]["radius_km"] == 50


def test_record_emits_domain_event(client):
    from uuid import UUID

    record = client.post("/api/records", json={"record_type": "lead", "title": "УК Север"})
    assert record.status_code == 201
    events = client.get("/api/events", headers={"X-Role": "manager"}).json()
    event = next(x for x in events if x["event_type"] == "lead.created" and x["aggregate_id"] == str(record.json()["id"]))
    assert str(UUID(event["event_id"])) == event["event_id"]
    assert event["schema_version"] == 1
    assert event["correlation_id"] == event["event_id"]
    assert event["causation_id"] == ""
    assert event["actor"] == "api-user"
    assert event["occurred_at"]
    assert event["deliveries"] == []


def test_sales_lifecycle_contacts_and_summary(client):
    lead = client.post("/api/records", json={"record_type": "lead", "title": "БЦ Восток", "status": "new", "data": {"budget": 120000}}).json()
    changed = client.patch(f"/api/records/{lead['id']}", json={"status": "qualified", "data": {"next_action": "send_proposal"}})
    assert changed.status_code == 200
    assert changed.json()["data"] == {"budget": 120000, "next_action": "send_proposal"}
    touch = client.post(f"/api/records/{lead['id']}/contacts", json={"channel": "phone", "direction": "outbound", "outcome": "meeting_booked"})
    assert touch.status_code == 201
    assert len(client.get(f"/api/records/{lead['id']}/contacts").json()) == 1
    sales = client.get("/api/modules/summary").json()["sales"]
    assert sales["qualified"] >= 1
    assert sales["pipeline_amount"] >= 120000


def test_domain_rules_prevent_incomplete_records(client):
    invalid_type = client.post("/api/records", json={"record_type": "Invalid Type", "title": "Невалидный тип"})
    assert invalid_type.status_code == 422
    lost = client.post("/api/records", json={"record_type": "lead", "title": "Неполный лид", "status": "lost"})
    assert lost.status_code == 422
    finance = client.post("/api/records", json={"record_type": "expense", "title": "Без суммы", "status": "pending"})
    assert finance.status_code == 422
    tender = client.post("/api/records", json={"record_type": "tender", "title": "Без срока", "status": "preparing"})
    assert tender.status_code == 422


def test_event_bus_routes_domain_work_to_agent(client):
    from app.db import SessionLocal
    from app.platform import process_next_event

    lead = client.post("/api/records", json={"record_type": "lead", "title": "Маршрутизируемый лид"}).json()
    with SessionLocal() as db:
        while process_next_event(db):
            pass
    tasks = client.get("/api/tasks").json()
    assert any(x["agent_type"] == "sales" and x["payload"].get("record_id") == str(lead["id"]) for x in tasks)


def test_event_bus_consumer_receipt_prevents_duplicate_routing(client):
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import DomainEvent, EventConsumerReceipt, Task
    from app.platform import event_bus, process_next_event

    aggregate_id = "receipt-idempotency-test"
    with SessionLocal() as db:
        event = event_bus.publish(
            db,
            "lead.created",
            "lead",
            aggregate_id,
            {"title": "Receipt test"},
            idempotency_key="test:event-receipt-idempotency",
            actor="test-suite",
            correlation_id="correlation-receipt-test",
        )
        db.commit()
        event_db_id = event.id
        event_uid = event.event_id

        while True:
            processed = process_next_event(db)
            current = db.get(DomainEvent, event_db_id)
            if current and current.status == "published":
                break
            assert processed is not None

        receipt = db.scalar(select(EventConsumerReceipt).where(
            EventConsumerReceipt.event_id == event_db_id,
            EventConsumerReceipt.consumer == "domain_router",
        ))
        assert receipt is not None
        assert receipt.status == "succeeded"
        assert receipt.attempts == 1
        assert receipt.result_ref.startswith("task:")
        routed_before = db.scalar(select(func.count(Task.id)).where(Task.payload["event_uid"].as_string() == event_uid))

        current.status = "pending"
        db.commit()
        process_next_event(db)
        routed_after = db.scalar(select(func.count(Task.id)).where(Task.payload["event_uid"].as_string() == event_uid))
        db.refresh(receipt)
        assert routed_after == routed_before == 1
        assert receipt.attempts == 1

    api_event = next(row for row in client.get("/api/events", headers={"X-Role": "manager"}).json() if row["event_id"] == event_uid)
    assert len(api_event["deliveries"]) == 1
    delivery = api_event["deliveries"][0]
    assert delivery["consumer"] == "domain_router"
    assert delivery["status"] == "succeeded"
    assert delivery["attempts"] == 1
    assert delivery["result_ref"].startswith("task:")
    assert delivery["last_error"] == ""
    assert delivery["processed_at"]


def test_event_bus_persists_failed_consumer_delivery(monkeypatch):
    from pydantic import ValidationError
    from sqlalchemy import select

    from app import platform
    from app.db import SessionLocal
    from app.models import DomainEvent, EventConsumerReceipt

    with SessionLocal() as db:
        while platform.process_next_event(db):
            pass
        with pytest.raises(ValidationError):
            platform.event_bus.publish(db, "Invalid Event", "lead", "invalid")
        db.rollback()

        event = platform.event_bus.publish(
            db,
            "lead.created",
            "lead",
            "receipt-failure-test",
            idempotency_key="test:event-receipt-failure",
        )
        db.commit()
        event_db_id = event.id

        def fail_route(db, event):
            raise RuntimeError("consumer route failed")

        monkeypatch.setattr(platform, "route_event", fail_route)
        with pytest.raises(RuntimeError, match="consumer route failed"):
            platform.process_next_event(db)

        failed_event = db.get(DomainEvent, event_db_id)
        receipt = db.scalar(select(EventConsumerReceipt).where(
            EventConsumerReceipt.event_id == event_db_id,
            EventConsumerReceipt.consumer == "domain_router",
        ))
        assert failed_event.status == "pending"
        assert failed_event.attempts == 1
        assert receipt is not None
        assert receipt.status == "failed"
        assert receipt.attempts == 1
        assert receipt.last_error == "consumer route failed"


def test_company_graph_economics_and_simulator(client):
    customer = client.post("/api/entities", json={"entity_type": "client", "name": "УК Тест"}).json()
    site = client.post("/api/entities", json={"entity_type": "site", "name": "ЖК Тест", "parent_id": customer["id"], "data": {"materials_cost": 10000, "logistics_cost": 5000}}).json()
    contract = client.post("/api/entities", json={"entity_type": "contract", "name": "Контракт ЖК", "parent_id": site["id"], "data": {"monthly_revenue": 200000}})
    assert contract.status_code == 201
    client.post("/api/entities", json={"entity_type": "shift", "name": "Дневная смена", "parent_id": site["id"], "data": {"payroll_cost": 90000, "employee_id": 1}})
    graph = client.get("/api/company/graph").json()
    assert any(x["id"] == customer["id"] and x["children"][0]["id"] == site["id"] for x in graph["roots"])
    economics = client.get(f"/api/finance/site-economics?site_id={site['id']}").json()[0]
    assert economics["profit"] == 95000
    simulation = client.post("/api/simulations", json={"site_id": site["id"], "payroll_change_percent": 10}).json()
    assert simulation["base"]["profit"] == 86000


def test_goals_structured_decisions_and_outcomes(client):
    goal = client.post("/api/goals", headers={"X-Role": "manager"}, json={"title": "Рост прибыли", "metric": "monthly_profit", "baseline": 100, "target": 200}).json()
    progress = client.patch(f"/api/goals/{goal['id']}/progress", json={"current": 150}).json()
    assert progress["progress_percent"] == 50
    decision = client.post("/api/structured-decisions", json={"title": "Увеличить резерв", "problem": "Незакрытые смены", "confidence": 0.87, "options": [{"id": "A"}]}).json()
    outcome = client.put(f"/api/decisions/{decision['id']}/outcome", headers={"X-Role": "manager"}, json={"successful": True, "actual_value": 42}).json()
    assert outcome["successful"] is True


def test_tender_scoring_and_document_registry(client):
    tender = client.post("/api/records", json={"record_type": "tender", "title": "Клининг БЦ", "data": {"expected_margin": 80, "company_fit": 90, "competition_risk": 20, "contract_risk": 20, "logistics_fit": 90, "staffing_fit": 80, "strategic_value": 60}}).json()
    score = client.post(f"/api/tenders/{tender['id']}/score").json()
    assert score["score"] >= 80
    document = client.post(f"/api/tenders/{tender['id']}/documents", json={"name": "ТЗ.pdf", "source_url": "https://example.invalid/spec.pdf", "analysis": {"penalties": "1% per day"}})
    assert document.status_code == 201
    assert document.json()["status"] == "analyzed"


def test_csv_import_and_bulk_campaign_approval(client):
    import base64
    content = base64.b64encode("company,email,budget\nУК Альфа,alpha@example.com,500000\n,missing@example.com,10\n".encode()).decode()
    imported = client.post("/api/imports/leads", json={"filename": "leads.csv", "content_base64": content}).json()
    assert imported["imported_rows"] == 1
    launch = {"campaign_key": "approved-campaign", "recipients": ["alpha@example.com", "second@example.com"], "subject": "Клининг", "body": "Предложение"}
    blocked = client.post("/api/outreach/campaigns/launch", headers={"X-Role": "manager"}, json=launch).json()
    assert blocked["status"] == "waiting_approval"
    client.post(f"/api/approvals/{blocked['approval_id']}/approve", json={"note": "Разрешаю"})
    launch["approval_id"] = blocked["approval_id"]
    queued = client.post("/api/outreach/campaigns/launch", headers={"X-Role": "manager"}, json=launch).json()
    assert queued["status"] == "queued"
    assert queued["queued"] == 2


def test_bulk_campaign_approval_cannot_authorize_changed_content(client):
    launch = {"campaign_key": "tamper-proof-campaign", "recipients": ["one@example.com"], "subject": "Original", "body": "Original body"}
    blocked = client.post("/api/outreach/campaigns/launch", headers={"X-Role": "manager"}, json=launch).json()
    client.post(f"/api/approvals/{blocked['approval_id']}/approve", json={"note": "Original approved"})
    changed = {**launch, "subject": "Changed", "approval_id": blocked["approval_id"]}
    second = client.post("/api/outreach/campaigns/launch", headers={"X-Role": "manager"}, json=changed).json()
    assert second["status"] == "waiting_approval"
    assert second["approval_id"] != blocked["approval_id"]


def test_xlsx_lead_import(client):
    import base64
    import io
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["company", "email", "district"])
    sheet.append(["ТСЖ Excel", "excel@example.com", "ЮЗАО"])
    stream = io.BytesIO(); workbook.save(stream)
    payload = {"filename": "leads.xlsx", "content_base64": base64.b64encode(stream.getvalue()).decode()}
    result = client.post("/api/imports/leads", json=payload)
    assert result.status_code == 201
    assert result.json()["imported_rows"] == 1


def test_import_size_limit_and_mailbox_secret_reference(client, monkeypatch):
    import base64
    from app.config import settings

    monkeypatch.setattr(settings, "max_import_bytes", 3)
    oversized = client.post("/api/imports/leads", json={"filename": "large.csv", "content_base64": base64.b64encode(b"four").decode()})
    assert oversized.status_code == 413
    invalid_secret = client.post("/api/outreach/mailboxes", json={"name": "Unsafe mailbox", "address": "safe@example.com", "secret_ref": "API_KEY"})
    assert invalid_secret.status_code == 422


def test_structured_decision_creates_bound_approval(client):
    decision = client.post("/api/structured-decisions", json={"title": "Подписать договор", "problem": "Новый объект", "requires_approval": True, "approval_kind": "contract"}).json()
    assert decision["approval_id"] is not None
    approved = client.post(f"/api/approvals/{decision['approval_id']}/approve", json={"note": "Проверено"})
    assert approved.status_code == 200
    decisions = client.get("/api/decisions").json()
    assert next(x for x in decisions if x["id"] == decision["id"])["status"] == "approved"


def test_integration_status_is_truthful(client):
    status = client.get("/api/integrations").json()
    assert status["tender_sources"]["status"] in {"configured", "source_configuration_required"}
    assert status["llm"]["status"] in {"configured", "credentials_required", "model_configuration_required"}
    assert status["llm"]["provider"] == "openai_compatible_responses"


def test_llm_adapter_uses_structured_responses_contract(monkeypatch):
    import json
    from app.config import settings
    from app import llm

    captured = {}

    class Response:
        def raise_for_status(self): return None
        def json(self):
            output = {
                "summary": "Стабильное состояние",
                "risks": ["Один риск"],
                "data_gaps": ["Нет данных источника"],
                "recommendations": [{"title": "Проверить маржу", "agent_type": "finance", "rationale": "Маржа требует проверки", "priority": "high", "needs_owner_decision": False}],
            }
            return {"status": "completed", "model": "test-model", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}], "usage": {"total_tokens": 42}}

    class Client:
        def __init__(self, *args, **kwargs): captured["headers"] = kwargs["headers"]
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, json): captured.update({"url": url, "payload": json}); return Response()

    monkeypatch.setattr(llm.httpx, "Client", Client)
    monkeypatch.setattr(settings, "llm_api_key", "test-secret")
    monkeypatch.setattr(settings, "llm_base_url", "https://api.example/v1")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    result = llm.llm_advisor.review({"business_health": 90})
    assert result["status"] == "succeeded"
    assert result["recommendations"][0]["agent_type"] == "finance"
    assert captured["url"] == "https://api.example/v1/responses"
    assert captured["payload"]["text"]["format"]["strict"] is True
    assert captured["payload"]["store"] is False
    assert captured["headers"]["Authorization"] == "Bearer test-secret"


def test_ai_ceo_falls_back_without_llm_credentials(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    task = client.post("/api/tasks", json={"title": "CEO fallback check", "agent_type": "ceo"}).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()["result"]
    assert result["llm_advice"]["status"] == "credentials_required"
    assert isinstance(result["business_health"], int)


def test_ai_ceo_creates_only_safe_llm_tasks(client, monkeypatch):
    from app.agents import llm_advisor

    monkeypatch.setattr(llm_advisor, "review", lambda snapshot: {
        "status": "succeeded",
        "model": "test-model",
        "summary": "Test",
        "risks": [],
        "data_gaps": [],
        "recommendations": [
            {"title": "LLM safe finance analysis unique", "agent_type": "finance", "rationale": "Проверить данные", "priority": "high", "needs_owner_decision": False},
            {"title": "LLM protected commitment unique", "agent_type": "finance", "rationale": "Оплатить счет", "priority": "high", "needs_owner_decision": True},
        ],
    })
    task = client.post("/api/tasks", json={"title": "CEO LLM task check", "agent_type": "ceo"}).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()["result"]
    created = {x["title"]: x for x in result["tasks_created"]}
    assert created["LLM safe finance analysis unique"]["source"] == "llm_advisory"
    assert "LLM protected commitment unique" not in created
    queued = {x["title"]: x for x in client.get("/api/tasks").json()}
    assert queued["LLM safe finance analysis unique"]["payload"]["advisory_only"] is True


def test_research_agent_runs_real_collector_contract(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tender_sources", "")
    task = client.post("/api/tasks", json={"title": "Collect configured tender feeds", "agent_type": "research"}).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()
    assert result["status"] == "done"
    assert result["result"]["collection"] == "tenders"
    assert result["result"]["status"] == "source_configuration_required"
    assert result["result"]["created"] == 0


def test_mission_control_renders(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "company_name", "CleaningAIOS")
    response = client.get("/")
    assert response.status_code == 200
    assert "Порядок, который работает на ваш бизнес" in response.text
    assert 'content="http://testserver/static/og-cleaningaios.png"' in response.text
    assert "CleaningAIOS — профессиональный клининг" in response.text
    assert "__COMPANY_NAME__" not in response.text
    assert client.get("/mission-control").status_code == 200
    assert "Mission Control" in client.get("/mission-control").text


def test_unified_inbox_content_and_operations_views(client):
    lead = client.post("/api/records", json={"record_type": "lead", "title": "Лид из inbox"}).json()
    message = client.post("/api/inbox", json={"channel": "email", "external_id": "mail-001", "sender": "client@example.com", "subject": "Запрос цены", "record_id": lead["id"]})
    assert message.status_code == 201
    assert client.post("/api/inbox", json={"channel": "email", "external_id": "mail-001"}).status_code == 409
    updated = client.patch(f"/api/inbox/{message.json()['id']}", json={"status": "assigned", "record_id": lead["id"]}).json()
    assert updated["status"] == "assigned"
    content = client.post("/api/marketing/content", json={"channel": "telegram", "title": "Преимущества уборки", "status": "draft"})
    assert content.status_code == 201
    assert len(client.get("/api/marketing/content?status=draft").json()) == 1


def test_staffing_vacancy_draft_payment_calendar_and_quality(client):
    customer = client.post("/api/entities", json={"entity_type": "client", "name": "Клиент процессов"}).json()
    site = client.post("/api/entities", json={"entity_type": "site", "name": "Объект процессов", "parent_id": customer["id"]}).json()
    vacancy = client.post("/api/entities", json={"entity_type": "vacancy", "name": "Уборщик в БЦ", "data": {"district": "САО", "schedule": "2/2", "rate": 3500}}).json()
    client.post("/api/entities", json={"entity_type": "shift", "name": "Незакрытая смена", "parent_id": site["id"], "data": {"payroll_cost": 3500}})
    client.post("/api/entities", json={"entity_type": "complaint", "name": "Жалоба на качество", "parent_id": site["id"], "status": "open", "data": {"sla_deadline": "2020-01-01T00:00:00+00:00"}})
    staffing = client.get("/api/hr/staffing").json()
    assert len(staffing["unfilled_shifts"]) >= 1
    assert "САО" in client.get(f"/api/hr/vacancies/{vacancy['id']}/telegram-draft").json()["text"]
    client.post("/api/records", json={"record_type": "payment", "title": "Платёж клиента", "status": "overdue", "data": {"amount": 10000}, "deadline_at": "2020-01-01T00:00:00"})
    assert client.get("/api/finance/payment-calendar").json()[0]["overdue"] is True
    assert client.get("/api/operations/quality").json()["sla_breached"] >= 1


def test_tender_feed_and_document_download(client, monkeypatch, tmp_path):
    from app.config import settings
    from app import integrations

    feed = {"items": [{"external_id": "feed-1", "title": "Уборка бизнес-центра", "deadline_at": "2030-01-01T12:00:00Z", "data": {"expected_margin": 80, "company_fit": 90}, "documents": [{"name": "contract.pdf", "url": "https://feed.example/contract.pdf", "content_type": "application/pdf"}]}]}

    class Response:
        headers = {"content-type": "application/pdf", "content-length": "7"}
        def raise_for_status(self): return None
        def json(self): return feed
        def iter_bytes(self): yield b"PDFDATA"
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def get(self, url): return Response()
        def stream(self, method, url): return Response()

    monkeypatch.setattr(integrations.httpx, "Client", Client)
    monkeypatch.setattr(integrations.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(settings, "tender_sources", "https://feed.example/tenders")
    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    collected = client.post("/api/tender-sources/collect", headers={"X-Role": "manager"}).json()
    assert collected["created"] == 1
    from app.db import SessionLocal
    from app.models import TenderDocument
    from sqlalchemy import select
    with SessionLocal() as db:
        document_id = db.scalar(select(TenderDocument.id).where(TenderDocument.source_url == "https://feed.example/contract.pdf"))
    downloaded = client.post(f"/api/tender-documents/{document_id}/download").json()
    assert downloaded["status"] == "downloaded"
    assert downloaded["bytes"] == 7


def test_delivery_event_suppresses_bounced_recipient(client):
    queued = client.post("/api/outreach/messages", json={"campaign_key": "bounce-test", "recipient": "bounce@example.com", "subject": "Test", "body": "Body"}).json()
    event = client.post("/api/outreach/delivery-events", json={"event_type": "bounce", "recipient": "bounce@example.com", "message_id": queued["id"], "reason": "mailbox unavailable"})
    assert event.status_code == 201
    assert event.json()["suppressed"] is True
    retry = client.post("/api/outreach/messages", json={"campaign_key": "bounce-retry", "recipient": "bounce@example.com", "subject": "Test", "body": "Body"})
    assert retry.status_code == 409


def test_worker_sends_real_attachment(monkeypatch):
    import base64
    from sqlalchemy import update
    from app.config import settings
    from app.db import SessionLocal
    from app.models import OutboundMessage
    from app import worker

    sent = []
    class SMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def starttls(self): pass
        def login(self, username, password): pass
        def send_message(self, message): sent.append(message)

    monkeypatch.setattr(worker.smtplib, "SMTP", SMTP)
    for field, value in {"smtp_host": "smtp.example", "smtp_username": "user", "smtp_password": "secret", "smtp_from_email": "sender@example.com"}.items(): monkeypatch.setattr(settings, field, value)
    with SessionLocal() as db:
        db.execute(update(OutboundMessage).where(OutboundMessage.status.in_(["queued", "waiting_configuration"])).values(status="sent"))
        row = OutboundMessage(campaign_key="attachment-test", recipient="attach@example.com", subject="Документ", body="Смотрите вложение", attachments=[{"filename": "offer.txt", "content_type": "text/plain", "content_base64": base64.b64encode(b"offer").decode()}])
        db.add(row); db.commit(); row_id = row.id
        assert worker.send_next_email(db) is True
        db.refresh(row); assert row.status == "sent"
    assert sent and sent[0].is_multipart()
    assert any(part.get_filename() == "offer.txt" for part in sent[0].walk())
    assert "token=" in sent[0].get_body(preferencelist=("plain",)).get_content()


def test_outreach_suppression_and_deduplication(client):
    assert client.post("/api/outreach/suppress", json={"address": "stop@example.com"}).status_code == 201
    blocked = client.post("/api/outreach/messages", json={"campaign_key": "c1", "recipient": "stop@example.com", "subject": "Hi", "body": "Body"})
    assert blocked.status_code == 409
    payload = {"campaign_key": "c1", "recipient": "ok@example.com", "subject": "Hi", "body": "Body"}
    assert client.post("/api/outreach/messages", json=payload).status_code == 201
    assert client.post("/api/outreach/messages", json=payload).status_code == 409


def test_rbac_rejects_viewer_write(client):
    response = client.post("/api/tasks", headers={"X-Role": "viewer"}, json={"title": "Denied"})
    assert response.status_code == 403


def test_production_rbac_derives_role_from_api_key(monkeypatch):
    from fastapi import HTTPException
    from app.config import settings
    from app.security import principal, validate_production_security

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "api_key", "owner-secret")
    monkeypatch.setattr(settings, "manager_api_key", "manager-secret")
    monkeypatch.setattr(settings, "operator_api_key", "operator-secret")
    monkeypatch.setattr(settings, "viewer_api_key", "viewer-secret")
    validate_production_security()
    authenticated = principal(x_api_key="manager-secret", x_actor="spoofed-owner", x_role="owner")
    assert authenticated.role == "manager"
    assert authenticated.subject == "manager-api-key"
    with pytest.raises(HTTPException) as exc:
        principal(x_api_key="unknown", x_actor="api-user", x_role="owner")
    assert exc.value.status_code == 401


def test_tender_url_guard_blocks_private_networks(monkeypatch):
    from fastapi import HTTPException
    from app import integrations

    with pytest.raises(HTTPException):
        integrations._safe_url("http://127.0.0.1/admin")
    monkeypatch.setattr(integrations.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 443))])
    with pytest.raises(HTTPException):
        integrations._safe_url("https://feed.example/tenders")


def test_unsubscribe_link_requires_valid_signature(client):
    from app.security import unsubscribe_token

    address = "signed-unsubscribe@example.com"
    assert client.get(f"/api/outreach/unsubscribe?email={address}&token=invalid").status_code == 403
    response = client.get(f"/api/outreach/unsubscribe?email={address}&token={unsubscribe_token(address)}")
    assert response.status_code == 200
    assert response.json()["unsubscribed"] is True


def test_telegram_requires_owner_id(monkeypatch):
    from app.config import settings
    from app.bot import build_application

    monkeypatch.setattr(settings, "telegram_bot_token", "123456:fake-token-for-startup-check")
    monkeypatch.setattr(settings, "owner_telegram_id", "")
    with pytest.raises(RuntimeError, match="OWNER_TELEGRAM_ID is empty"):
        build_application()


def test_russian_chat_reads_existing_sections():
    from app.chat import understand_russian_message

    assert understand_russian_message("Покажи текущие задачи")["kind"] == "tasks"
    assert understand_russian_message("Что с тендерами?")["record_type"] == "tender"
    assert understand_russian_message("Покажи финансы")["module"] == "finance"
    assert understand_russian_message("Как дела у системы?")["kind"] == "dashboard"
    assert understand_russian_message("Пришли отчет о проделанной работе")["kind"] == "activity_report"
    assert understand_russian_message("Запусти весь функционал чат бота")["kind"] == "system_self_check"
    assert understand_russian_message("Сколько нужно времени на выполнение задачи?") == {
        "kind": "task_eta",
        "task_id": None,
    }
    assert understand_russian_message("Когда будет готова задача 42?") == {
        "kind": "task_eta",
        "task_id": 42,
    }


def test_task_timing_report_does_not_treat_empty_agent_summary_as_completion(client):
    from app.chat import understand_russian_message

    message = (
        "Собери базу управляющих компаний из всех возможных источников, "
        "найди сайты и почты, проверь их и выгрузи PDF, XLSX и Word"
    )
    intent = understand_russian_message("Сколько нужно времени на выполнение задачи?")
    assessment = client.post(
        "/api/request-analysis",
        json={"message": "Сколько нужно времени на выполнение задачи?", "intent": intent},
    ).json()
    assert assessment["classification"] == "supported"
    assert assessment["improvement_id"] is None

    business_task = client.post("/api/tasks", json={
        "title": message,
        "agent_type": "marketing",
        "payload": {
            "source": "telegram_natural_language",
            "original_message": message,
        },
    }).json()
    quality_gated = client.post(f"/api/tasks/{business_task['id']}/run").json()
    assert quality_gated["status"] == "blocked"
    assert quality_gated["result"]["improvement_id"]
    assert quality_gated["result"]["ceo_incident_task_id"]

    timing_task = client.post("/api/tasks", json={
        "title": "Оценить срок выполнения задачи",
        "agent_type": "orchestrator",
        "payload": {
            "action": "task_timing_report",
            "task_id": None,
            "source": "telegram_read_request",
        },
        "max_attempts": 1,
    }).json()
    completed = client.post(f"/api/tasks/{timing_task['id']}/run").json()
    result = completed["result"]

    assert completed["status"] == "done"
    assert result["task"]["id"] == business_task["id"]
    assert result["timing_status"] == "blocked"
    assert result["result_verified"] is False
    assert result["remaining_seconds"] is None
    assert result["actual_runtime_seconds"] is not None
    assert result["planning_estimate"]["min_hours"] == 8
    assert result["planning_estimate"]["max_hours"] == 24
    assert any(
        row["action"] == "task.completed" and row["resource_id"] == str(timing_task["id"])
        for row in client.get("/api/audit").json()
    )


def test_telegram_task_timing_returns_truthful_result(monkeypatch):
    from app import bot

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/request-analysis":
            return {"classification": "supported", "improvement_id": None}
        if path == "/api/tasks":
            return {"id": 601}
        if path == "/api/tasks/601/run":
            return {
                "status": "done",
                "result": {
                    "outcome": "completed",
                    "task": {"id": 9, "title": "Собрать базу УК", "status": "done"},
                    "timing_status": "result_unverified",
                    "actual_runtime_seconds": 0.013,
                    "planning_estimate": {
                        "min_hours": 8,
                        "max_hours": 24,
                        "confidence": "low",
                        "starts_after": ["подключены источники"],
                    },
                    "reason": "Подтвержденный результат отсутствует.",
                },
            }
        raise AssertionError(path)

    class Message:
        text = "Сколько нужно времени на выполнение задачи?"

        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append(value)

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(bot, "allowed", lambda update: True)
    monkeypatch.setattr(bot, "api", fake_api)
    asyncio.run(bot.natural_language(Update(), None))

    reply = Update.effective_message.replies[-1]
    assert "Задача #9" in reply
    assert "результата или файла нет" in reply
    assert "8–24 ч" in reply
    assert "это не равно" in reply
    task_payload = next(kwargs["json"] for _, path, kwargs in calls if path == "/api/tasks")
    assert task_payload["payload"] == {
        "action": "task_timing_report",
        "task_id": None,
        "source": "telegram_read_request",
    }
    assert task_payload["max_attempts"] == 1


def test_system_self_check_is_safe_audited_and_truthful(client, monkeypatch):
    import json

    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "test-secret-must-not-leak")
    monkeypatch.setattr(settings, "owner_telegram_id", "123")
    monkeypatch.setattr(settings, "smtp_password", "smtp-secret-must-not-leak")
    monkeypatch.setattr(settings, "llm_api_key", "")
    message = "Запусти весь функционал чат бота"
    intent = understand_russian_message(message)
    analysis = client.post("/api/request-analysis", json={"message": message, "intent": intent}).json()
    assert analysis["classification"] == "supported"
    assert analysis["improvement_id"] is None
    approvals_before = len(client.get("/api/approvals").json())

    task = client.post("/api/tasks", json={
        "title": "Безопасная самопроверка функционала чат-бота",
        "agent_type": "orchestrator",
        "payload": {"action": "system_self_check"},
        "max_attempts": 1,
    }).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    result = completed["result"]

    assert completed["status"] == "done"
    assert result["outcome"] == "completed"
    assert result["check_kind"] == "system_functional_readiness"
    assert result["overall_status"] in {"ready", "partial"}
    assert result["summary"]["total"] == len(result["checks"])
    assert result["safety"] == {
        "protected_actions_executed": False,
        "external_messages_sent": False,
        "financial_commitments_created": False,
        "owner_approval_bypassed": False,
    }
    assert {row["name"] for row in result["checks"]} >= {
        "PostgreSQL", "API и Orchestrator", "Telegram-бот", "Sales/CRM",
        "Тендеры и Research", "Email и горячие лиды", "Публичный сайт и лид-форма",
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "test-secret-must-not-leak" not in serialized
    assert "smtp-secret-must-not-leak" not in serialized
    assert len(client.get("/api/approvals").json()) == approvals_before
    assert any(item["type"] == "database_probe" for item in result["evidence"])
    assert any(
        row["action"] == "task.completed" and row["resource_id"] == str(task["id"])
        for row in client.get("/api/audit").json()
    )


def test_telegram_system_self_check_returns_result_not_task_acceptance(monkeypatch):
    from app import bot

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/request-analysis":
            return {"classification": "supported", "improvement_id": None}
        if path == "/api/tasks":
            return {"id": 501, "agent_type": "orchestrator", "title": "Самопроверка"}
        if path == "/api/tasks/501/run":
            return {
                "status": "done",
                "result": {
                    "outcome": "completed",
                    "overall_status": "partial",
                    "summary": {"ready": 8, "total": 14},
                    "checks": [
                        {"name": "PostgreSQL", "status": "ready", "detail": "Контрольный запрос выполнен."},
                        {"name": "Email", "status": "credentials_required", "detail": "SMTP не настроен."},
                    ],
                    "credentials_required": ["SMTP_PASSWORD"],
                },
            }
        raise AssertionError(path)

    class Message:
        text = "Запусти весь функционал чат бота"

        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append(value)

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(bot, "allowed", lambda update: True)
    monkeypatch.setattr(bot, "api", fake_api)
    asyncio.run(bot.natural_language(Update(), None))
    reply = Update.effective_message.replies[-1]
    assert "частично готово" in reply
    assert "PostgreSQL" in reply
    assert "Платежи, договоры" in reply
    assert "Task accepted" not in reply
    task_payload = next(kwargs["json"] for method, path, kwargs in calls if path == "/api/tasks")
    assert task_payload["payload"]["action"] == "system_self_check"
    assert task_payload["max_attempts"] == 1


def test_telegram_system_self_check_preserves_failed_status(monkeypatch):
    from app import bot

    async def fake_api(method, path, **kwargs):
        if path == "/api/request-analysis":
            return {"classification": "supported", "improvement_id": None}
        if path == "/api/tasks":
            return {"id": 502}
        if path == "/api/tasks/502/run":
            return {"status": "failed", "result": {"error": "database unavailable"}}
        raise AssertionError(path)

    class Message:
        text = "Запусти весь функционал чат бота"

        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append(value)

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(bot, "allowed", lambda update: True)
    monkeypatch.setattr(bot, "api", fake_api)
    asyncio.run(bot.natural_language(Update(), None))
    assert "не завершилась" in Update.effective_message.replies[-1]
    assert "failed" in Update.effective_message.replies[-1]


def test_activity_report_is_a_real_audited_orchestrator_result(client, monkeypatch):
    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    message = "Пришли отчёт о проделанной работе"
    intent = understand_russian_message(message)
    analysis = client.post("/api/request-analysis", json={"message": message, "intent": intent}).json()
    assert analysis["classification"] == "supported"
    assert analysis["improvement_id"] is None
    active_statuses = {"open", "queued", "running"}
    active_before = sum(row["status"] in active_statuses for row in client.get("/api/tasks").json())

    task = client.post("/api/tasks", json={
        "title": "Сформировать отчёт о проделанной работе",
        "agent_type": "orchestrator",
        "payload": {"action": "system_activity_report", "period_hours": 24},
        "max_attempts": 1,
    }).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    result = completed["result"]
    assert completed["status"] == "done"
    assert result["outcome"] == "completed"
    assert result["report_kind"] == "system_activity"
    assert result["summary"]["tasks_active"] == active_before
    assert isinstance(result["recent_completed_tasks"], list)
    assert any(item["type"] == "database_snapshot" for item in result["evidence"])
    audit_rows = client.get("/api/audit").json()
    assert any(
        row["action"] == "task.completed" and row["resource_id"] == str(task["id"])
        for row in audit_rows
    )


def test_telegram_activity_report_returns_actual_result(monkeypatch):
    from app import bot

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/request-analysis":
            return {"classification": "supported", "improvement_id": None}
        if path == "/api/tasks":
            return {"id": 401, "agent_type": "orchestrator", "title": "Отчёт"}
        if path == "/api/tasks/401/run":
            return {
                "status": "done",
                "result": {
                    "outcome": "completed",
                    "period_hours": 24,
                    "summary": {
                        "tasks_completed": 3,
                        "tasks_active": 0,
                        "tasks_failed": 0,
                        "tasks_blocked": 0,
                        "queued_improvements": 1,
                        "pending_approvals": 0,
                    },
                    "recent_completed_tasks": [
                        {"id": 9, "agent_type": "sales", "title": "Подготовить КП"}
                    ],
                    "blockers": [],
                },
            }
        raise AssertionError(path)

    class Message:
        text = "Пришли отчет о проделанной работе"
        replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append(value)

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(bot, "allowed", lambda update: True)
    monkeypatch.setattr(bot, "api", fake_api)
    asyncio.run(bot.natural_language(Update(), None))
    assert "Выполнено задач: 3" in Update.effective_message.replies[-1]
    assert "#9 [sales] Подготовить КП" in Update.effective_message.replies[-1]
    task_payload = next(kwargs["json"] for method, path, kwargs in calls if path == "/api/tasks")
    assert task_payload["payload"]["action"] == "system_activity_report"
    assert task_payload["max_attempts"] == 1


def test_russian_chat_routes_business_requests_to_agents():
    from app.chat import understand_russian_message

    research = understand_russian_message("Найди тендеры по уборке бизнес-центров")
    assert research["kind"] == "task"
    assert research["agent_type"] == "research"
    assert research["payload"]["collection"] == "tenders"

    sales = understand_russian_message("Создай задачу связаться с новым клиентом")
    assert sales["agent_type"] == "sales"
    assert sales["payload"]["source"] == "telegram_natural_language"


def test_russian_chat_recognizes_proposal_client_without_command():
    from app.chat import understand_russian_message

    intent = understand_russian_message("Подготовь коммерческое предложение в PDF для тестового клиента Request Analyst")
    assert intent["kind"] == "task"
    assert intent["agent_type"] == "sales"
    assert intent["payload"]["action"] == "generate_proposal"
    assert intent["payload"]["client_query"] == "Request Analyst"


@pytest.mark.parametrize(
    ("message", "agent_type", "action_kind"),
    [
        ("Оплати счет поставщику", "finance", "financial"),
        ("Подпиши договор с новым клиентом", "sales", "contract"),
        ("Подай заявку на этот тендер", "tender", "tender_submission"),
        ("Найми уборщика на объект", "hr", "hr_final"),
        ("Разошли предложение всем клиентам", "sales", "bulk_outreach"),
    ],
)
def test_russian_chat_preserves_owner_approval_gates(message, agent_type, action_kind):
    from app.chat import understand_russian_message

    intent = understand_russian_message(message)
    assert intent["agent_type"] == agent_type
    assert intent["payload"]["action_kind"] == action_kind
    assert intent["protected"] is True


def test_russian_chat_protected_task_is_blocked_by_runtime(client):
    from app.chat import understand_russian_message

    intent = understand_russian_message("Оплати счет поставщику")
    task = client.post("/api/tasks", json={
        "title": intent["title"],
        "agent_type": intent["agent_type"],
        "priority": intent["priority"],
        "payload": intent["payload"],
    }).json()
    result = client.post(f"/api/tasks/{task['id']}/run").json()
    assert result["status"] == "blocked"
    assert result["result"]["reason"] == "owner_approval_required"


def test_telegram_application_registers_natural_language_handler(monkeypatch):
    from telegram.ext import MessageHandler
    from app.config import settings
    from app.bot import build_application

    monkeypatch.setattr(settings, "telegram_bot_token", "123456:fake-token-for-startup-check")
    monkeypatch.setattr(settings, "owner_telegram_id", "123")
    application = build_application()
    handlers = [handler for group in application.handlers.values() for handler in group]
    assert any(isinstance(handler, MessageHandler) for handler in handlers)


def test_compose_telegram_route_is_configurable_without_a_secret():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    route_line = next(line for line in compose.splitlines() if "api.telegram.org=" in line)
    assert "api.telegram.org=${TELEGRAM_API_IP:-" in route_line
    assert "TELEGRAM_BOT_TOKEN" not in route_line


def test_sales_agent_generates_downloadable_proposal_pdf(client, monkeypatch, tmp_path):
    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "company_name", "CleaningAIOS")
    monkeypatch.setattr(settings, "company_legal_name", "ИП Тестовый Владелец")
    monkeypatch.setattr(settings, "company_inn", "123456789012")
    lead = client.post("/api/records", json={
        "record_type": "lead",
        "title": "Request Analyst",
        "status": "qualified",
        "source": "test",
        "data": {"name": "Request Analyst", "company": "Request Analyst", "service": "business_center", "object_area": 2500, "budget": 180000},
    }).json()
    message = "Подготовь коммерческое предложение в PDF для тестового клиента Request Analyst"
    intent = understand_russian_message(message)
    assessment = client.post("/api/request-analysis", json={"message": message, "intent": intent}).json()
    assert assessment["classification"] == "supported"
    assert assessment["improvement_id"] is None
    task = client.post("/api/tasks", json={"title": message, "agent_type": "sales", "payload": intent["payload"], "max_attempts": 1}).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    assert completed["status"] == "done"
    result = completed["result"]
    assert result["status"] == "ready"
    assert result["client_record_id"] == lead["id"]
    assert result["owner_approval_required_before_sending"] is True
    assert result["sent_to_client"] is False
    downloaded = client.get(result["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.content.startswith(b"%PDF")
    assert len(downloaded.content) > 10_000
    proposals = client.get("/api/records?record_type=proposal").json()
    assert any(row["id"] == result["proposal_id"] and row["status"] == "ready" for row in proposals)
    audit = client.get("/api/audit").json()
    assert any(row["action"] == "proposal.downloaded" and row["resource_id"] == str(result["proposal_id"]) for row in audit)


def test_sales_agent_proposal_failure_is_recorded(client, monkeypatch, tmp_path):
    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    intent = understand_russian_message("Подготовь коммерческое предложение для клиента Отсутствует Уникально")
    task = client.post("/api/tasks", json={"title": "Missing CRM proposal", "agent_type": "sales", "payload": intent["payload"], "max_attempts": 1}).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    assert completed["status"] == "failed"
    assert "не найден в CRM" in completed["result"]["error"]
    audit = client.get("/api/audit").json()
    assert any(row["action"] == "task.failed" and row["resource_id"] == str(task["id"]) for row in audit)


def test_telegram_proposal_request_returns_real_pdf(monkeypatch):
    from app import bot

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/request-analysis":
            return {"classification": "supported", "improvement_id": None}
        if path == "/api/tasks":
            return {"id": 321, "agent_type": "sales", "title": "КП"}
        if path == "/api/tasks/321/run":
            return {"status": "done", "result": {"proposal_number": "KP-TEST", "download_url": "/api/proposals/77/download"}}
        raise AssertionError(path)

    async def fake_file(path):
        assert path == "/api/proposals/77/download"
        return b"%PDF-telegram-test", "proposal-test.pdf"

    class Message:
        text = "Подготовь коммерческое предложение для клиента Request Analyst"
        documents = []
        replies = []

        async def reply_document(self, **kwargs):
            self.documents.append(kwargs)

        async def reply_text(self, value, **kwargs):
            self.replies.append(value)

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(bot, "allowed", lambda update: True)
    monkeypatch.setattr(bot, "api", fake_api)
    monkeypatch.setattr(bot, "api_file", fake_file)
    asyncio.run(bot.natural_language(Update(), None))
    assert len(Update.effective_message.documents) == 1
    document = Update.effective_message.documents[0]
    assert document["filename"] == "proposal-test.pdf"
    assert document["document"].read().startswith(b"%PDF")
    assert "не отправлен клиенту" in document["caption"]
    task_payload = next(kwargs["json"] for method, path, kwargs in calls if path == "/api/tasks")
    assert task_payload["max_attempts"] == 1


def test_request_analyst_accepts_supported_request_without_improvement(client, monkeypatch):
    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    intent = understand_russian_message("Покажи текущие задачи")
    result = client.post("/api/request-analysis", json={"message": "Покажи текущие задачи", "intent": intent}).json()
    assert result["classification"] == "supported"
    assert result["fully_supported"] is True
    assert result["improvement_id"] is None


def test_request_analyst_creates_deduplicated_codex_prompt(client, monkeypatch):
    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "workspace_agent_trigger_id", "")
    monkeypatch.setattr(settings, "workspace_agent_access_token", "")
    message = "Позвони клиенту и договорись о встрече"
    intent = understand_russian_message(message)
    first = client.post("/api/request-analysis", json={"message": message, "intent": intent}).json()
    second = client.post("/api/request-analysis", json={"message": message, "intent": intent}).json()
    assert first["classification"] == "capability_gap"
    assert first["improvement_id"] == second["improvement_id"]
    assert first["handoff_status"] == "credentials_required"
    queued = client.get("/api/improvements?status=queued").json()
    row = next(x for x in queued if x["id"] == first["improvement_id"])
    assert row["occurrence_count"] == 2
    assert "телефонии" in row["suggested_function"]
    assert "Required test plan" in row["codex_prompt"]
    assert row["acceptance_criteria"]
    assert row["test_plan"]


def test_request_analyst_redacts_secrets_before_storage(client, monkeypatch):
    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    secret = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
    message = f"Добавь новую функцию, token={secret}"
    intent = understand_russian_message(message)
    result = client.post("/api/request-analysis", json={"message": message, "intent": intent}).json()
    row = next(x for x in client.get("/api/improvements").json() if x["id"] == result["improvement_id"])
    assert secret not in row["request_text"]
    assert secret not in row["codex_prompt"]
    assert "[REDACTED]" in row["request_text"]


def test_request_analyst_does_not_turn_approval_policy_into_feature_gap(client, monkeypatch):
    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    message = "Оплати счет поставщику"
    result = client.post("/api/request-analysis", json={"message": message, "intent": understand_russian_message(message)}).json()
    assert result["classification"] == "approval_required"
    assert result["improvement_id"] is None


def test_workspace_agent_handoff_uses_official_trigger_contract(client, monkeypatch):
    from app.chat import understand_russian_message
    from app.config import settings
    from app import improvements as improvement_module

    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "workspace_agent_trigger_id", "")
    monkeypatch.setattr(settings, "workspace_agent_access_token", "")
    message = "Позвони клиенту и согласуй время встречи"
    created = client.post("/api/request-analysis", json={"message": message, "intent": understand_russian_message(message)}).json()
    captured = {}

    class Response:
        def raise_for_status(self): return None
        def json(self): return {"conversation_url": "https://chatgpt.com/c/improvement-test", "agent_trigger_run_id": "apirun_test"}

    class Client:
        def __init__(self, *args, **kwargs): captured["client"] = kwargs
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, headers, json): captured.update({"url": url, "headers": headers, "payload": json}); return Response()

    monkeypatch.setattr(improvement_module.httpx, "Client", Client)
    monkeypatch.setattr(settings, "workspace_agent_trigger_id", "agtch_cleaning_test")
    monkeypatch.setattr(settings, "workspace_agent_access_token", "workspace-secret")
    handed_off = client.post(f"/api/improvements/{created['improvement_id']}/handoff").json()
    assert handed_off["handoff_status"] == "queued"
    assert handed_off["workspace_run_id"] == "apirun_test"
    assert captured["url"] == "https://api.chatgpt.com/v1/workspace_agents/agtch_cleaning_test/trigger"
    assert captured["headers"]["Authorization"] == "Bearer workspace-secret"
    assert captured["headers"]["Idempotency-Key"]
    assert captured["payload"]["input"].startswith("CleaningAI OS improvement request")


def test_codex_can_update_improvement_with_test_evidence(client, monkeypatch):
    from app.chat import understand_russian_message
    from app.config import settings

    monkeypatch.setattr(settings, "llm_api_key", "")
    message = "Добавь интеграцию календаря для встреч"
    created = client.post("/api/request-analysis", json={"message": message, "intent": understand_russian_message(message)}).json()
    updated = client.patch(f"/api/improvements/{created['improvement_id']}", json={
        "status": "implemented",
        "implementation_summary": "Добавлена календарная интеграция",
        "test_evidence": [{"command": "pytest -q", "result": "passed"}],
    }).json()
    assert updated["status"] == "implemented"
    assert updated["test_evidence"][0]["result"] == "passed"


def test_request_analyst_llm_uses_strict_structured_output(monkeypatch):
    import json
    from app import llm
    from app.config import settings

    captured = {}

    class Response:
        def raise_for_status(self): return None
        def json(self):
            analysis = {
                "capability_score": 0.3,
                "reason": "Нет исполняемой функции",
                "missing_capabilities": ["proposal_generator"],
                "suggested_function": "Добавить генератор КП",
                "acceptance_criteria": ["КП создаётся из CRM"],
                "test_plan": ["Проверить PDF"],
                "should_create_improvement": True,
            }
            return {"status": "completed", "model": "test-model", "output_text": json.dumps(analysis)}

    class Client:
        def __init__(self, *args, **kwargs): captured["headers"] = kwargs["headers"]
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, json): captured.update({"url": url, "payload": json}); return Response()

    monkeypatch.setattr(llm.httpx, "Client", Client)
    monkeypatch.setattr(settings, "llm_api_key", "test-secret")
    monkeypatch.setattr(settings, "llm_base_url", "https://api.example/v1")
    monkeypatch.setattr(settings, "llm_model", "test-model")
    result = llm.llm_advisor.analyze_request("Подготовь КП", {"kind": "task"}, {"should_create_improvement": True})
    assert result["status"] == "succeeded"
    assert result["should_create_improvement"] is True
    assert captured["payload"]["text"]["format"]["strict"] is True
    assert captured["payload"]["store"] is False


def test_orchestrator_revises_attached_proposal_with_copy_and_creative_agents(client, monkeypatch, tmp_path):
    from docx import Document
    from app.config import settings

    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "company_name", "CleaningAIOS")
    monkeypatch.setattr(settings, "company_legal_name", "ИП Тестовый Владелец")
    monkeypatch.setattr(settings, "company_inn", "123456789012")
    source = tmp_path / "КП ИП СОКОЛОВ А С ЖК РЕЧНОЙ.docx"
    document = Document()
    document.add_paragraph("Коммерческое предложение для ЖК Речной")
    document.add_paragraph("Площадь объекта: 4 500 м²")
    document.add_paragraph("Стоимость услуг: 320 000 руб. в месяц")
    document.add_paragraph("График: ежедневно, 2 смены")
    document.save(source)

    task = client.post("/api/tasks", json={
        "title": "Обновить приложенное КП",
        "agent_type": "orchestrator",
        "priority": "high",
        "payload": {
            "action": "revise_proposal",
            "source": "telegram_document",
            "source_path": str(source),
            "source_filename": source.name,
            "request_text": "Сделай КП красивее и профессиональнее, представь мне на утверждение",
            "original_message": "Сделай КП красивее и профессиональнее, представь мне на утверждение",
        },
        "max_attempts": 1,
    }).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    result = completed["result"]
    assert completed["status"] == "done"
    assert result["status"] == "ready_for_owner_review"
    assert result["owner_approval_required_before_sending"] is True
    assert result["sent_to_client"] is False
    assert {item["type"] for item in result["evidence"]} >= {
        "proposal_copy_review", "proposal_design_review", "document_export"
    }

    tasks = client.get("/api/tasks").json()
    copy_task = next(row for row in tasks if row["id"] == result["copy_task_id"])
    creative_task = next(row for row in tasks if row["id"] == result["creative_task_id"])
    assert copy_task["agent_type"] == "copywriter"
    assert copy_task["result"]["external_ai_used"] is False
    assert creative_task["agent_type"] == "creative"
    assert creative_task["result"]["preset"] == "narrative_proposal"

    docx_response = client.get(result["download_urls"]["docx"])
    pdf_response = client.get(result["download_urls"]["pdf"])
    assert docx_response.status_code == 200
    assert docx_response.content.startswith(b"PK")
    assert pdf_response.status_code == 200
    assert pdf_response.content.startswith(b"%PDF")

    approval = client.post(
        f"/api/approvals/{result['approval_id']}/approve",
        json={"note": "Утверждаю только проект, без отправки клиенту"},
    ).json()
    assert approval["status"] == "approved"
    revision = next(
        row for row in client.get("/api/records?record_type=proposal_revision").json()
        if row["id"] == result["proposal_revision_id"]
    )
    assert revision["status"] == "approved"
    assert revision["data"]["sent_to_client"] is False


def test_proposal_studio_separates_source_price_rows_and_total():
    from app.proposal_studio import _fact_lines

    text = """
    Уборка МОП (2 уборщица) 217 600
    Снабжение 14 900
    Придомовая территория (2 дворника) 232 545 Итоговая стоимость
    Менеджер клининга 35 400 500 445 руб.
    """
    assert _fact_lines(text) == [
        "Уборка МОП (2 уборщицы) — 217 600 руб.",
        "Снабжение — 14 900 руб.",
        "Придомовая территория (2 дворника) — 232 545 руб.",
        "Менеджер клининга — 35 400 руб.",
        "Итоговая стоимость — 500 445 руб.",
    ]


def test_proposal_studio_enforces_document_layout_preset(tmp_path):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt
    from pypdf import PdfReader

    from app.proposal_studio import _build_docx, _build_pdf

    copy = {
        "client_name": "ЖК Речной",
        "sections": {
            "opening": "Профессиональное предложение — на проверку владельцу.",
            "value": "Решение по задачам объекта.",
            "quality": "Контроль качества.",
            "launch": "Порядок запуска.",
        },
        "source_facts": ["Уборка МОП — 217 600 руб.", "Итоговая стоимость — 500 445 руб."],
        "source_contacts": {"phone": "8-995-599-60-95"},
    }
    docx_path = tmp_path / "proposal.docx"
    pdf_path = tmp_path / "proposal.pdf"
    _build_docx(docx_path, copy)
    _build_pdf(pdf_path, copy)

    document = Document(docx_path)
    section = document.sections[0]
    assert section.page_width == Inches(8.5)
    assert section.page_height == Inches(11)
    assert section.top_margin == section.right_margin == section.bottom_margin == section.left_margin == Inches(1)
    normal = document.styles["Normal"]
    assert normal.font.name == "Calibri"
    assert normal.font.size == Pt(11)
    assert normal.paragraph_format.space_after == Pt(8)
    assert normal.paragraph_format.line_spacing == pytest.approx(1.333, abs=0.001)
    assert normal.paragraph_format.alignment == WD_ALIGN_PARAGRAPH.JUSTIFY
    heading = document.styles["Heading 1"]
    assert heading.font.size == Pt(16)
    assert str(heading.font.color.rgb) == "2E74B5"
    assert heading.paragraph_format.space_before == Pt(18)
    assert heading.paragraph_format.space_after == Pt(10)

    for table in document.tables:
        assert table.style.name == "Table Grid"
        width = table._tbl.tblPr.first_child_found_in("w:tblW")
        assert width.get(qn("w:w")) == "9360"
        assert width.get(qn("w:type")) == "dxa"
        assert sum(int(node.get(qn("w:w"))) for node in table._tbl.tblGrid) == 9360

    assert len(PdfReader(str(pdf_path)).pages) == 2
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
    assert "—" not in pdf_text
    assert "ПРОЕКТ · ТРЕБУЕТ ПРОВЕРКИ И УТВЕРЖДЕНИЯ ВЛАДЕЛЬЦА" in pdf_text


def test_incomplete_telegram_task_creates_deduplicated_improvement_and_ceo_report(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "workspace_agent_trigger_id", "")
    monkeypatch.setattr(settings, "workspace_agent_access_token", "")
    task = client.post("/api/tasks", json={
        "title": "Обработать крупное приложенное КП",
        "agent_type": "orchestrator",
        "payload": {
            "action": "revise_proposal",
            "source": "telegram_document",
            "document_status": "credentials_required",
            "source_filename": "large-proposal.docx",
        },
        "max_attempts": 1,
    }).json()
    first = client.post(f"/api/tasks/{task['id']}/run").json()
    assert first["status"] == "blocked"
    assert first["result"]["improvement_id"]
    assert first["result"]["ceo_incident_task_id"]
    assert first["result"]["handoff_status"] == "credentials_required"
    assert first["result"]["responsible_party"] == "owner_configuration"

    incident = next(
        row for row in client.get("/api/tasks").json()
        if row["id"] == first["result"]["ceo_incident_task_id"]
    )
    assert incident["agent_type"] == "ceo"
    assert incident["result"]["report_kind"] == "agent_incident"
    assert incident["result"]["source_task_id"] == task["id"]
    runs = client.get("/api/agent-runs", headers={"X-Role": "manager"}).json()
    source_run = next(row for row in runs if row["task_id"] == task["id"])
    assert source_run["status"] == "incomplete"


def test_telegram_large_proposal_is_not_silently_ignored(monkeypatch):
    from app import bot
    from app.config import settings

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/request-analysis":
            return {"classification": "supported"}
        if path == "/api/tasks":
            return {"id": 701}
        if path == "/api/tasks/701/run":
            return {"id": 701, "status": "blocked", "result": {"improvement_id": 81, "ceo_incident_task_id": 702}}
        raise AssertionError(path)

    class Document:
        file_name = "КП ЖК Речной.docx"
        file_size = 29_800_000
        file_id = "telegram-file-id"

    class Message:
        document = Document()
        caption = "Сделай коммерческое предложение более красивым и профессиональным и представь на утверждение"

        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append(value)

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(settings, "telegram_bot_api_base_url", "")
    monkeypatch.setattr(bot, "allowed", lambda update: True)
    monkeypatch.setattr(bot, "api", fake_api)
    asyncio.run(bot.proposal_document(Update(), None))
    assert "зарегистрированы как задача #701" in Update.effective_message.replies[-1]
    assert "улучшение #81" in Update.effective_message.replies[-1].lower()
    task_payload = next(kwargs["json"] for method, path, kwargs in calls if path == "/api/tasks")
    assert task_payload["payload"]["document_status"] == "credentials_required"
    assert task_payload["payload"]["source"] == "telegram_document"


def test_telegram_small_proposal_returns_docx_and_pdf_for_owner_review(monkeypatch, tmp_path):
    from app import bot
    from app.config import settings

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/request-analysis":
            return {"classification": "supported"}
        if path == "/api/tasks":
            return {"id": 801}
        if path == "/api/tasks/801/run":
            return {
                "id": 801,
                "status": "done",
                "result": {
                    "status": "ready_for_owner_review",
                    "proposal_number": "KPR-TEST",
                    "approval_id": 91,
                    "download_urls": {"docx": "/revision.docx", "pdf": "/revision.pdf"},
                },
            }
        raise AssertionError(path)

    async def fake_file(path):
        return (b"PK-test", "revision.docx") if path.endswith("docx") else (b"%PDF-test", "revision.pdf")

    class TelegramFile:
        async def download_to_drive(self, custom_path):
            Path(custom_path).write_bytes(b"source-docx")

    class TelegramBot:
        async def get_file(self, file_id):
            assert file_id == "file-id"
            return TelegramFile()

    class Context:
        bot = TelegramBot()

    class Document:
        file_name = "КП ЖК Речной.docx"
        file_size = 100
        file_id = "file-id"

    class Message:
        document = Document()
        caption = "Сделай это КП профессиональнее и представь мне на утверждение"

        def __init__(self):
            self.replies = []
            self.documents = []

        async def reply_text(self, value, **kwargs):
            self.replies.append(value)

        async def reply_document(self, **kwargs):
            self.documents.append(kwargs)

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(settings, "document_storage_path", str(tmp_path))
    monkeypatch.setattr(settings, "telegram_bot_api_base_url", "")
    monkeypatch.setattr(bot, "allowed", lambda update: True)
    monkeypatch.setattr(bot, "api", fake_api)
    monkeypatch.setattr(bot, "api_file", fake_file)
    asyncio.run(bot.proposal_document(Update(), Context()))
    assert [item["filename"] for item in Update.effective_message.documents] == ["revision.docx", "revision.pdf"]
    assert "Клиенту ничего не отправлено" in Update.effective_message.documents[-1]["caption"]
    assert Update.effective_message.documents[-1]["reply_markup"] is not None


def test_public_website_lead_enters_crm_inbox_and_hot_queue(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hot_lead_score", 70)
    payload = {
        "name": "Анна Петрова",
        "company": "УК Публичный тест",
        "phone": "+7 999 123-45-67",
        "email": "public-hot@example.com",
        "service": "business_center",
        "object_area": 5000,
        "budget": 400000,
        "urgency": "today",
        "message": "Нужен быстрый запуск ежедневной уборки",
        "consent": True,
        "utm_source": "yandex",
        "utm_medium": "cpc",
        "utm_campaign": "public-hot-test",
    }
    response = client.post("/api/public/leads", json=payload)
    assert response.status_code == 201
    result = response.json()
    assert result["status"] == "qualified"
    assert result["owner_notification"] in {"queued", "credentials_required"}
    leads = client.get("/api/records?record_type=lead").json()
    lead = next(row for row in leads if row["id"] == result["lead_id"])
    assert lead["source"] == "yandex"
    assert lead["data"]["utm_campaign"] == "public-hot-test"
    inbox = client.get("/api/inbox?channel=web").json()
    assert any(row["record_id"] == result["lead_id"] for row in inbox)
    tasks = client.get("/api/tasks").json()
    assert any(row["agent_type"] == "sales" and row["payload"].get("record_id") == result["lead_id"] for row in tasks)


def test_public_lead_requires_contact_and_consent(client):
    base = {"name": "Тест", "service": "commercial", "urgency": "month", "consent": True}
    assert client.post("/api/public/leads", json=base).status_code == 422
    assert client.post("/api/public/leads", json={**base, "phone": "+7 999 000-00-00", "consent": False}).status_code == 422


def test_published_website_news_and_media_workflow(client):
    content = client.post("/api/marketing/content", json={"channel": "website", "title": "Запустили новый стандарт", "body": "Проверяем качество по единому регламенту.", "status": "draft"}).json()
    published = client.patch(f"/api/marketing/content/{content['id']}", json={"status": "published", "metrics": {"cover_url": "/static/cleaning-hero.png"}})
    assert published.status_code == 200
    asset = client.post("/api/marketing/media-assets", json={"kind": "image", "title": "Визуал новости", "prompt": "Минималистичный чистый интерьер"}).json()
    assert asset["provider"] == "codex_imagegen_workflow"
    completed = client.patch(f"/api/marketing/media-assets/{asset['id']}", headers={"X-Role": "manager"}, json={"status": "published", "public_url": "/static/cleaning-hero.png", "alt_text": "Чистый интерьер"})
    assert completed.status_code == 200
    site = client.get("/api/public/site").json()
    assert any(row["id"] == content["id"] for row in site["news"])
    assert any(row["id"] == asset["id"] for row in site["media"])


def test_marketing_experiment_attribution_and_manual_activation(client):
    experiment = client.post("/api/marketing/experiments", headers={"X-Role": "manager"}, json={
        "title": "Спрос на уборку БЦ",
        "channel": "yandex_direct",
        "hypothesis": "Уточнение SLA увеличит число квалифицированных лидов",
        "audience": "Управляющие бизнес-центров",
        "offer": "Аудит объекта до расчёта",
        "primary_metric": "qualified_leads",
        "budget_limit": 0,
        "utm_campaign": "experiment-attribution-test",
    }).json()
    waiting = client.post(f"/api/marketing/experiments/{experiment['id']}/launch", headers={"X-Role": "manager"}, json={}).json()
    assert waiting["status"] == "approved_waiting_manual_activation"
    started = client.post(f"/api/marketing/experiments/{experiment['id']}/launch", headers={"X-Role": "manager"}, json={"external_campaign_id": "manual-ya-123"}).json()
    assert started["status"] == "running"
    assert started["automatic_spend"] is False
    client.post("/api/public/leads", json={"name": "Иван Тест", "phone": "+7 999 111-22-33", "service": "business_center", "urgency": "week", "consent": True, "utm_campaign": "experiment-attribution-test"})
    analytics = client.get(f"/api/marketing/experiments/{experiment['id']}/analytics").json()
    assert analytics["leads"] == 1


def test_marketing_invoice_routes_to_owner_without_payment(client):
    provider = client.post("/api/marketing/providers", json={"name": "Рекламное агентство Тест", "platform": "agency", "contact": "manager@example.com"}).json()
    requisites = client.post("/api/company/requisites", json={
        "profile_name": "Основные тестовые",
        "legal_name": "ООО Тест Клининг",
        "inn": "7701234567",
        "kpp": "770101001",
        "settlement_account": "40702810000000000001",
        "currency": "RUR",
        "bank_name": "Тестовый банк",
        "bank_inn": "7701234567",
        "bank_address": "Москва, тестовый адрес банка",
        "bic": "044525001",
        "correspondent_account": "30101810000000000001",
    }).json()
    assert requisites["currency"] == "RUB"
    assert requisites["bank_inn"].endswith("4567")
    assert requisites["bank_address"] == "Москва, тестовый адрес банка"
    invoice = client.post("/api/marketing/invoices", headers={"X-Role": "manager"}, json={
        "provider_id": provider["id"],
        "requisites_profile_id": requisites["id"],
        "invoice_number": "MKT-001",
        "amount": 25000,
        "description": "Тест рекламной гипотезы",
    }).json()
    assert invoice["status"] == "pending_approval"
    assert invoice["automatic_payment"] is False
    assert invoice["telegram_notification"] in {"queued", "credentials_required"}
    decision = client.post(f"/api/approvals/{invoice['approval_id']}/approve", json={"note": "Проверено"}).json()
    assert decision["execution"] == "not_executed"
    saved = next(row for row in client.get("/api/marketing/invoices", headers={"X-Role": "manager"}).json() if row["id"] == invoice["id"])
    assert saved["status"] == "approved_for_manual_payment"
    assert saved["automatic_payment"] is False


def test_ai_provider_router_has_least_privilege_policy(client):
    response = client.get("/api/ai/providers", headers={"X-Role": "manager"})
    assert response.status_code == 200
    result = response.json()
    assert result["blanket_access"] is False
    assert result["policy"] == "least_privilege"
    assert all(row["forbidden"] for row in result["providers"])


def test_telegram_owner_notification_uses_existing_approval_buttons(monkeypatch):
    from app import notifications
    from app.config import settings
    from app.models import OwnerNotification

    captured = {}

    class Response:
        def raise_for_status(self): return None

    class Client:
        def __init__(self, *args, **kwargs): captured["client"] = kwargs
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, json): captured.update({"url": url, "payload": json}); return Response()

    monkeypatch.setattr(notifications.httpx, "Client", Client)
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token-value")
    monkeypatch.setattr(settings, "owner_telegram_id", "999")
    row = OwnerNotification(
        idempotency_key="telegram-contract-test",
        channel="telegram",
        recipient="999",
        subject="Счёт на рекламу",
        body="Одобрение не выполняет оплату",
        data={"approval_id": 42},
    )
    notifications._send_telegram(row)
    assert captured["url"].endswith("/sendMessage")
    assert captured["payload"]["chat_id"] == "999"
    buttons = captured["payload"]["reply_markup"]["inline_keyboard"][0]
    assert {item["callback_data"] for item in buttons} == {"approve:42", "reject:42"}
