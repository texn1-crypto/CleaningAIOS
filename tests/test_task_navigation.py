import asyncio
from datetime import datetime, timedelta, timezone


def _owner(monkeypatch):
    from app.config import settings

    user_id, chat_id = 81001, 82001
    monkeypatch.setattr(settings, "owner_telegram_id", str(user_id))
    monkeypatch.setattr(settings, "owner_telegram_chat_id", str(chat_id))
    monkeypatch.setattr(settings, "telegram_callback_secret", "navigation-test-secret")
    return user_id, chat_id


def _query(client, user_id, chat_id, view, page=1, page_size=10):
    response = client.post(
        "/api/telegram/control/tasks/query",
        json={
            "user_id": user_id,
            "chat_id": chat_id,
            "view": view,
            "page": page,
            "page_size": page_size,
        },
    )
    assert response.status_code == 200
    return response.json()


def test_task_views_are_paginated_assigned_due_and_workflow_correlated(client, monkeypatch):
    from app.db import SessionLocal
    from app.models import OwnerNotification

    user_id, chat_id = _owner(monkeypatch)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    created_ids = []
    for index in range(12):
        task = client.post(
            "/api/tasks",
            json={
                "title": f"Telegram assigned navigation {index}",
                "agent_type": "orchestrator",
                "assigned_to": "telegram-owner",
                "due_at": (now + timedelta(days=1)).isoformat(),
            },
        ).json()
        created_ids.append(task["id"])
        assert task["assigned_to"] == "telegram-owner"
        assert task["due_at"] is not None

    overdue = client.post(
        "/api/tasks",
        json={
            "title": "Telegram overdue navigation",
            "agent_type": "orchestrator",
            "assigned_to": "telegram-owner",
            "due_at": (now - timedelta(hours=1)).isoformat(),
        },
    ).json()
    critical = client.post(
        "/api/tasks",
        json={
            "title": "Telegram critical navigation",
            "agent_type": "orchestrator",
            "assigned_to": "telegram-owner",
            "priority": "critical",
            "payload": {"correlation_id": "corr-navigation-critical"},
        },
    ).json()
    assert client.post(f"/api/tasks/{critical['id']}/run").status_code == 200

    with SessionLocal() as db:
        alert = OwnerNotification(
            idempotency_key="navigation-critical-event",
            channel="telegram",
            subject="Navigation critical event",
            body="Test critical event",
            resource_type="task",
            resource_id=str(critical["id"]),
            severity="critical",
            correlation_id="corr-navigation-alert",
            status="sent",
            sent_at=now,
        )
        db.add(alert)
        db.commit()
        alert_id = alert.id

    first = _query(client, user_id, chat_id, "mine", page=1, page_size=10)
    second = _query(client, user_id, chat_id, "mine", page=2, page_size=10)
    assert first["page"] == 1
    assert first["has_next"] is True
    assert len(first["items"]) == 10
    assert second["has_previous"] is True
    returned_ids = {item["id"] for item in first["items"] + second["items"]}
    assert set(created_ids + [overdue["id"], critical["id"]]) <= returned_ids
    assert all(item["assigned_to"] == "telegram-owner" for item in first["items"])

    overdue_view = _query(client, user_id, chat_id, "overdue")
    overdue_item = next(item for item in overdue_view["items"] if item["id"] == overdue["id"])
    assert overdue_item["due_at"] is not None
    assert overdue_item["last_transition"]["reason"] == "task_created"

    critical_view = _query(client, user_id, chat_id, "critical")
    critical_item = next(item for item in critical_view["items"] if item["id"] == critical["id"])
    assert critical_item["correlation_id"] == "corr-navigation-critical"
    assert critical_item["last_transition"]["to_status"] == "done"

    event_view = _query(client, user_id, chat_id, "critical_events")
    event = next(item for item in event_view["items"] if item["id"] == alert_id)
    assert event["item_type"] == "critical_event"
    assert event["correlation_id"] == "corr-navigation-alert"
    assert event["source"] == {"resource_type": "task", "resource_id": str(critical["id"])}


def test_task_query_rejects_unbound_identity_and_invalid_navigation(client, monkeypatch):
    user_id, chat_id = _owner(monkeypatch)
    denied = client.post(
        "/api/telegram/control/tasks/query",
        json={
            "user_id": user_id + 1,
            "chat_id": chat_id + 1,
            "view": "all",
            "page": 1,
            "page_size": 10,
        },
    )
    assert denied.status_code == 403
    assert client.post(
        "/api/telegram/control/tasks/query",
        json={
            "user_id": user_id,
            "chat_id": chat_id,
            "view": "unknown",
            "page": 1,
            "page_size": 10,
        },
    ).status_code == 422
    assert client.post(
        "/api/telegram/control/tasks/query",
        json={
            "user_id": user_id,
            "chat_id": chat_id,
            "view": "all",
            "page": 0,
            "page_size": 10,
        },
    ).status_code == 422


def test_telegram_task_navigation_is_stable_and_pagination_is_server_backed(monkeypatch):
    from app import bot

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        assert path == "/api/telegram/control/tasks/query"
        page = kwargs["json"]["page"]
        return {
            "view": kwargs["json"]["view"],
            "page": page,
            "page_size": 10,
            "total": 11,
            "total_pages": 2,
            "has_previous": page > 1,
            "has_next": page < 2,
            "items": [
                {
                    "item_type": "task",
                    "id": 91,
                    "title": "Навигационный тест",
                    "status": "queued",
                    "priority": "high",
                    "agent_type": "orchestrator",
                    "assigned_to": "telegram-owner",
                    "due_at": "2026-08-14T12:00:00",
                    "correlation_id": "corr-nav",
                    "last_transition": {
                        "id": 1,
                        "from_status": "open",
                        "to_status": "queued",
                        "reason": "scheduled",
                    },
                }
            ],
        }

    async def fake_authorize(update, minimum_role):
        assert minimum_role == "viewer"
        return {"authorized": True, "role": "viewer", "subject": "telegram-owner"}

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

    class User:
        id = 81001

    class Chat:
        id = 82001

    class Update:
        def __init__(self, data):
            self.effective_message = Message()
            self.effective_user = User()
            self.effective_chat = Chat()
            self.callback_query = Query(data)

    class Context:
        user_data = {}

    monkeypatch.setattr(bot, "api", fake_api)
    monkeypatch.setattr(bot, "_authorize_update", fake_authorize)
    update = Update("tasks:mine:1")
    asyncio.run(bot.callback(update, Context()))
    text, kwargs = update.effective_message.replies[-1]
    assert "Мои задачи · стр. 1/2" in text
    assert "workflow corr corr-nav" in text
    callbacks = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "tasks:mine:2" in callbacks
    assert "tasks:critical_events:1" in callbacks
    assert "dashboard" in callbacks
    assert calls[-1][2]["json"] == {
        "user_id": 81001,
        "chat_id": 82001,
        "view": "mine",
        "page": 1,
        "page_size": 10,
    }

    invalid = Update("tasks:mine:0001")
    asyncio.run(bot.callback(invalid, Context()))
    assert "недействительна" in invalid.effective_message.replies[-1][0]


def test_telegram_created_task_is_assigned_to_authorized_identity(monkeypatch):
    from app import bot

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": 501}

    class Client:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, "kwargs": kwargs})
            return Response()

    monkeypatch.setattr(bot.httpx, "AsyncClient", Client)
    identity_token = bot._telegram_identity.set(
        {"authorized": True, "role": "operator", "subject": "telegram:pseudonym"}
    )
    try:
        result = asyncio.run(
            bot.api(
                "POST",
                "/api/tasks",
                json={"title": "Assigned through Telegram", "agent_type": "orchestrator"},
            )
        )
    finally:
        bot._telegram_identity.reset(identity_token)
    assert result == {"id": 501}
    assert captured["kwargs"]["json"]["assigned_to"] == "telegram:pseudonym"
