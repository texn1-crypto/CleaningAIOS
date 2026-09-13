from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from .chat import redact_sensitive_text
from .config import settings


request_correlation_id: ContextVar[str] = ContextVar("request_correlation_id", default="")


def _redact(value: str) -> str:
    safe = redact_sensitive_text(value)
    for secret in (
        settings.api_key,
        settings.telegram_bot_token,
        settings.smtp_password,
        settings.llm_api_key,
        settings.anthropic_api_key,
        settings.perplexity_api_key,
    ):
        if secret:
            safe = safe.replace(secret, "[REDACTED]")
    return safe


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "message": _redact(record.getMessage()),
        }
        correlation_id = str(
            getattr(record, "correlation_id", "") or request_correlation_id.get()
        )
        if correlation_id:
            payload["correlation_id"] = correlation_id[:128]
        for field in (
            "event",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "task_id",
            "agent_type",
            "outcome",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = _redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))[:4000]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(service: str) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    if settings.log_format.lower() == "json":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter(service))
        logging.basicConfig(level=level, handlers=[handler], force=True)
    else:
        logging.basicConfig(level=level, force=True)
    # These libraries may include credentials in request URLs. The formatter is
    # a second line of defence, but routine request logging stays disabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
