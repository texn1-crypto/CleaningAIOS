from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app import openjarvis_client
from app.config import settings


def test_openjarvis_rejects_non_local_service(monkeypatch):
    monkeypatch.setattr(settings, "openjarvis_enabled", True)
    monkeypatch.setattr(settings, "openjarvis_base_url", "https://example.com")
    assert openjarvis_client.configuration_status() == "invalid_configuration"


def test_container_service_requires_credential(monkeypatch):
    monkeypatch.setattr(settings, "openjarvis_enabled", True)
    monkeypatch.setattr(settings, "openjarvis_base_url", "http://openjarvis:8011")
    monkeypatch.setattr(settings, "openjarvis_api_key", "")
    assert openjarvis_client.configuration_status() == "credentials_required"


def test_openjarvis_redacts_secret_authenticates_and_bounds_answer(monkeypatch):
    monkeypatch.setattr(settings, "openjarvis_enabled", True)
    monkeypatch.setattr(settings, "openjarvis_base_url", "http://openjarvis:8011")
    monkeypatch.setattr(settings, "openjarvis_api_key", "internal-credential")
    monkeypatch.setattr(settings, "openjarvis_max_response_chars", 500)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Р" * 600}}]},
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(openjarvis_client.httpx, "AsyncClient", client_factory)
    answer = asyncio.run(
        openjarvis_client.ask_openjarvis(
            "api key=very-secret-value покажи статус"
        )
    )
    assert "very-secret-value" not in captured["body"]
    assert "[REDACTED]" in captured["body"]
    assert captured["authorization"] == "Bearer internal-credential"
    assert answer == "Р" * 500


def test_openjarvis_rejects_oversized_response(monkeypatch):
    monkeypatch.setattr(settings, "openjarvis_enabled", True)
    monkeypatch.setattr(settings, "openjarvis_base_url", "http://127.0.0.1:8011")
    monkeypatch.setattr(settings, "openjarvis_api_key", "")
    monkeypatch.setattr(settings, "openjarvis_max_response_bytes", 1_024)

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 1_025)
    )
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(openjarvis_client.httpx, "AsyncClient", client_factory)
    with pytest.raises(openjarvis_client.OpenJarvisUnavailable):
        asyncio.run(openjarvis_client.ask_openjarvis("status"))


def test_telegram_jarvis_runs_request_analysis_before_adviser(monkeypatch):
    from app import bot

    calls = []

    async def fake_api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"classification": "supported"}

    async def fake_ask(message):
        assert calls and calls[-1][1] == "/api/request-analysis"
        return "Защищённый ответ"

    class Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append((value, kwargs))

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(bot, "api", fake_api)
    monkeypatch.setattr(bot, "ask_openjarvis", fake_ask)
    asyncio.run(
        bot.jarvis_command(
            Update(), SimpleNamespace(args=["Как", "улучшить", "план?"])
        )
    )
    payload = calls[0][2]["json"]
    assert payload["intent"]["payload"]["advisory_only"] is True
    assert payload["intent"]["payload"]["external_action"] is False
    assert payload["source_user"] == "telegram-owner"
    assert "123" not in payload["source_user"]
    assert Update.effective_message.replies[-1][0] == (
        "🤖 Jarvis (совет):\nЗащищённый ответ"
    )


def test_telegram_jarvis_reads_one_public_url_through_crawl4ai(monkeypatch):
    from app import bot

    async def fake_api(method, path, **kwargs):
        return {"classification": "supported"}

    monkeypatch.setattr(
        bot,
        "crawl_public_page",
        lambda arguments: {
            "success": True,
            "resolved_url": arguments["url"],
            "markdown": "verified public page",
        },
    )

    async def fake_ask(message, *, web_context=""):
        assert "https://example.com/report" in message
        assert web_context == "verified public page"
        return "Ответ по источнику"

    class Message:
        def __init__(self):
            self.replies = []

        async def reply_text(self, value, **kwargs):
            self.replies.append((value, kwargs))

    class User:
        id = 123

    class Update:
        effective_message = Message()
        effective_user = User()

    monkeypatch.setattr(bot, "api", fake_api)
    monkeypatch.setattr(bot, "ask_openjarvis", fake_ask)
    asyncio.run(
        bot.jarvis_command(
            Update(),
            SimpleNamespace(args=["Проверь", "https://example.com/report"]),
        )
    )

    reply = Update.effective_message.replies[-1][0]
    assert "Ответ по источнику" in reply
    assert "🌐 Источник: https://example.com/report" in reply
