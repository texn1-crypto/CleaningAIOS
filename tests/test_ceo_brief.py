import asyncio
from datetime import datetime, timezone


def test_ceo_brief_separates_primary_facts_from_recommendations(client, monkeypatch):
    from app.agents import AGENTS
    from app.db import SessionLocal
    from app.models import BusinessRecord, OwnerNotification

    class FailingAgent:
        name = "ceo_brief_failure"

        def execute(self, db, payload):
            raise RuntimeError("controlled CEO brief failure")

    monkeypatch.setitem(AGENTS, "ceo_brief_failure", FailingAgent())
    failed_task = client.post(
        "/api/tasks",
        json={
            "title": "CEO brief failed task",
            "agent_type": "ceo_brief_failure",
            "max_attempts": 1,
        },
    ).json()
    assert client.post(f"/api/tasks/{failed_task['id']}/run").json()["status"] == "failed"
    protected_task = client.post(
        "/api/tasks",
        json={
            "title": "CEO brief blocked task",
            "agent_type": "tender",
            "payload": {"action_kind": "tender_submission"},
        },
    ).json()
    blocked = client.post(f"/api/tasks/{protected_task['id']}/run").json()
    approval_id = blocked["result"]["approval_id"]

    with SessionLocal() as db:
        payment = BusinessRecord(
            record_type="payment",
            title="CEO brief overdue payment",
            status="overdue",
            data={"amount": 12500},
        )
        alert = OwnerNotification(
            idempotency_key="ceo-brief-critical-alert",
            channel="telegram",
            subject="CEO brief critical alert",
            body="Test alert",
            severity="critical",
            correlation_id="corr-ceo-brief",
            status="sent",
            sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add_all([payment, alert])
        db.commit()
        payment_id = payment.id
        alert_id = alert.id

    response = client.get("/api/ceo/brief", headers={"X-Role": "manager"})
    assert response.status_code == 200
    brief = response.json()
    assert brief["freshness"]["source"] == "primary_database"
    assert brief["generated_at"] == brief["freshness"]["as_of"]
    assert brief["ai_generated_facts"] is False
    assert brief["automatic_critical_action"] is False
    assert failed_task["id"] in brief["facts"]["tasks"]["failed_ids"]
    assert protected_task["id"] in brief["facts"]["tasks"]["blocked_ids"]
    assert approval_id in brief["facts"]["approvals"]["ids"]
    assert alert_id in brief["facts"]["critical_alerts"]["ids"]
    assert payment_id in brief["facts"]["finance"]["payment_ids"]
    assert brief["facts"]["finance"]["overdue_amount"] >= 12500
    assert brief["recommendations"]
    assert all(item["source_ids"] for item in brief["recommendations"])
    assert {source["endpoint"] for source in brief["sources"]} >= {
        "/api/tasks",
        "/api/approvals",
        "/api/owner-notifications",
    }
    assert client.get("/api/ceo/brief", headers={"X-Role": "viewer"}).status_code == 403


def test_telegram_ceo_brief_is_read_only_and_task_button_uses_tasks_api(monkeypatch):
    from app import bot

    brief = {
        "generated_at": "2026-08-13T12:00:00",
        "facts": {
            "tasks": {
                "active": 2,
                "failed": 1,
                "blocked": 1,
                "failed_ids": [10],
                "blocked_ids": [11],
            },
            "approvals": {"pending": 1, "ids": [22]},
            "critical_alerts": {"unacknowledged": 1, "dead_letter": 0, "ids": [33]},
            "finance": {"overdue_payments": 1, "overdue_amount": 5000, "payment_ids": [44]},
        },
        "recommendations": [
            {
                "priority": "high",
                "text": "Разобрать задачи.",
                "source_ids": [10, 11],
            }
        ],
    }
    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/ceo/brief":
            return brief
        if path == "/api/tasks":
            return {"id": 701}
        raise AssertionError(path)

    async def fake_authorize(update, minimum_role):
        assert minimum_role == "manager"
        return {"authorized": True, "role": "manager"}

    class Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append((value, kwargs))

    class Query:
        def __init__(self, data):
            self.data = data

        async def answer(self):
            return None

    class Update:
        def __init__(self):
            self.effective_message = Message()
            self.callback_query = Query("ceo")

    class Context:
        user_data = {}

    monkeypatch.setattr(bot, "api", fake_api)
    monkeypatch.setattr(bot, "_authorize_update", fake_authorize)
    update = Update()
    asyncio.run(bot.callback(update, Context()))
    text, kwargs = update.effective_message.replies[-1]
    assert "ФАКТЫ ИЗ БД" in text
    assert "РЕКОМЕНДАЦИИ (НЕ ВЫПОЛНЕНЫ)" in text
    assert "source task IDs: [10, 11]" in text
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == [
        "ceo:refresh",
        "ceo:create_review_task",
    ]
    assert [path for _, path, _ in calls] == ["/api/ceo/brief"]

    update.callback_query = Query("ceo:create_review_task")
    asyncio.run(bot.callback(update, Context()))
    assert [path for _, path, _ in calls] == ["/api/ceo/brief", "/api/tasks"]
    task_payload = calls[-1][2]["json"]
    assert task_payload["agent_type"] == "ceo"
    assert task_payload["payload"]["automatic_critical_action"] is False
    assert "Критические действия не запускались" in update.effective_message.replies[-1][0]
