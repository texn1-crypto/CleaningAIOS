import asyncio

import pytest
from sqlalchemy import func, select
from telegram.ext import ApplicationHandlerStop, CommandHandler


def _payload(**overrides):
    data = {
        "conversation_id": "lead_autopilot_test_001",
        "requester_key": "a" * 64,
        "name": "Иван Петров",
        "company": "УК Север",
        "phone": "+7 999 111-22-33",
        "email": None,
        "telegram_username": "ivan_clean_test",
        "service": "business_center",
        "object_area": 3200,
        "location": "Санкт-Петербург, Петроградский район",
        "frequency": "daily",
        "urgency": "week",
        "message": "Нужна утренняя смена и контроль входной группы.",
        "consent": True,
    }
    data.update(overrides)
    return data


def test_lead_autopilot_creates_crm_task_notifications_and_is_idempotent(client, monkeypatch):
    from app.config import settings
    from app.db import SessionLocal
    from app.models import AuditLog, BusinessRecord, ContactEvent, InboxMessage, OwnerNotification, Task

    monkeypatch.setattr(settings, "lead_estimate_min_rub_per_sqm", 0)
    monkeypatch.setattr(settings, "lead_estimate_max_rub_per_sqm", 0)
    monkeypatch.setattr(settings, "owner_telegram_id", "9100001")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    response = client.post("/api/leads/autopilot", headers={"X-Role": "operator"}, json=_payload())
    assert response.status_code == 201
    result = response.json()
    assert result["accepted"] is True
    assert result["hot"] is True
    assert result["estimate"] == {
        "status": "pricing_configuration_required",
        "reason": "owner_approved_price_book_missing",
        "site_survey_required": True,
        "is_offer": False,
    }
    assert result["owner_notifications"]["telegram"] == "queued"

    with SessionLocal() as db:
        lead = db.get(BusinessRecord, result["lead_id"])
        assert lead.record_type == "lead"
        assert lead.status == "qualified"
        assert lead.source == "telegram_lead_autopilot"
        assert lead.data["consent_version"] == "telegram-lead-v1"
        assert lead.data["location"] == "Санкт-Петербург, Петроградский район"
        assert db.scalar(select(func.count()).select_from(ContactEvent).where(ContactEvent.record_id == lead.id)) == 1
        assert db.scalar(select(func.count()).select_from(InboxMessage).where(InboxMessage.record_id == lead.id)) == 1
        task = db.get(Task, result["task_id"])
        assert task.agent_type == "sales"
        assert task.priority == "high"
        assert task.payload["next_action"] == "contact_and_schedule_site_survey"
        assert db.scalar(select(func.count()).select_from(OwnerNotification).where(OwnerNotification.resource_type == "lead", OwnerNotification.resource_id == str(lead.id))) >= 1
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "lead_autopilot.accepted", AuditLog.resource_id == str(lead.id)).order_by(AuditLog.id.desc()))
        assert audit is not None
        assert "phone" not in audit.details and "email" not in audit.details

    replay = client.post("/api/leads/autopilot", headers={"X-Role": "operator"}, json=_payload())
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["lead_id"] == result["lead_id"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ContactEvent).where(ContactEvent.record_id == result["lead_id"])) == 1
        assert db.scalar(select(func.count()).select_from(InboxMessage).where(InboxMessage.record_id == result["lead_id"])) == 1
        assert db.scalar(select(func.count()).select_from(Task).where(Task.id == result["task_id"])) == 1


def test_lead_autopilot_creates_a_followup_task_for_a_new_contact(client, monkeypatch):
    from app.config import settings
    from app.db import SessionLocal
    from app.models import ContactEvent, Task

    monkeypatch.setattr(settings, "owner_telegram_id", "9100001")
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    first = client.post(
        "/api/leads/autopilot",
        headers={"X-Role": "operator"},
        json=_payload(conversation_id="lead_followup_001", phone="+7 999 555-11-22"),
    )
    second = client.post(
        "/api/leads/autopilot",
        headers={"X-Role": "operator"},
        json=_payload(conversation_id="lead_followup_002", phone="+7 999 555-11-22", message="Повторное обращение"),
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["lead_id"] == second.json()["lead_id"]
    assert first.json()["task_id"] != second.json()["task_id"]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ContactEvent).where(ContactEvent.record_id == first.json()["lead_id"])) == 2
        assert db.scalar(select(func.count()).select_from(Task).where(Task.id.in_([first.json()["task_id"], second.json()["task_id"]]))) == 2


def test_lead_autopilot_refuses_pii_when_legal_profile_is_missing(client, monkeypatch):
    from app.config import settings
    from app.readiness import integration_status

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "company_legal_name", "")
    monkeypatch.setattr(settings, "company_email", "")
    monkeypatch.setattr(settings, "privacy_contact_email", "")
    response = client.post(
        "/api/leads/autopilot",
        headers={"X-API-Key": settings.api_key},
        json=_payload(conversation_id="lead_legal_block_001"),
    )
    assert response.status_code == 503
    assert "COMPANY_LEGAL_NAME" in response.json()["detail"]
    assert integration_status()["lead_autopilot"]["status"] == "legal_profile_required"


def test_lead_autopilot_rejects_missing_consent_or_contact(client):
    rejected = client.post(
        "/api/leads/autopilot",
        headers={"X-Role": "operator"},
        json=_payload(conversation_id="lead_no_consent_001", consent=False),
    )
    assert rejected.status_code == 422
    assert "Consent" in rejected.json()["detail"]
    missing_contact = client.post(
        "/api/leads/autopilot",
        headers={"X-Role": "operator"},
        json=_payload(conversation_id="lead_no_contact_001", phone="", email=None),
    )
    assert missing_contact.status_code == 422
    assert "email or Russian phone" in missing_contact.json()["detail"]


def test_lead_autopilot_rate_limits_pseudonymous_requester(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "public_lead_rate_limit_per_hour", 1)
    requester_key = "b" * 64
    first = client.post(
        "/api/leads/autopilot",
        headers={"X-Role": "operator"},
        json=_payload(
            conversation_id="lead_rate_limit_001",
            requester_key=requester_key,
            phone="+7 999 101-20-30",
        ),
    )
    assert first.status_code == 201
    blocked = client.post(
        "/api/leads/autopilot",
        headers={"X-Role": "operator"},
        json=_payload(
            conversation_id="lead_rate_limit_002",
            requester_key=requester_key,
            phone="+7 999 101-20-31",
        ),
    )
    assert blocked.status_code == 429
    assert "Too many requests" in blocked.json()["detail"]


def test_owner_configured_price_book_produces_non_offer_range(monkeypatch):
    from app.config import settings
    from app.lead_autopilot import preliminary_estimate
    from app.schemas import LeadAutopilotCreate

    monkeypatch.setattr(settings, "lead_estimate_min_rub_per_sqm", 10)
    monkeypatch.setattr(settings, "lead_estimate_max_rub_per_sqm", 15)
    monkeypatch.setattr(settings, "lead_estimate_min_order_rub", 5000)
    payload = LeadAutopilotCreate(**_payload(
        conversation_id="lead_price_test_001",
        object_area=100,
        frequency="weekly",
    ))
    estimate = preliminary_estimate(payload)
    assert estimate["status"] == "preliminary"
    assert estimate["min_rub"] == 5000
    assert estimate["max_rub"] == 6000
    assert estimate["period"] == "month"
    assert estimate["is_offer"] is False
    assert estimate["site_survey_required"] is True


def test_public_telegram_lead_wizard_submits_only_consented_business_fields(monkeypatch):
    from app import bot

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        assert path == "/api/leads/autopilot"
        return {
            "accepted": True,
            "lead_id": 812,
            "task_id": 913,
            "status": "qualified",
            "score": 83,
            "hot": True,
            "estimate": {"status": "pricing_configuration_required"},
            "owner_notifications": {"telegram": "queued", "email": "queued"},
            "idempotent_replay": False,
        }

    class Message:
        text = ""

        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append((value, kwargs))

    class User:
        id = 777001
        username = "client_test"

    class Chat:
        id = 777002

    class Query:
        data = ""

        async def answer(self):
            return None

    class Update:
        effective_message = Message()
        effective_user = User()
        effective_chat = Chat()
        callback_query = Query()

    class Context:
        user_data = {}

    update = Update()
    context = Context()
    monkeypatch.setattr(bot, "api", fake_api)
    asyncio.run(bot.lead_start(update, context))
    draft = context.user_data["lead_draft"]

    def click(action, value):
        update.callback_query.data = bot._lead_callback(draft, action, value)
        with pytest.raises(ApplicationHandlerStop):
            asyncio.run(bot.lead_callback(update, context))

    click("consent", "yes")
    click("service", "business_center")
    update.effective_message.text = "1500"
    assert asyncio.run(bot.lead_input(update, context)) is True
    update.effective_message.text = "Санкт-Петербург, Московский район"
    assert asyncio.run(bot.lead_input(update, context)) is True
    click("frequency", "weekdays")
    click("urgency", "week")
    update.effective_message.text = "Анна"
    assert asyncio.run(bot.lead_input(update, context)) is True
    update.effective_message.text = "ООО Чистый объект"
    assert asyncio.run(bot.lead_input(update, context)) is True
    update.effective_message.text = "+7 999 222-33-44"
    assert asyncio.run(bot.lead_input(update, context)) is True
    update.effective_message.text = "Нужен осмотр после 18:00"
    assert asyncio.run(bot.lead_input(update, context)) is True

    assert context.user_data.get("lead_draft") is None
    assert len(calls) == 1
    sent = calls[0][2]["json"]
    assert sent["consent"] is True
    assert sent["service"] == "business_center"
    assert sent["frequency"] == "weekdays"
    assert sent["telegram_username"] == "client_test"
    assert "user_id" not in sent and "chat_id" not in sent
    assert "request-analysis" not in calls[0][1]
    assert "Заявка #812 принята" in update.effective_message.replies[-1][0]


def test_telegram_application_registers_public_estimate_command(monkeypatch):
    from app.bot import build_application
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", "123456:lead-autopilot-test")
    monkeypatch.setattr(settings, "owner_telegram_id", "123")
    application = build_application()
    handlers = [handler for group in application.handlers.values() for handler in group]
    commands = {
        command
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert {"start", "estimate", "cancel"}.issubset(commands)
