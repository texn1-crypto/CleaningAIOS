from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from .chat import redact_sensitive_text
from .config import settings


_ALLOWED_HOSTS = {
    "127.0.0.1",
    "::1",
    "host.docker.internal",
    "localhost",
    "openjarvis",
}
_AUTHENTICATED_HOSTS = {"openjarvis"}


class OpenJarvisError(RuntimeError):
    """Base error for the optional OpenJarvis adviser."""


class OpenJarvisConfigurationError(OpenJarvisError):
    pass


class OpenJarvisUnavailable(OpenJarvisError):
    pass


def _validated_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise OpenJarvisConfigurationError("OpenJarvis port is invalid") from exc
    if (
        parsed.scheme != "http"
        or hostname not in _ALLOWED_HOSTS
        or port != 8011
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise OpenJarvisConfigurationError(
            "OpenJarvis must use the isolated service on port 8011"
        )
    return raw


def configuration_status() -> str:
    if not settings.openjarvis_enabled:
        return "disabled"
    try:
        parsed = urlparse(_validated_base_url(settings.openjarvis_base_url))
    except OpenJarvisConfigurationError:
        return "invalid_configuration"
    if (parsed.hostname or "").lower().rstrip(".") in _AUTHENTICATED_HOSTS:
        if not settings.openjarvis_api_key.strip():
            return "credentials_required"
    return "configured"


def _answer_from_response(data: Any) -> str:
    if not isinstance(data, dict):
        raise OpenJarvisUnavailable("OpenJarvis returned an invalid response")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise OpenJarvisUnavailable("OpenJarvis returned an invalid response")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    answer = message.get("content") if isinstance(message, dict) else None
    if not isinstance(answer, str) or not answer.strip():
        raise OpenJarvisUnavailable("OpenJarvis returned an empty response")
    response_limit = min(3_500, max(500, settings.openjarvis_max_response_chars))
    return answer.strip()[:response_limit]


async def ask_openjarvis(message: str) -> str:
    """Ask the adviser without granting it tools or execution authority."""
    if configuration_status() != "configured":
        raise OpenJarvisUnavailable("OpenJarvis is not configured")
    safe_message = redact_sensitive_text(" ".join(message.split()).strip())
    if not safe_message:
        raise OpenJarvisError("OpenJarvis request is empty")
    prompt_limit = min(8_000, max(500, settings.openjarvis_max_prompt_chars))
    safe_message = safe_message[:prompt_limit]
    payload = {
        "model": settings.openjarvis_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты персональный советник владельца CleaningAIOS. Отвечай "
                    "по-русски, кратко и практично. Ты не выполняешь действия, не "
                    "принимаешь финансовые, юридические, кадровые или тендерные "
                    "решения и не утверждаешь, что изменил систему. Никогда не проси "
                    "и не повторяй пароли, токены, персональные или платёжные данные. "
                    "Для действий внутри CleaningAIOS рекомендуй создать обычную "
                    "проверяемую задачу в системе."
                ),
            },
            {"role": "user", "content": safe_message},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    headers = {"Accept": "application/json"}
    if settings.openjarvis_api_key:
        headers["Authorization"] = f"Bearer {settings.openjarvis_api_key}"
    try:
        async with httpx.AsyncClient(
            timeout=min(180.0, max(1.0, settings.openjarvis_timeout_seconds)),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response_limit = min(
                1_000_000,
                max(1_024, settings.openjarvis_max_response_bytes),
            )
            async with client.stream(
                "POST",
                f"{_validated_base_url(settings.openjarvis_base_url)}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > response_limit:
                        raise OpenJarvisUnavailable(
                            "OpenJarvis response exceeded the configured size limit"
                        )
            return _answer_from_response(json.loads(body))
    except (httpx.HTTPError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OpenJarvisUnavailable("OpenJarvis request failed") from exc
