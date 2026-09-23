import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app import agent_tools, bot
from app.chat import format_public_research, redact_sensitive_text, understand_russian_message
from app.db import SessionLocal
from app.models import AgentToolCall, ApprovalRequest, DomainEvent, Task


@pytest.mark.parametrize("message,agent", [
    ("Исследуй сайт https://example.com/", "research"),
    ("Изучи сайт https://example.com/contacts.", "research"),
    ("Проанализируй сайт клиента https://example.com/", "sales"),
    ("Проверь страницу финансов https://example.com/", "finance"),
])
def test_research_message_routes_to_real_tool(message, agent):
    intent = understand_russian_message(message)
    assert intent["agent_type"] == agent
    assert intent["payload"]["action"] == "public_web_research"
    assert intent["payload"]["automatic_outreach"] is False
    assert intent["protected"] is False
    assert intent["payload"]["research"]["max_pages"] == 3


@pytest.mark.parametrize("message", [
    "Исследуй сайт", "Изучи сайт https://a.example/ https://b.example/",
    "Изучи сайт http://example.com/", "Изучи сайт https://example.com:8443/",
    "Исследуй сайт https://user:secret@example.com/", "Исследуй сайт https://example.com/?token=secret",
    "Изучи сайт https://[broken/", "Изучи сайт https://example.com/#secret",
])
def test_ambiguous_or_sensitive_research_is_not_queued(message):
    intent = understand_russian_message(message)
    assert intent["kind"] == "clarification"
    assert "secret" not in json.dumps(intent)


@pytest.mark.parametrize("action,kind", [
    ("оплати счет", "financial"), ("подпиши договор", "contract"),
    ("подай заявку на тендер", "tender_submission"), ("разошли предложение всем клиентам", "bulk_outreach"),
])
def test_mixed_research_does_not_bypass_approval(action, kind):
    intent = understand_russian_message(f"Изучи сайт https://example.com/ и {action}")
    assert intent["protected"] is True
    assert intent["payload"]["action_kind"] == kind
    assert intent["payload"].get("action") != "public_web_research"


def test_url_credentials_are_redacted_before_analysis():
    assert "secret" not in redact_sensitive_text("Исследуй сайт https://user:secret@example.com/")
    assert "secret" not in redact_sensitive_text("Изучи сайт https://example.com/?session=secret")
    assert "secret" not in redact_sensitive_text("Изучи сайт https://example.com/#secret")


def _evidence():
    return {"success": True, "partial": True, "pages_succeeded": 1,
            "pages": [{"success": True, "resolved_url": "https://example.com/contacts",
                       "markdown": "Verified public company page", "content_sha256": "sha256:test"}]}


def _request(message="Исследуй сайт https://example.com/"):
    intent = understand_russian_message(message)
    return {k: intent[k] for k in ("title", "agent_type", "priority", "payload")}


def test_research_chat_persists_tool_evidence_and_reuses_completed_task(client, monkeypatch):
    calls = []
    def research(args):
        calls.append(args)
        return _evidence()
    monkeypatch.setattr(agent_tools, "research_public_site", research)
    message = "Исследуй сайт https://example.com/"
    intent = understand_russian_message(message)
    analysis = client.post("/api/request-analysis", json={"message": message, "intent": intent}).json()
    assert analysis["classification"] == "supported"
    assert analysis["improvement_id"] is None
    headers = {"Idempotency-Key": "research-chat-e2e"}
    task = client.post("/api/tasks", headers=headers, json=_request()).json()
    completed = client.post(f"/api/tasks/{task['id']}/run").json()
    assert completed["status"] == "done"
    replay = client.post("/api/tasks", headers=headers, json=_request()).json()
    assert replay["id"] == task["id"] and replay["status"] == "done"
    assert client.get(f"/api/tasks/{task['id']}").json() == replay
    report = format_public_research(completed)
    assert "частичный обзор" in report and "https://example.com/contacts" in report
    assert "Verified public company page" in report
    assert len(calls) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AgentToolCall).where(AgentToolCall.task_id == task["id"])) == 1
        assert db.scalar(select(func.count()).select_from(ApprovalRequest).where(ApprovalRequest.resource_id == str(task["id"]))) == 0


def test_task_idempotency_conflict_legacy_and_missing_read(client):
    headers = {"Idempotency-Key": "research-conflict"}
    first = client.post("/api/tasks", headers=headers, json=_request())
    assert first.status_code == 201
    changed = client.post("/api/tasks", headers=headers, json=_request("Изучи сайт https://other.example/"))
    assert changed.status_code == 409
    a = client.post("/api/tasks", json=_request()).json()
    b = client.post("/api/tasks", json=_request()).json()
    assert a["id"] != b["id"]
    assert client.get("/api/tasks/999999999").status_code == 404
    assert client.post("/api/tasks", headers={"Idempotency-Key": "x" * 201}, json=_request()).status_code == 422


def test_task_idempotency_is_actor_scoped(client):
    from app.schemas import TaskCreate
    from app.task_requests import create_requested_task
    with SessionLocal() as db:
        payload = TaskCreate(**_request())
        a = create_requested_task(db, payload, actor="one", idempotency_key="same")
        b = create_requested_task(db, payload, actor="two", idempotency_key="same")
        assert a.id != b.id
        assert create_requested_task(db, payload, actor="one", idempotency_key="same").id == a.id
        db.rollback()


@pytest.mark.parametrize("unique_collision", [False, True])
def test_concurrent_replay_rolls_back_extra_task(client, monkeypatch, unique_collision):
    from app.schemas import TaskCreate
    from app.task_requests import create_requested_task, event_bus
    with SessionLocal() as db:
        payload = TaskCreate(**_request())
        original = create_requested_task(db, payload, actor="race", idempotency_key="winner")
        receipt = db.scalar(select(DomainEvent).where(DomainEvent.aggregate_id == str(original.id), DomainEvent.event_type == "task.requested"))
        count = db.scalar(select(func.count()).select_from(Task))
        original_scalar = db.scalar
        missed = False
        def miss_first(statement, *args, **kwargs):
            nonlocal missed
            if not missed:
                missed = True
                return None
            return original_scalar(statement, *args, **kwargs)
        monkeypatch.setattr(db, "scalar", miss_first)
        def concurrent_publish(*args, **kwargs):
            if unique_collision:
                from uuid import uuid4
                db.add(DomainEvent(event_id=str(uuid4()), event_type="task.requested", aggregate_type="task",
                                   aggregate_id="new", correlation_id="race", actor="race", payload={},
                                   idempotency_key=receipt.idempotency_key))
                db.flush()
            return receipt
        monkeypatch.setattr(event_bus, "publish", concurrent_publish)
        replay = create_requested_task(db, payload, actor="race", idempotency_key="winner")
        assert replay.id == original.id
        assert db.scalar(select(func.count()).select_from(Task)) == count
        db.rollback()


def test_research_task_endpoints_preserve_rbac(client, monkeypatch):
    from app.config import settings
    task = client.post("/api/tasks", json=_request()).json()
    monkeypatch.setattr(settings, "environment", "production")
    assert client.get(f"/api/tasks/{task['id']}").status_code == 401
    monkeypatch.setattr(settings, "viewer_api_key", "test-research-viewer")
    headers = {"X-API-Key": "test-research-viewer", "Idempotency-Key": "viewer-write"}
    assert client.post("/api/tasks", headers=headers, json=_request()).status_code == 403
    assert client.get(f"/api/tasks/{task['id']}", headers=headers).status_code == 200


@pytest.mark.parametrize("outcome", ["success", "already_done", "worker_race", "timeout", "blocked"])
def test_telegram_research_delivers_evidence_and_never_blindly_retries(monkeypatch, outcome):
    calls = []
    replies = []
    completed = {"id": 42, "status": "done", "result": {"status": "ready",
                 "read_only_tool_results": [{"name": "web.public_research", "result": _evidence()}]}}
    async def api(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/api/request-analysis":
            return {"classification": "supported"}
        if path == "/api/tasks":
            assert kwargs["headers"]["Idempotency-Key"].startswith("telegram-research:")
            return completed if outcome == "already_done" else {"id": 42, "status": "open"}
        if path == "/api/tasks/42":
            return completed
        if path.endswith("/run"):
            assert kwargs["timeout"] == 60
            if outcome == "worker_race":
                response = httpx.Response(409, request=httpx.Request("POST", "https://example.com"))
                raise httpx.HTTPStatusError("race", request=response.request, response=response)
            if outcome == "timeout":
                raise httpx.ReadTimeout("timeout")
            if outcome == "blocked":
                return {"id": 42, "status": "blocked", "result": {"status": "unavailable"}}
            return completed
        raise AssertionError(path)
    async def reply(value, **kwargs):
        replies.append(value)
    update = SimpleNamespace(effective_message=SimpleNamespace(
        text="Исследуй сайт https://example.com/", message_id=2, reply_to_message=None, reply_text=reply),
        effective_user=SimpleNamespace(id=3), effective_chat=SimpleNamespace(id=4))
    monkeypatch.setattr(bot, "allowed", lambda _: True)
    monkeypatch.setattr(bot, "api", api)
    asyncio.run(bot.natural_language(update, SimpleNamespace(user_data={})))
    assert len([c for c in calls if c[1] == "/api/tasks"]) == 1
    assert len([c for c in calls if c[1].endswith("/run")]) == (0 if outcome == "already_done" else 1)
    if outcome == "timeout":
        assert "не подтверждение" in replies[-1]
    elif outcome == "blocked":
        assert "Готового результата пока нет" in replies[-1]
    else:
        assert "Verified public company page" in replies[-1]
        assert "частичный обзор" in replies[-1]


def test_report_never_claims_unverified_success():
    report = format_public_research({"id": 1, "status": "done", "result": {"status": "ready"}})
    assert "подтверждённых страниц" in report
