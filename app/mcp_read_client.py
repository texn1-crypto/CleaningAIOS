from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .config import settings


NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){15,18}(?!\d)")
PHONE_PATTERN = re.compile(r"(?<!\w)\+?\d(?:[\s()\-]*\d){9,14}(?!\w)")
SENSITIVE_KEY_PARTS = (
    "address",
    "bank",
    "card",
    "credential",
    "cvv",
    "email",
    "password",
    "phone",
    "private",
    "recipient",
    "secret",
    "token",
)
KNOWN_AGENTS = frozenset(
    {
        "ceo",
        "copywriter",
        "creative",
        "evolution_researcher",
        "finance",
        "growth_officer",
        "hr",
        "marketing",
        "meta_brain",
        "orchestrator",
        "request_analyst",
        "research",
        "sales",
        "system_admin",
        "tender",
    }
)


class MCPConfigurationError(ValueError):
    pass


class MCPReadError(RuntimeError):
    pass


class MCPPolicyDenied(MCPReadError):
    pass


@dataclass(frozen=True)
class MCPReadTool:
    name: str
    server_name: str
    remote_name: str
    endpoint: str
    allowed_agents: frozenset[str]
    timeout_seconds: float
    secret_ref: str


def _validated_endpoint(value: Any) -> str:
    endpoint = str(value or "").strip()
    parsed = urlparse(endpoint)
    schemes = {"https"} if settings.production else {"http", "https"}
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise MCPConfigurationError("MCP endpoint must be an absolute approved URL")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise MCPConfigurationError("Local MCP endpoints are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise MCPConfigurationError("Private MCP endpoint addresses are not allowed")
    return endpoint


def _configured_tools() -> list[MCPReadTool]:
    if settings.agent_mcp_protocol_version != "2026-07-28":
        raise MCPConfigurationError("Unsupported MCP protocol version")
    try:
        rows = json.loads(settings.agent_mcp_read_servers_json or "[]")
    except json.JSONDecodeError as exc:
        raise MCPConfigurationError("MCP server configuration is not valid JSON") from exc
    if not isinstance(rows, list) or len(rows) > 10:
        raise MCPConfigurationError("MCP server configuration must be a list of at most 10 servers")
    tools: list[MCPReadTool] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("mode") != "read_only":
            raise MCPConfigurationError("Every MCP server must explicitly use read_only mode")
        server_name = str(row.get("name") or "")
        if not NAME_PATTERN.fullmatch(server_name):
            raise MCPConfigurationError("MCP server name is invalid")
        endpoint = _validated_endpoint(row.get("url"))
        allowed = row.get("allowed_agents")
        if not isinstance(allowed, list) or not allowed:
            raise MCPConfigurationError("MCP server needs a non-empty agent allowlist")
        allowed_agents = frozenset(str(value) for value in allowed)
        if not allowed_agents.issubset(KNOWN_AGENTS):
            raise MCPConfigurationError("MCP server contains an unknown agent")
        secret_ref = str(row.get("secret_ref") or "")
        if secret_ref and not re.fullmatch(r"MCP_READ_[A-Z0-9_]{1,100}", secret_ref):
            raise MCPConfigurationError("MCP secret_ref must use the MCP_READ_ prefix")
        names = row.get("tools")
        if not isinstance(names, list) or not names or len(names) > 20:
            raise MCPConfigurationError("MCP server needs 1 to 20 allowed tools")
        timeout = float(row.get("timeout_seconds") or settings.agent_read_tool_timeout_seconds)
        timeout = max(0.1, min(timeout, settings.agent_read_tool_timeout_seconds, 30.0))
        for remote_name_value in names:
            remote_name = str(remote_name_value)
            if not NAME_PATTERN.fullmatch(remote_name):
                raise MCPConfigurationError("MCP tool name is invalid")
            tools.append(
                MCPReadTool(
                    name=f"mcp.{server_name}.{remote_name}",
                    server_name=server_name,
                    remote_name=remote_name,
                    endpoint=endpoint,
                    allowed_agents=allowed_agents,
                    timeout_seconds=timeout,
                    secret_ref=secret_ref,
                )
            )
    if len(tools) > 100 or len({item.name for item in tools}) != len(tools):
        raise MCPConfigurationError("MCP tool names must be unique and total at most 100")
    return tools


def configured_mcp_tools() -> tuple[str, list[MCPReadTool]]:
    try:
        tools = _configured_tools()
    except (MCPConfigurationError, TypeError, ValueError):
        return "invalid_configuration", []
    return ("configured" if tools else "disabled"), tools


def _reject_sensitive_arguments(value: Any, *, key: str = "") -> None:
    normalized = key.lower()
    if any(marker in normalized for marker in SENSITIVE_KEY_PARTS):
        raise MCPPolicyDenied("Sensitive arguments are forbidden for remote MCP tools")
    if isinstance(value, dict):
        for child_key, child in value.items():
            _reject_sensitive_arguments(child, key=str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            _reject_sensitive_arguments(child)
        return
    if isinstance(value, str) and (
        EMAIL_PATTERN.search(value) or CARD_PATTERN.search(value) or PHONE_PATTERN.search(value)
    ):
        raise MCPPolicyDenied("Personal or financial data is forbidden for remote MCP tools")


def call_mcp_read_tool(tool: MCPReadTool, arguments: dict[str, Any]) -> dict[str, Any]:
    encoded_arguments = json.dumps(arguments, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded_arguments) > 8_192:
        raise MCPPolicyDenied("Remote MCP arguments exceed the size limit")
    _reject_sensitive_arguments(arguments)
    request_id = uuid4().hex
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": settings.agent_mcp_protocol_version,
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool.remote_name,
    }
    if tool.secret_ref:
        secret = os.environ.get(tool.secret_ref, "")
        if not secret:
            raise MCPReadError("Remote MCP credentials are not configured")
        headers["Authorization"] = f"Bearer {secret}"
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool.remote_name,
            "arguments": arguments,
            "_meta": {
                "io.modelcontextprotocol/clientInfo": {
                    "name": "CleaningAIOS",
                    "version": "1.0",
                }
            },
        },
    }
    try:
        with httpx.Client(
            timeout=tool.timeout_seconds,
            follow_redirects=False,
            headers=headers,
        ) as client:
            response = client.post(tool.endpoint, json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise MCPReadError("Remote MCP request failed") from exc
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or body.get("id") != request_id:
        raise MCPReadError("Remote MCP response did not match the request")
    if body.get("error") is not None:
        raise MCPReadError("Remote MCP returned a protocol error")
    result = body.get("result")
    if not isinstance(result, dict):
        raise MCPReadError("Remote MCP result is not an object")
    if result.get("isError") or result.get("resultType") in {"input_required", "task"}:
        raise MCPReadError("Remote MCP tool did not complete synchronously")
    return {
        "untrusted_external_data": True,
        "server": tool.server_name,
        "tool": tool.remote_name,
        "result": result,
    }
