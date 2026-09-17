from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

import httpx

from .config import settings


CRAWL4AI_VERSION = "0.9.3"
LOCAL_SERVICE_HOST = "crawl4ai"


class Crawl4AIError(RuntimeError):
    pass


class Crawl4AIConfigurationError(Crawl4AIError):
    pass


class Crawl4AIPolicyDenied(Crawl4AIError):
    pass


class Crawl4AIUnavailable(Crawl4AIError):
    pass


def _validated_service_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise Crawl4AIConfigurationError("Crawl4AI service URL is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise Crawl4AIConfigurationError("Crawl4AI service port is invalid") from exc
    if parsed.scheme == "http":
        allowed_hosts = {LOCAL_SERVICE_HOST}
        if not settings.production:
            allowed_hosts.update({"localhost", "127.0.0.1", "::1"})
        if hostname not in allowed_hosts or port not in {None, 11235}:
            raise Crawl4AIConfigurationError(
                "Plain HTTP is allowed only for the internal Crawl4AI service"
            )
    elif parsed.scheme == "https":
        if port not in {None, 443}:
            raise Crawl4AIConfigurationError("External Crawl4AI HTTPS must use port 443")
        try:
            service_address = ipaddress.ip_address(hostname)
        except ValueError:
            service_address = None
        if service_address is not None and not service_address.is_global:
            raise Crawl4AIConfigurationError("External Crawl4AI service must be public")
    else:
        raise Crawl4AIConfigurationError("Crawl4AI service must use internal HTTP or HTTPS")
    return raw


def _resolved_public_addresses(hostname: str, port: int) -> set[str]:
    try:
        return {
            str(item[4][0])
            for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise Crawl4AIPolicyDenied("Public page hostname cannot be resolved") from exc


def _validated_public_https_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not 8 <= len(raw) <= 2_048:
        raise Crawl4AIPolicyDenied("url must be from 8 to 2048 characters")
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise Crawl4AIPolicyDenied("Public page port is invalid") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
    ):
        raise Crawl4AIPolicyDenied(
            "Only public HTTPS URLs on the standard port without credentials are allowed"
        )
    if hostname == "localhost" or hostname.endswith((".local", ".internal", ".localhost")):
        raise Crawl4AIPolicyDenied("Private, local or reserved website hosts are forbidden")
    try:
        direct_address = ipaddress.ip_address(hostname)
    except ValueError:
        direct_address = None
    if direct_address is not None:
        raise Crawl4AIPolicyDenied("Direct IP address targets are forbidden")
    addresses = _resolved_public_addresses(hostname, 443)
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise Crawl4AIPolicyDenied("Private, local or reserved website addresses are forbidden")
    netloc = hostname
    return urlunparse(("https", netloc, parsed.path or "/", "", parsed.query, ""))


def _content_limit(arguments: dict[str, Any]) -> int:
    unknown = set(arguments) - {"url", "max_chars"}
    if unknown:
        raise Crawl4AIPolicyDenied("web.public_crawl received unsupported arguments")
    value = arguments.get("max_chars", settings.crawl4ai_default_content_chars)
    maximum = max(1_000, settings.crawl4ai_max_content_chars)
    if isinstance(value, bool) or not isinstance(value, int) or not 1_000 <= value <= maximum:
        raise Crawl4AIPolicyDenied(f"max_chars must be an integer from 1000 to {maximum}")
    return cast(int, value)


def _read_bounded_json(response: httpx.Response) -> dict[str, Any]:
    maximum = max(1_024, settings.crawl4ai_max_response_bytes)
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > maximum:
            raise Crawl4AIUnavailable("Crawl4AI response exceeded the configured size limit")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Crawl4AIUnavailable("Crawl4AI returned an invalid JSON response") from exc
    if not isinstance(value, dict):
        raise Crawl4AIUnavailable("Crawl4AI returned an invalid response object")
    return value


def _markdown_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("fit_markdown", "raw_markdown"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    return ""


def configuration_status() -> str:
    if not settings.crawl4ai_enabled:
        return "disabled"
    if not settings.crawl4ai_api_token.strip():
        return "credentials_required"
    try:
        _validated_service_base_url(settings.crawl4ai_base_url)
    except Crawl4AIConfigurationError:
        return "invalid_configuration"
    return "configured"


def crawl_public_page(arguments: dict[str, Any]) -> dict[str, Any]:
    if configuration_status() != "configured":
        raise Crawl4AIUnavailable("Crawl4AI is not configured")
    if not isinstance(arguments, dict):
        raise Crawl4AIPolicyDenied("web.public_crawl arguments must be an object")

    content_limit = _content_limit(arguments)
    requested_url = _validated_public_https_url(arguments.get("url"))
    base_url = _validated_service_base_url(settings.crawl4ai_base_url)
    payload = {
        "urls": [requested_url],
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
                "page_timeout": int(
                    max(1.0, settings.crawl4ai_timeout_seconds) * 1_000
                ),
            },
        },
    }
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {settings.crawl4ai_api_token.strip()}",
    }
    try:
        with httpx.Client(
            timeout=max(1.0, settings.crawl4ai_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        ) as client:
            with client.stream("POST", f"{base_url}/crawl", json=payload) as response:
                response.raise_for_status()
                data = _read_bounded_json(response)
    except httpx.HTTPError as exc:
        raise Crawl4AIUnavailable("Crawl4AI request failed") from exc

    rows = data.get("results")
    if data.get("success") is not True or not isinstance(rows, list) or len(rows) != 1:
        raise Crawl4AIUnavailable("Crawl4AI did not return one crawl result")
    row = rows[0]
    if not isinstance(row, dict):
        raise Crawl4AIUnavailable("Crawl4AI returned an invalid crawl result")

    success = row.get("success") is True
    resolved_url = (
        _validated_public_https_url(row.get("url") or requested_url)
        if success
        else requested_url
    )
    markdown = _markdown_text(row.get("markdown")) if success else ""
    truncated = len(markdown) > content_limit
    markdown = markdown[:content_limit]
    status_code = row.get("status_code")
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = None

    return {
        "provider": "crawl4ai",
        "provider_version": CRAWL4AI_VERSION,
        "success": success,
        "requested_url": requested_url,
        "resolved_url": resolved_url,
        "status_code": status_code,
        "failure_category": None if success else "remote_page_unavailable",
        "markdown": markdown,
        "content_chars": len(markdown),
        "content_sha256": "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "truncated": truncated,
        "untrusted_external_data": True,
        "automatic_action_allowed": False,
    }
