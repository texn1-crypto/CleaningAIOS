from __future__ import annotations

import time
import json

from sqlalchemy import select

from app.db import SessionLocal
from app.models import AgentRun, AgentToolCall, Task


def _create_and_run(client, *, tools: list[dict], max_attempts: int = 1):
    task = client.post(
        "/api/tasks",
        headers={"X-Role": "operator"},
        json={
            "title": "Read-only agent tool test",
            "agent_type": "orchestrator",
            "max_attempts": max_attempts,
            "payload": {
                "message": "aggregate diagnostic only",
                "read_only_tools": tools,
            },
        },
    ).json()
    return client.post(
        f"/api/tasks/{task['id']}/run", headers={"X-Role": "operator"}
    ).json()


def test_allowlisted_read_tool_executes_and_is_audited(client):
    task = _create_and_run(
        client,
        tools=[{"name": "workflow.status_counts", "arguments": {}}],
    )

    assert task["status"] == "done"
    tool_results = task["result"]["read_only_tool_results"]
    assert len(tool_results) == 1
    assert tool_results[0]["name"] == "workflow.status_counts"
    assert tool_results[0]["mode"] == "read_only"
    assert "tasks" in tool_results[0]["result"]
    assert any(
        item.get("type") == "agent_read_tool_call"
        for item in task["result"]["evidence"]
    )

    with SessionLocal() as db:
        run = db.scalar(
            select(AgentRun).where(AgentRun.task_id == task["id"])
        )
        call = db.scalar(
            select(AgentToolCall).where(AgentToolCall.agent_run_id == run.id)
        )
        assert call.status == "succeeded"
        assert call.mode == "read_only"
        assert call.tool_name == "workflow.status_counts"
        assert call.input_digest.startswith("sha256:")
        assert call.result_bytes > 0

    snapshot = client.get(
        "/api/observability/agents", headers={"X-Role": "manager"}
    ).json()
    assert snapshot["tools"]["by_status"]["succeeded"] >= 1
    metrics = client.get("/metrics", headers={"X-Role": "manager"}).text
    assert 'cleaningai_agent_tool_calls{tool_name="workflow.status_counts",status="succeeded"}' in metrics


def test_unknown_or_unallowlisted_tool_is_denied_before_agent_logic(client):
    task = _create_and_run(
        client,
        tools=[{"name": "shell.execute", "arguments": {"command": "ignored"}}],
    )
    assert task["status"] == "failed"
    assert "not allowed" in task["result"]["error"]

    with SessionLocal() as db:
        run = db.scalar(
            select(AgentRun).where(AgentRun.task_id == task["id"])
        )
        call = db.scalar(
            select(AgentToolCall).where(AgentToolCall.agent_run_id == run.id)
        )
        assert run.status == "failed"
        assert call.status == "denied"
        assert call.error_category == "policy_denied"


def test_tool_call_count_and_result_size_budgets_are_enforced(client, monkeypatch):
    from app.config import settings
    from app import agent_tools

    monkeypatch.setattr(settings, "agent_read_tool_max_calls_per_run", 1)
    over_count = _create_and_run(
        client,
        tools=[
            {"name": "workflow.status_counts", "arguments": {}},
            {"name": "workflow.status_counts", "arguments": {}},
        ],
    )
    assert over_count["status"] == "failed"
    assert "budget exceeded" in over_count["result"]["error"]

    def large_result(db, arguments):
        del db, arguments
        return {"aggregate": "x" * 300}

    monkeypatch.setattr(settings, "agent_read_tool_max_calls_per_run", 4)
    monkeypatch.setattr(settings, "agent_read_tool_max_result_bytes", 256)
    monkeypatch.setitem(
        agent_tools.READ_ONLY_TOOLS,
        "test.large",
        agent_tools.ReadOnlyTool(
            name="test.large",
            description="Test-only oversized aggregate.",
            allowed_agents=frozenset({"orchestrator"}),
            timeout_seconds=1,
            handler=large_result,
        ),
    )
    oversized = _create_and_run(
        client,
        tools=[{"name": "test.large", "arguments": {}}],
    )
    assert oversized["status"] == "failed"
    with SessionLocal() as db:
        call = db.scalar(
            select(AgentToolCall)
            .where(AgentToolCall.task_id == oversized["id"])
            .order_by(AgentToolCall.id.desc())
        )
        assert call.status == "denied"
        assert call.error_category == "result_size_exceeded"


def test_tool_timeout_discards_result_and_records_timeout(client, monkeypatch):
    from app import agent_tools

    def slow_result(db, arguments):
        del db, arguments
        time.sleep(0.06)
        return {"status": "too_late"}

    monkeypatch.setitem(
        agent_tools.READ_ONLY_TOOLS,
        "test.slow",
        agent_tools.ReadOnlyTool(
            name="test.slow",
            description="Test-only slow aggregate.",
            allowed_agents=frozenset({"orchestrator"}),
            timeout_seconds=0.05,
            handler=slow_result,
        ),
    )
    task = _create_and_run(
        client,
        tools=[{"name": "test.slow", "arguments": {}}],
    )
    assert task["status"] == "failed"
    assert "time budget exceeded" in task["result"]["error"]
    with SessionLocal() as db:
        call = db.scalar(
            select(AgentToolCall).where(AgentToolCall.task_id == task["id"])
        )
        assert call.status == "timed_out"
        assert call.error_category == "time_budget_exceeded"


def test_agent_tool_catalog_is_manager_only_and_contains_no_handlers(client):
    denied = client.get("/api/agent-tools", headers={"X-Role": "viewer"})
    assert denied.status_code == 403
    response = client.get("/api/agent-tools", headers={"X-Role": "manager"})
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "read_only"
    assert result["budgets"]["max_calls_per_run"] >= 0
    assert {item["name"] for item in result["tools"]} >= {
        "agent.slo_snapshot",
        "system.integration_readiness",
        "workflow.status_counts",
    }
    assert "handler" not in response.text


def test_allowlisted_stateless_mcp_read_tool_uses_current_protocol(client, monkeypatch):
    from app import mcp_read_client
    from app.config import settings

    monkeypatch.setattr(
        settings,
        "agent_mcp_read_servers_json",
        json.dumps(
            [
                {
                    "name": "public",
                    "url": "https://mcp.example/mcp",
                    "mode": "read_only",
                    "allowed_agents": ["orchestrator"],
                    "tools": ["search"],
                    "timeout_seconds": 1,
                }
            ]
        ),
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "jsonrpc": "2.0",
                "id": captured["payload"]["id"],
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": "public aggregate"}],
                },
            }

    class Client:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return Response()

    monkeypatch.setattr(mcp_read_client.httpx, "Client", Client)
    task = _create_and_run(
        client,
        tools=[{"name": "mcp.public.search", "arguments": {"q": "cleaning market"}}],
    )
    assert task["status"] == "done"
    remote = task["result"]["read_only_tool_results"][0]["result"]
    assert remote["untrusted_external_data"] is True
    assert remote["server"] == "public"
    assert captured["url"] == "https://mcp.example/mcp"
    assert captured["follow_redirects"] is False
    assert captured["headers"]["MCP-Protocol-Version"] == "2026-07-28"
    assert captured["headers"]["Mcp-Method"] == "tools/call"
    assert captured["headers"]["Mcp-Name"] == "search"
    assert captured["payload"]["method"] == "tools/call"


def test_remote_mcp_rejects_personal_data_and_unsafe_server_configuration(client, monkeypatch):
    from app.config import settings

    safe_config = [
        {
            "name": "public",
            "url": "https://mcp.example/mcp",
            "mode": "read_only",
            "allowed_agents": ["orchestrator"],
            "tools": ["search"],
        }
    ]
    monkeypatch.setattr(settings, "agent_mcp_read_servers_json", json.dumps(safe_config))
    denied = _create_and_run(
        client,
        tools=[
            {
                "name": "mcp.public.search",
                "arguments": {"q": "contact person@example.com"},
            }
        ],
    )
    assert denied["status"] == "failed"
    with SessionLocal() as db:
        call = db.scalar(
            select(AgentToolCall).where(AgentToolCall.task_id == denied["id"])
        )
        assert call.status == "denied"
        assert call.error_category == "input_denied"

    unsafe_config = [{**safe_config[0], "url": "http://127.0.0.1:9000/mcp"}]
    monkeypatch.setattr(settings, "agent_mcp_read_servers_json", json.dumps(unsafe_config))
    catalog = client.get(
        "/api/agent-tools", headers={"X-Role": "manager"}
    ).json()
    assert catalog["remote_mcp"]["status"] == "invalid_configuration"
    assert catalog["remote_mcp"]["configured_tools"] == 0
