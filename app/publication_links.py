from __future__ import annotations

import re
from urllib.parse import quote, urlparse

from .config import settings
from .models import ContentItem


_SAFE_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SOCIAL_HOSTS = {
    "telegram": {"t.me", "www.t.me"},
    "vk": {"vk.com", "www.vk.com"},
    "odnoklassniki": {"ok.ru", "www.ok.ru"},
}


def _safe_https_url(value: object, *, allowed_hosts: set[str]) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    parsed = urlparse(candidate)
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.hostname.lower() not in allowed_hosts
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return candidate


def _website_url() -> str:
    base = str(settings.public_base_url or "").strip().rstrip("/")
    parsed = urlparse(base)
    if not parsed.hostname:
        return ""
    safe_base = _safe_https_url(base, allowed_hosts={parsed.hostname.lower()})
    return f"{safe_base}/#news" if safe_base else ""


def publication_url(channel: str, external_post_id: object = "") -> str:
    """Build a public, provider-owned URL without accepting arbitrary hosts."""
    external_id = str(external_post_id or "").strip()
    if channel == "website":
        return _website_url()
    if not _SAFE_EXTERNAL_ID.fullmatch(external_id):
        return ""
    if channel == "telegram":
        base = _safe_https_url(
            settings.social_telegram_url,
            allowed_hosts=_SOCIAL_HOSTS["telegram"],
        ).rstrip("/")
        return f"{base}/{quote(external_id, safe='')}" if base else ""
    if channel == "vk":
        group_id = str(settings.vk_community_id or "").strip().lstrip("-")
        if not group_id.isdigit() or not external_id.isdigit():
            return ""
        return f"https://vk.com/wall-{group_id}_{external_id}"
    if channel == "odnoklassniki":
        group_id = str(settings.odnoklassniki_group_id or "").strip()
        if not group_id.isdigit():
            return ""
        return f"https://ok.ru/group/{group_id}/topic/{quote(external_id, safe='')}"
    return ""


def verified_publication_url(item: ContentItem) -> str:
    """Return a URL only when the database proves a successful publication."""
    if item.status != "published" or item.published_at is None:
        return ""
    metrics = item.metrics or {}
    if item.channel == "website":
        return publication_url("website")
    external_id = metrics.get("external_post_id")
    if not external_id or metrics.get("publication_status") != "published":
        return ""
    allowed_hosts = _SOCIAL_HOSTS.get(item.channel)
    derived = publication_url(item.channel, external_id)
    stored = (
        _safe_https_url(metrics.get("public_post_url"), allowed_hosts=allowed_hosts)
        if allowed_hosts
        else ""
    )
    return stored if stored and stored == derived else derived
