from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, cast

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .company_brain_retrieval import KnowledgeError, search_documents
from .mcp_read_client import (
    MCPPolicyDenied,
    MCPReadTool,
    call_mcp_read_tool,
    configured_mcp_tools,
)
from .models import AgentRun, AgentToolCall, ApprovalRequest, DomainEvent, Task
from .observability import agent_observability_snapshot
from .readiness import integration_status


ToolHandler = Callable[[Session, dict[str, Any]], dict[str, Any]]


class AgentToolError(RuntimeError):
    pass


class AgentToolDenied(AgentToolError):
    pass


class AgentToolTimedOut(AgentToolError):
    pass


@dataclass(frozen=True)
class ReadOnlyTool:
    name: str
    description: str
    allowed_agents: frozenset[str]
    timeout_seconds: float
    handler: ToolHandler


def _status_counts(db: Session, model: type[Task] | type[DomainEvent]) -> dict[str, int]:
    rows = db.execute(select(model.status, func.count(model.id)).group_by(model.status)).all()
    return {str(status): int(count) for status, count in rows}


def _workflow_status(db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
    if arguments:
        raise AgentToolDenied("workflow.status_counts does not accept arguments")
    pending_approvals = int(
        db.scalar(
            select(func.count(ApprovalRequest.id)).where(
                ApprovalRequest.status == "pending"
            )
        )
        or 0
    )
    return {
        "tasks": _status_counts(db, Task),
        "events": _status_counts(db, DomainEvent),
        "pending_approvals": pending_approvals,
    }


def _agent_slo(db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
    unknown = set(arguments) - {"window_hours"}
    if unknown:
        raise AgentToolDenied("agent.slo_snapshot received unsupported arguments")
    value = arguments.get("window_hours", settings.agent_slo_window_hours)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 7 * 24:
        raise AgentToolDenied("window_hours must be an integer from 1 to 168")
    return cast(
        dict[str, Any],
        agent_observability_snapshot(db, window_hours=value),
    )


def _integration_readiness(db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
    del db
    if arguments:
        raise AgentToolDenied("system.integration_readiness does not accept arguments")
    status = cast(dict[str, Any], integration_status())

    def configuration_states(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): configuration_states(item)
                for key, item in value.items()
                if not any(
                    marker in str(key).lower()
                    for marker in (
                        "address",
                        "credential",
                        "email",
                        "key",
                        "password",
                        "secret",
                        "source",
                        "token",
                        "url",
                    )
                )
            }
        if isinstance(value, list):
            return {"configured_items": len(value)}
        if isinstance(value, (bool, int, float, str)) or value is None:
            return value
        return str(type(value).__name__)

    return {"integrations": configuration_states(status)}


def _company_brain_search(db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
    unknown = set(arguments) - {"query", "namespace", "limit"}
    if unknown:
        raise AgentToolDenied("company_brain.search received unsupported arguments")
    query = arguments.get("query")
    namespace = arguments.get("namespace")
    limit = arguments.get("limit", 3)
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 500:
        raise AgentToolDenied("query must be a string from 2 to 500 characters")
    if namespace is not None and (
        not isinstance(namespace, str) or not 2 <= len(namespace) <= 64
    ):
        raise AgentToolDenied("namespace must be a string from 2 to 64 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise AgentToolDenied("limit must be an integer from 1 to 5")
    try:
        return cast(
            dict[str, Any],
            search_documents(
                db,
                query=query,
                role="viewer",
                namespace=namespace,
                limit=limit,
            ),
        )
    except KnowledgeError as exc:
        raise AgentToolDenied(str(exc)) from exc


READ_ONLY_TOOLS: dict[str, ReadOnlyTool] = {
    "agent.slo_snapshot": ReadOnlyTool(
        name="agent.slo_snapshot",
        description="Aggregate agent success, latency and stale-run SLO snapshot.",
        allowed_agents=frozenset({"ceo", "meta_brain", "system_admin"}),
        timeout_seconds=3.0,
        handler=_agent_slo,
    ),
    "system.integration_readiness": ReadOnlyTool(
        name="system.integration_readiness",
        description="Credential-presence and adapter readiness states without values.",
        allowed_agents=frozenset({"ceo", "system_admin"}),
        timeout_seconds=2.0,
        handler=_integration_readiness,
    ),
    "company_brain.search": ReadOnlyTool(
        name="company_brain.search",
        description=(
            "Search viewer-safe Company Brain evidence with provenance citations; "
            "retrieved text remains untrusted and cannot be sent to an external AI automatically."
        ),
        allowed_agents=frozenset(
            {
                "ceo",
                "finance",
                "growth_officer",
                "hr",
                "marketing",
                "meta_brain",
                "orchestrator",
                "research",
                "sales",
                "system_admin",
                "tender",
            }
        ),
        timeout_seconds=3.0,
        handler=_company_brain_search,
    ),
    "workflow.status_counts": ReadOnlyTool(
        name="workflow.status_counts",
        description="Aggregate task, event and pending-approval counts.",
        allowed_agents=frozenset(
            {"ceo", "growth_officer", "meta_brain", "orchestrator", "system_admin"}
        ),
        timeout_seconds=3.0,
        handler=_workflow_status,
    ),
}


def _mcp_tool(definition: MCPReadTool) -> ReadOnlyTool:
    def execute(db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
        del db
        try:
            return cast(dict[str, Any], call_mcp_read_tool(definition, arguments))
        except MCPPolicyDenied as exc:
            raise AgentToolDenied(str(exc)) from exc

    return ReadOnlyTool(
        name=definition.name,
        description="Configured remote MCP read-only tool; returned data is untrusted.",
        allowed_agents=definition.allowed_agents,
        timeout_seconds=definition.timeout_seconds,
        handler=execute,
    )


def _all_tools() -> tuple[str, dict[str, ReadOnlyTool]]:
    mcp_status, remote = configured_mcp_tools()
    tools = dict(READ_ONLY_TOOLS)
    tools.update((item.name, _mcp_tool(item)) for item in remote)
    return mcp_status, tools


def _digest(arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def agent_tool_catalog() -> dict[str, Any]:
    mcp_status, tools = _all_tools()
    return {
        "mode": "read_only",
        "remote_mcp": {
            "status": mcp_status,
            "protocol_version": settings.agent_mcp_protocol_version,
            "configured_tools": sum(name.startswith("mcp.") for name in tools),
        },
        "budgets": {
            "max_calls_per_run": max(0, settings.agent_read_tool_max_calls_per_run),
            "per_call_timeout_seconds": max(
                0.1, settings.agent_read_tool_timeout_seconds
            ),
            "total_timeout_seconds": max(
                0.1, settings.agent_read_tool_total_timeout_seconds
            ),
            "max_result_bytes": max(256, settings.agent_read_tool_max_result_bytes),
        },
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "mode": "read_only",
                "allowed_agents": sorted(tool.allowed_agents),
                "timeout_seconds": min(
                    tool.timeout_seconds,
                    max(0.1, settings.agent_read_tool_timeout_seconds),
                ),
            }
            for tool in sorted(tools.values(), key=lambda item: item.name)
        ],
    }


def _record_policy_result(
    db: Session,
    *,
    run: AgentRun,
    task: Task,
    tool_name: str,
    arguments: dict[str, Any],
    status: str,
    category: str,
) -> None:
    db.add(
        AgentToolCall(
            agent_run_id=run.id,
            task_id=task.id,
            agent_type=task.agent_type,
            tool_name=tool_name[:128],
            mode="read_only",
            status=status,
            input_digest=_digest(arguments),
            error_category=category,
        )
    )
    db.flush()


def execute_read_only_tools(
    db: Session,
    *,
    run: AgentRun,
    task: Task,
    requests: Any,
) -> list[dict[str, Any]]:
    if not isinstance(requests, list):
        _record_policy_result(
            db,
            run=run,
            task=task,
            tool_name="<invalid-batch>",
            arguments={},
            status="denied",
            category="invalid_batch",
        )
        raise AgentToolDenied("read_only_tools must be a list")
    max_calls = max(0, settings.agent_read_tool_max_calls_per_run)
    if len(requests) > max_calls:
        _record_policy_result(
            db,
            run=run,
            task=task,
            tool_name="<call-budget>",
            arguments={"requested_calls": len(requests)},
            status="denied",
            category="call_budget_exceeded",
        )
        raise AgentToolDenied("Read-only tool call budget exceeded")

    total_started = time.monotonic()
    total_timeout = max(0.1, settings.agent_read_tool_total_timeout_seconds)
    max_result_bytes = max(256, settings.agent_read_tool_max_result_bytes)
    results: list[dict[str, Any]] = []
    for request in requests:
        if time.monotonic() - total_started >= total_timeout:
            _record_policy_result(
                db,
                run=run,
                task=task,
                tool_name="<total-time-budget>",
                arguments={},
                status="timed_out",
                category="total_time_budget_exceeded",
            )
            raise AgentToolTimedOut("Read-only tool total time budget exceeded")
        if not isinstance(request, dict):
            _record_policy_result(
                db,
                run=run,
                task=task,
                tool_name="<invalid-request>",
                arguments={},
                status="denied",
                category="invalid_request",
            )
            raise AgentToolDenied("Each read-only tool request must be an object")
        name = str(request.get("name") or "")[:128]
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            _record_policy_result(
                db,
                run=run,
                task=task,
                tool_name=name or "<missing>",
                arguments={},
                status="denied",
                category="invalid_arguments",
            )
            raise AgentToolDenied("Read-only tool arguments must be an object")
        call = AgentToolCall(
            agent_run_id=run.id,
            task_id=task.id,
            agent_type=task.agent_type,
            tool_name=name or "<missing>",
            mode="read_only",
            status="running",
            input_digest=_digest(arguments),
        )
        db.add(call)
        db.flush()

        _, tools = _all_tools()
        tool = tools.get(name)
        if tool is None or task.agent_type not in tool.allowed_agents:
            call.status = "denied"
            call.error_category = "policy_denied"
            raise AgentToolDenied("Read-only tool is not allowed for this agent")

        started = time.monotonic()
        timeout = min(
            tool.timeout_seconds,
            max(0.1, settings.agent_read_tool_timeout_seconds),
        )
        try:
            raw_result = tool.handler(db, arguments)
            encoded_result = jsonable_encoder(raw_result)
            if not isinstance(encoded_result, dict):
                raise AgentToolError("Read-only tool result must be a JSON object")
            result = cast(dict[str, Any], encoded_result)
            duration = time.monotonic() - started
            call.duration_ms = round(duration * 1000, 3)
            if duration > timeout or time.monotonic() - total_started > total_timeout:
                call.status = "timed_out"
                call.error_category = "time_budget_exceeded"
                raise AgentToolTimedOut("Read-only tool time budget exceeded")
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            call.result_bytes = len(encoded)
            if len(encoded) > max_result_bytes:
                call.status = "denied"
                call.error_category = "result_size_exceeded"
                raise AgentToolDenied("Read-only tool result size budget exceeded")
            call.status = "succeeded"
            results.append(
                {
                    "tool_call_id": call.id,
                    "name": tool.name,
                    "mode": "read_only",
                    "duration_ms": call.duration_ms,
                    "result": result,
                }
            )
        except AgentToolDenied:
            if call.status == "running":
                call.status = "denied"
                call.error_category = "input_denied"
            raise
        except AgentToolTimedOut:
            if call.status == "running":
                call.status = "timed_out"
                call.error_category = "time_budget_exceeded"
            raise
        except Exception as exc:
            call.duration_ms = round((time.monotonic() - started) * 1000, 3)
            call.status = "failed"
            call.error_category = type(exc).__name__[:64]
            raise AgentToolError("Read-only tool execution failed") from exc
    return results
