from __future__ import annotations

import json

import pytest

from app import agent_tools, crawl4ai_client
from app.config import settings


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield json.dumps(self.payload).encode("utf-8")


class _RawResponse(_Response):
    def __init__(self, body: bytes):
        self.body = body

    def iter_bytes(self):
        yield self.body


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "crawl4ai_enabled", True)
    monkeypatch.setattr(settings, "crawl4ai_base_url", "http://crawl4ai:11235")
    monkeypatch.setattr(settings, "crawl4ai_api_token", "test-only-token")
    monkeypatch.setattr(settings, "crawl4ai_timeout_seconds", 5)
    monkeypatch.setattr(
        crawl4ai_client,
        "_resolved_public_addresses",
        lambda hostname, port: {"93.184.216.34"},
    )


def test_crawl_public_page_uses_only_bounded_declarative_config(monkeypatch):
    _configure(monkeypatch)
    captured = {}

    class Client:
        def __init__(self, *args, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json):
            captured.update({"method": method, "url": url, "payload": json})
            return _Response(
                {
                    "success": True,
                    "results": [
                        {
                            "url": "https://example.com/research",
                            "success": True,
                            "status_code": 200,
                            "markdown": {"fit_markdown": "# Evidence\n" + "x" * 2_000},
                        }
                    ],
                }
            )

    monkeypatch.setattr(crawl4ai_client.httpx, "Client", Client)
    result = crawl4ai_client.crawl_public_page(
        {"url": "https://example.com/research#ignored", "max_chars": 1_000}
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "http://crawl4ai:11235/crawl"
    assert captured["client"]["follow_redirects"] is False
    assert captured["client"]["trust_env"] is False
    assert captured["client"]["headers"]["Authorization"] == "Bearer test-only-token"
    assert captured["payload"] == {
        "urls": ["https://example.com/research"],
        "browser_config": {
            "type": "BrowserConfig",
            "params": {"headless": True},
        },
        "crawler_config": {
            "type": "CrawlerRunConfig",
            "params": {
                "stream": False,
                "cache_mode": "bypass",
                "check_robots_txt": True,
                "page_timeout": 5_000,
            },
        },
    }
    serialized_payload = json.dumps(captured["payload"])
    for forbidden in ("js_code", "cookies", "headers", "proxy_config", "deep_crawl_strategy"):
        assert forbidden not in serialized_payload
    assert result["success"] is True
    assert result["untrusted_external_data"] is True
    assert result["automatic_action_allowed"] is False
    assert result["truncated"] is True
    assert result["content_chars"] == 1_000
    assert result["content_sha256"].startswith("sha256:")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://localhost/private",
        "https://127.0.0.1/private",
        "https://user:password@example.com/private",
        "https://example.com:8443/private",
        "https://example.com:invalid/private",
    ],
)
def test_crawl_public_page_rejects_unsafe_targets_before_network(monkeypatch, url):
    _configure(monkeypatch)

    class NoNetworkClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("unsafe URL reached the network client")

    monkeypatch.setattr(crawl4ai_client.httpx, "Client", NoNetworkClient)
    with pytest.raises(crawl4ai_client.Crawl4AIPolicyDenied):
        crawl4ai_client.crawl_public_page({"url": url})


def test_crawl_public_page_rejects_private_dns_answer(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        crawl4ai_client,
        "_resolved_public_addresses",
        lambda hostname, port: {"10.0.0.5"},
    )
    with pytest.raises(crawl4ai_client.Crawl4AIPolicyDenied):
        crawl4ai_client.crawl_public_page({"url": "https://example.com/private"})


def test_crawl_public_page_rejects_active_or_unknown_arguments(monkeypatch):
    _configure(monkeypatch)
    with pytest.raises(crawl4ai_client.Crawl4AIPolicyDenied):
        crawl4ai_client.crawl_public_page(
            {"url": "https://example.com/", "js_code": "return document.cookie"}
        )


@pytest.mark.parametrize("max_chars", [True, "12000", 999, 24_001])
def test_crawl_public_page_rejects_invalid_content_limits(monkeypatch, max_chars):
    _configure(monkeypatch)
    with pytest.raises(crawl4ai_client.Crawl4AIPolicyDenied):
        crawl4ai_client.crawl_public_page(
            {"url": "https://example.com/", "max_chars": max_chars}
        )


def test_crawl_public_page_caps_service_response_before_json_parsing(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(settings, "crawl4ai_max_response_bytes", 1_024)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json):
            return _RawResponse(b"x" * 1_025)

    monkeypatch.setattr(crawl4ai_client.httpx, "Client", Client)
    with pytest.raises(crawl4ai_client.Crawl4AIUnavailable):
        crawl4ai_client.crawl_public_page({"url": "https://example.com/"})


def test_failed_remote_page_returns_no_remote_error_text(monkeypatch):
    _configure(monkeypatch)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json):
            return _Response(
                {
                    "success": True,
                    "results": [
                        {
                            "url": "unknown",
                            "success": False,
                            "status_code": 403,
                            "error_message": "untrusted remote instructions",
                        }
                    ],
                }
            )

    monkeypatch.setattr(crawl4ai_client.httpx, "Client", Client)
    result = crawl4ai_client.crawl_public_page({"url": "https://example.com/"})

    assert result["success"] is False
    assert result["failure_category"] == "remote_page_unavailable"
    assert result["markdown"] == ""
    assert "untrusted remote instructions" not in json.dumps(result)


def test_successful_redirect_to_private_target_is_rejected(monkeypatch):
    _configure(monkeypatch)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, json):
            return _Response(
                {
                    "success": True,
                    "results": [
                        {
                            "url": "https://internal.local/redirected",
                            "success": True,
                            "status_code": 200,
                            "markdown": "must not be returned",
                        }
                    ],
                }
            )

    monkeypatch.setattr(crawl4ai_client.httpx, "Client", Client)
    with pytest.raises(crawl4ai_client.Crawl4AIPolicyDenied):
        crawl4ai_client.crawl_public_page({"url": "https://example.com/"})


def test_crawl_public_page_requires_explicit_enablement_and_token(monkeypatch):
    monkeypatch.setattr(settings, "crawl4ai_enabled", False)
    monkeypatch.setattr(settings, "crawl4ai_api_token", "")
    assert crawl4ai_client.configuration_status() == "disabled"
    with pytest.raises(crawl4ai_client.Crawl4AIUnavailable):
        crawl4ai_client.crawl_public_page({"url": "https://example.com/"})

    monkeypatch.setattr(settings, "crawl4ai_enabled", True)
    assert crawl4ai_client.configuration_status() == "credentials_required"

    monkeypatch.setattr(settings, "crawl4ai_api_token", "test-only-token")
    monkeypatch.setattr(settings, "crawl4ai_base_url", "http://db:5432")
    assert crawl4ai_client.configuration_status() == "invalid_configuration"


def test_agent_registry_exposes_crawl_only_to_selected_business_agents(monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "crawl_public_page",
        lambda arguments: {
            "success": True,
            "markdown": "public evidence",
            "untrusted_external_data": True,
            "automatic_action_allowed": False,
        },
    )
    tool = agent_tools.READ_ONLY_TOOLS["web.public_crawl"]
    result = tool.handler(None, {"url": "https://example.com/"})

    assert result["untrusted_external_data"] is True
    assert "research" in tool.allowed_agents
    assert "ceo" in tool.allowed_agents
    assert "tender" in tool.allowed_agents
    assert "marketing" in tool.allowed_agents
    assert "finance" not in tool.allowed_agents
    assert "hr" not in tool.allowed_agents
    assert "system_admin" not in tool.allowed_agents


def test_agent_runtime_attaches_crawl_result_as_audited_evidence(client, monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "crawl_public_page",
        lambda arguments: {
            "provider": "crawl4ai",
            "success": True,
            "resolved_url": arguments["url"],
            "markdown": "public evidence",
            "untrusted_external_data": True,
            "automatic_action_allowed": False,
        },
    )
    created = client.post(
        "/api/tasks",
        headers={"X-Role": "operator"},
        json={
            "title": "Crawl public evidence",
            "agent_type": "marketing",
            "payload": {
                "message": "research only",
                "read_only_tools": [
                    {
                        "name": "web.public_crawl",
                        "arguments": {"url": "https://example.com/"},
                    }
                ],
            },
        },
    ).json()
    result = client.post(
        f"/api/tasks/{created['id']}/run",
        headers={"X-Role": "operator"},
    ).json()

    assert result["status"] == "done"
    assert result["result"]["read_only_tool_results"][0]["name"] == "web.public_crawl"
    assert result["result"]["read_only_tool_results"][0]["result"][
        "untrusted_external_data"
    ] is True
    assert any(
        row.get("tool_name") == "web.public_crawl"
        for row in result["result"]["evidence"]
    )


def test_agent_runtime_denies_crawl_for_non_research_finance_role(client, monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "crawl_public_page",
        lambda arguments: pytest.fail("denied role reached Crawl4AI"),
    )
    created = client.post(
        "/api/tasks",
        headers={"X-Role": "operator"},
        json={
            "title": "Denied crawl",
            "agent_type": "finance",
            "max_attempts": 1,
            "payload": {
                "read_only_tools": [
                    {
                        "name": "web.public_crawl",
                        "arguments": {"url": "https://example.com/"},
                    }
                ]
            },
        },
    ).json()
    result = client.post(
        f"/api/tasks/{created['id']}/run",
        headers={"X-Role": "operator"},
    ).json()

    assert result["status"] == "failed"
    assert result["result"]["error_type"] == "AgentToolDenied"


def test_agent_runtime_allows_only_one_crawl_per_run(client, monkeypatch):
    monkeypatch.setattr(
        agent_tools,
        "crawl_public_page",
        lambda arguments: pytest.fail("over-budget crawl reached Crawl4AI"),
    )
    created = client.post(
        "/api/tasks",
        headers={"X-Role": "operator"},
        json={
            "title": "Too many crawls",
            "agent_type": "research",
            "max_attempts": 1,
            "payload": {
                "read_only_tools": [
                    {"name": "web.public_crawl", "arguments": {"url": "https://example.com/a"}},
                    {"name": "web.public_crawl", "arguments": {"url": "https://example.com/b"}},
                ]
            },
        },
    ).json()
    result = client.post(
        f"/api/tasks/{created['id']}/run",
        headers={"X-Role": "operator"},
    ).json()

    assert result["status"] == "failed"
    assert result["result"]["error_type"] == "AgentToolDenied"
