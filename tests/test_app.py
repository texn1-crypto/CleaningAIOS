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
    record = client.post("/api/records", json={"record_type": "lead", "title": "УК Север"})
    assert record.status_code == 201
    events = client.get("/api/events", headers={"X-Role": "manager"}).json()
    assert any(x["event_type"] == "lead.created" and x["aggregate_type"] == "lead" for x in events)


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


def test_mission_control_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Mission Control" in response.text


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


def test_russian_chat_routes_business_requests_to_agents():
    from app.chat import understand_russian_message

    research = understand_russian_message("Найди тендеры по уборке бизнес-центров")
    assert research["kind"] == "task"
    assert research["agent_type"] == "research"
    assert research["payload"]["collection"] == "tenders"

    sales = understand_russian_message("Создай задачу связаться с новым клиентом")
    assert sales["agent_type"] == "sales"
    assert sales["payload"]["source"] == "telegram_natural_language"


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
    message = "Подготовь коммерческое предложение клиенту"
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
