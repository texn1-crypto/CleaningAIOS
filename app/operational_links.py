from __future__ import annotations

from urllib.parse import urlparse

from .config import settings


def _safe_http_url(value: str, *, fallback: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return fallback
    return candidate


def operational_links() -> dict[str, str]:
    base = _safe_http_url(
        settings.public_base_url,
        fallback="http://localhost:8000",
    )
    mission_control = f"{base}/mission-control"
    return {
        "mission_control": mission_control,
        "crm": _safe_http_url(settings.crm_public_url, fallback=mission_control),
    }
