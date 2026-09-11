from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from .config import settings


MAX_FEED_BYTES = 2_000_000
RELEVANCE_TERMS = (
    "clean",
    "janitor",
    "facility",
    "hygiene",
    "sanitary",
    "disinfect",
    "restroom",
    "floor care",
    "housekeeping",
)


@dataclass(frozen=True)
class CleaningNewsItem:
    title: str
    summary: str
    source_url: str
    source_name: str
    published_at: datetime | None

    def evidence(self) -> dict:
        value = asdict(self)
        value["published_at"] = self.published_at.isoformat() if self.published_at else None
        return value


def configured_feed_urls() -> list[str]:
    return list(dict.fromkeys(value.strip() for value in settings.cleaning_news_feeds.split(",") if value.strip()))


def _safe_https_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Cleaning news URLs must be public HTTPS URLs without credentials")
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "0.0.0.0"} or hostname.endswith((".local", ".internal")):
        raise ValueError("Private cleaning news hosts are not allowed")
    return value.strip()


def _text(value: str, *, limit: int) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(unescape(without_tags).split())[:limit]


def _node_text(node: ElementTree.Element, *names: str) -> str:
    for child in list(node):
        local_name = child.tag.rsplit("}", 1)[-1].lower()
        if local_name in names:
            if local_name == "link" and child.attrib.get("href"):
                return str(child.attrib["href"])
            return "".join(child.itertext()).strip()
    return ""


def _published(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def parse_cleaning_news_feed(raw: bytes, *, feed_url: str, now: datetime) -> list[CleaningNewsItem]:
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError("Cleaning news feed exceeds the size limit")
    root = ElementTree.fromstring(raw)
    source_name = _text(_node_text(root, "title"), limit=120) or urlparse(feed_url).hostname or "Источник"
    candidates = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    cutoff = now - timedelta(days=max(1, settings.cleaning_news_max_age_days))
    result: list[CleaningNewsItem] = []
    for node in candidates:
        title = _text(_node_text(node, "title"), limit=240)
        summary = _text(_node_text(node, "description", "summary", "content"), limit=1200)
        source_url = _node_text(node, "link")
        try:
            source_url = _safe_https_url(source_url)
        except ValueError:
            continue
        published_at = _published(_node_text(node, "pubdate", "published", "updated", "date"))
        if published_at and published_at < cutoff:
            continue
        haystack = f"{title} {summary}".lower()
        if not title or not any(term in haystack for term in RELEVANCE_TERMS):
            continue
        result.append(CleaningNewsItem(title, summary, source_url, source_name, published_at))
    return result


def fetch_cleaning_news(*, now: datetime | None = None, limit: int = 20) -> list[CleaningNewsItem]:
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    items: dict[str, CleaningNewsItem] = {}
    with httpx.Client(
        timeout=settings.cleaning_news_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": "CleaningAIOS-NewsAgent/1.0"},
    ) as client:
        for configured_url in configured_feed_urls():
            try:
                feed_url = _safe_https_url(configured_url)
                response = client.get(feed_url)
                response.raise_for_status()
                final_url = _safe_https_url(str(response.url))
                parsed = parse_cleaning_news_feed(response.content, feed_url=final_url, now=current)
            except (httpx.HTTPError, ElementTree.ParseError, ValueError):
                continue
            for item in parsed:
                items.setdefault(item.source_url, item)
    return sorted(
        items.values(),
        key=lambda item: item.published_at or datetime.min,
        reverse=True,
    )[: max(1, limit)]


def _response_text(body: dict) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    return "".join(
        str(chunk.get("text") or "")
        for output in body.get("output", [])
        if isinstance(output, dict) and output.get("type") == "message"
        for chunk in output.get("content", [])
        if isinstance(chunk, dict) and chunk.get("type") == "output_text"
    )


def editorialize_news(item: CleaningNewsItem) -> tuple[str, str, str]:
    """Create a source-bound Russian caption without granting the model tools."""
    fallback_title = f"Новость клининговой отрасли: {item.title}"[:240]
    fallback_summary = (
        f"Профессиональное издание опубликовало материал «{item.title}». "
        "Мы следим за мировыми практиками и отдельно оцениваем, что из них применимо к обслуживанию объектов в России. "
        "Подробности доступны по ссылке на первоисточник."
    )[:900]
    if not settings.llm_api_key:
        return fallback_title, fallback_summary, "deterministic_source_excerpt"
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["title", "summary"],
        "additionalProperties": False,
    }
    payload = {
        "model": settings.llm_model,
        "input": [
            {
                "role": "system",
                "content": (
                    "Ты редактор русскоязычного канала клининговой компании. Переведи и кратко перескажи "
                    "только переданные факты. Не добавляй числа, имена, обещания или выводы, которых нет в источнике. "
                    "Заголовок до 140 знаков, summary до 650 знаков. Верни только JSON."
                ),
            },
            {"role": "user", "content": json.dumps(item.evidence(), ensure_ascii=False)},
        ],
        "text": {"format": {"type": "json_schema", "name": "cleaning_news_post", "strict": True, "schema": schema}},
        "max_output_tokens": 800,
        "store": False,
    }
    try:
        endpoint = f"{settings.llm_base_url.rstrip('/')}/responses"
        _safe_https_url(endpoint)
        with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
            response = client.post(
                endpoint,
                headers={"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            value = json.loads(_response_text(response.json()))
        title = _text(str(value.get("title") or ""), limit=140)
        summary = _text(str(value.get("summary") or ""), limit=650)
        if title and summary:
            return title, summary, "openai_source_bound_editor"
    except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return fallback_title, fallback_summary, "deterministic_source_excerpt"
