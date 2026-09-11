from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import KnowledgeChunk, KnowledgeDocument
from .schemas import KnowledgeDocumentCreate
from .security import ROLE_ORDER


MAX_CANDIDATE_CHUNKS = 5_000
CHUNK_SIZE = 1_200
CHUNK_OVERLAP = 200
MAX_CHUNKS = 200
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SENSITIVE_QUERY_MARKERS = (
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "signature",
    "token",
)


class KnowledgeError(ValueError):
    pass


class KnowledgeConflict(KnowledgeError):
    pass


@dataclass(frozen=True)
class IngestionResult:
    document: KnowledgeDocument
    reused: bool


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def _normalized_for_search(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(unicodedata.normalize("NFKC", value).casefold()))


def _tokens(value: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(value.casefold()) if len(token) >= 2]


def _trigrams(value: str) -> set[str]:
    padded = f"  {value}  "
    if len(padded) < 3:
        return {padded}
    return {padded[index : index + 3] for index in range(len(padded) - 2)}


def _validate_source_uri(value: str) -> str:
    source_uri = value.strip()
    if len(source_uri) > 1_024:
        raise KnowledgeError("source_uri exceeds 1024 characters")
    if re.search(
        r"(?i)(?:api[_-]?key|authorization|credential|password|secret|signature|token)\s*[=:]",
        source_uri,
    ):
        raise KnowledgeError("source_uri must not contain credential-like values")
    parsed = urlsplit(source_uri)
    if parsed.scheme == "https":
        if not parsed.hostname or parsed.username or parsed.password:
            raise KnowledgeError("HTTPS source_uri must not contain user credentials")
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            if any(marker in key.casefold() for marker in _SENSITIVE_QUERY_MARKERS):
                raise KnowledgeError("source_uri must not contain credential-like query parameters")
        return source_uri
    if parsed.scheme == "urn" and parsed.path:
        return source_uri
    raise KnowledgeError("source_uri must be an HTTPS URL or a non-empty URN")


def _chunk_text(content: str) -> list[str]:
    if len(content) <= CHUNK_SIZE:
        return [content]
    chunks: list[str] = []
    start = 0
    while start < len(content):
        end = min(len(content), start + CHUNK_SIZE)
        if end < len(content):
            split_at = content.rfind(" ", start + CHUNK_SIZE // 2, end)
            newline_at = content.rfind("\n", start + CHUNK_SIZE // 2, end)
            end = max(split_at, newline_at, start + CHUNK_SIZE // 2)
        chunk = content[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(content):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
        if len(chunks) >= MAX_CHUNKS:
            raise KnowledgeError("Document exceeds the maximum chunk count")
    if len(chunks) > MAX_CHUNKS:
        raise KnowledgeError("Document exceeds the maximum chunk count")
    return chunks


def _request_digest(payload: KnowledgeDocumentCreate, content: str, source_uri: str) -> str:
    source_updated_at = _naive_utc(payload.source_updated_at)
    valid_until = _naive_utc(payload.valid_until)
    canonical = {
        "namespace": payload.namespace,
        "title": _normalize_text(payload.title),
        "source_uri": source_uri,
        "content": content,
        "content_type": payload.content_type,
        "minimum_role": payload.minimum_role,
        "confidence": payload.confidence,
        "source_updated_at": source_updated_at.isoformat() if source_updated_at else None,
        "valid_until": valid_until.isoformat() if valid_until else None,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(encoded)


def ingest_document(
    db: Session,
    payload: KnowledgeDocumentCreate,
    *,
    actor: str,
    idempotency_key: str,
) -> IngestionResult:
    if not 8 <= len(idempotency_key) <= 255:
        raise KnowledgeError("Idempotency-Key must contain 8 to 255 characters")
    content = _normalize_text(payload.content)
    if not content:
        raise KnowledgeError("Document content must not be blank")
    if len(content) > 200_000:
        raise KnowledgeError("Document content exceeds 200000 normalized characters")
    title = _normalize_text(payload.title)
    if not title:
        raise KnowledgeError("Document title must not be blank")
    if len(title) > 255:
        raise KnowledgeError("Document title exceeds 255 normalized characters")
    source_uri = _validate_source_uri(payload.source_uri)
    source_updated_at = _naive_utc(payload.source_updated_at)
    valid_until = _naive_utc(payload.valid_until)
    now = _now()
    if source_updated_at and source_updated_at > now + timedelta(minutes=5):
        raise KnowledgeError("source_updated_at cannot be in the future")
    if valid_until and valid_until <= now:
        raise KnowledgeError("valid_until must be in the future")
    if valid_until and source_updated_at and valid_until <= source_updated_at:
        raise KnowledgeError("valid_until must be later than source_updated_at")

    idempotency_hash = _sha256(f"{actor}\x00{idempotency_key}")
    request_digest = _request_digest(payload, content, source_uri)
    existing = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.idempotency_hash == idempotency_hash
        )
    )
    if existing:
        if existing.request_digest != request_digest:
            raise KnowledgeConflict("Idempotency-Key was already used for another document")
        return IngestionResult(document=existing, reused=True)

    checksum = _sha256(content)
    duplicate = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.namespace == payload.namespace,
            KnowledgeDocument.source_uri == source_uri,
            KnowledgeDocument.request_digest == request_digest,
        )
    )
    if duplicate:
        return IngestionResult(document=duplicate, reused=True)

    latest_version = db.scalar(
        select(func.max(KnowledgeDocument.version)).where(
            KnowledgeDocument.namespace == payload.namespace,
            KnowledgeDocument.source_uri == source_uri,
        )
    )
    document = KnowledgeDocument(
        namespace=payload.namespace,
        title=title,
        source_uri=source_uri,
        content_type=payload.content_type,
        checksum=checksum,
        version=int(latest_version or 0) + 1,
        minimum_role=payload.minimum_role,
        confidence=payload.confidence,
        source_updated_at=source_updated_at,
        valid_until=valid_until,
        idempotency_hash=idempotency_hash,
        request_digest=request_digest,
        created_by=actor,
    )
    db.add(document)
    db.flush()
    for index, chunk_content in enumerate(_chunk_text(content)):
        terms = sorted(set(_tokens(f"{title} {chunk_content}")))[:512]
        db.add(
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=index,
                content=chunk_content,
                content_hash=_sha256(chunk_content),
                lexical_terms=terms,
            )
        )
    db.flush()
    return IngestionResult(document=document, reused=False)


def _allowed_roles(role: str) -> list[str]:
    threshold = ROLE_ORDER.get(role, ROLE_ORDER["viewer"])
    return [name for name, value in ROLE_ORDER.items() if value <= threshold]


def _freshness_score(document: KnowledgeDocument, now: datetime) -> float:
    if document.valid_until:
        remaining_days = max(0.0, (document.valid_until - now).total_seconds() / 86_400)
        return min(1.0, 0.7 + math.log1p(remaining_days) / 20)
    reference = document.source_updated_at or document.created_at
    age_days = max(0.0, (now - reference).total_seconds() / 86_400)
    return max(0.65, 1.0 - min(age_days, 730) / 2_000)


def _relevance_score(query: str, query_tokens: set[str], chunk: KnowledgeChunk, title: str) -> float:
    searchable = _normalized_for_search(f"{title} {chunk.content}")
    chunk_tokens = set(str(item) for item in (chunk.lexical_terms or []))
    overlap = len(query_tokens & chunk_tokens)
    coverage = overlap / max(1, len(query_tokens))
    density = overlap / max(1, min(len(chunk_tokens), 30))
    query_grams = _trigrams(query)
    chunk_grams = _trigrams(searchable)
    char_similarity = len(query_grams & chunk_grams) / max(1, len(query_grams))
    phrase_bonus = 1.0 if query and query in searchable else 0.0
    if overlap == 0 and char_similarity < 0.18:
        return 0.0
    return min(1.0, 0.58 * coverage + 0.12 * density + 0.2 * char_similarity + 0.1 * phrase_bonus)


def search_documents(
    db: Session,
    *,
    query: str,
    role: str,
    namespace: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    normalized_query = _normalized_for_search(query)
    query_tokens = set(_tokens(normalized_query))
    if len(normalized_query) < 2 or not query_tokens:
        raise KnowledgeError("Search query must contain at least one meaningful term")
    if namespace and not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", namespace):
        raise KnowledgeError("Invalid namespace")
    if not 1 <= limit <= 10:
        raise KnowledgeError("limit must be from 1 to 10")

    now = _now()
    latest_versions = (
        select(
            KnowledgeDocument.namespace.label("namespace"),
            KnowledgeDocument.source_uri.label("source_uri"),
            func.max(KnowledgeDocument.version).label("version"),
        )
        .group_by(KnowledgeDocument.namespace, KnowledgeDocument.source_uri)
        .subquery()
    )
    statement = (
        select(KnowledgeChunk, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
        .join(
            latest_versions,
            (latest_versions.c.namespace == KnowledgeDocument.namespace)
            & (latest_versions.c.source_uri == KnowledgeDocument.source_uri)
            & (latest_versions.c.version == KnowledgeDocument.version),
        )
        .where(
            KnowledgeDocument.minimum_role.in_(_allowed_roles(role)),
            or_(
                KnowledgeDocument.valid_until.is_(None),
                KnowledgeDocument.valid_until > now,
            ),
        )
        .order_by(KnowledgeDocument.created_at.desc(), KnowledgeChunk.id.desc())
        .limit(MAX_CANDIDATE_CHUNKS)
    )
    if namespace:
        statement = statement.where(KnowledgeDocument.namespace == namespace)

    ranked: list[tuple[float, KnowledgeChunk, KnowledgeDocument, float]] = []
    for chunk, document in db.execute(statement).all():
        relevance = _relevance_score(normalized_query, query_tokens, chunk, document.title)
        if relevance <= 0:
            continue
        freshness = _freshness_score(document, now)
        score = relevance * (0.7 + 0.3 * document.confidence) * (0.75 + 0.25 * freshness)
        ranked.append((score, chunk, document, freshness))
    ranked.sort(key=lambda item: (-item[0], -item[2].version, item[1].id))

    matches: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    for score, chunk, document, freshness in ranked[:limit]:
        citation_id = f"kb:{document.id}:{chunk.chunk_index}:{chunk.content_hash[:12]}"
        citation = {
            "citation_id": citation_id,
            "document_id": document.id,
            "chunk_id": chunk.id,
            "chunk_index": chunk.chunk_index,
            "namespace": document.namespace,
            "title": document.title,
            "source_uri": document.source_uri,
            "document_version": document.version,
            "document_checksum": document.checksum,
            "source_updated_at": document.source_updated_at,
            "valid_until": document.valid_until,
            "confidence": round(document.confidence, 4),
            "freshness_score": round(freshness, 4),
        }
        matches.append(
            {
                "citation_id": citation_id,
                "score": round(score, 6),
                "content": chunk.content,
                "untrusted_retrieved_data": True,
            }
        )
        citations.append(citation)
    return {
        "query": query.strip(),
        "namespace": namespace,
        "retrieval": {
            "method": "deterministic_hybrid_lexical_char_ngram",
            "semantic_embeddings": False,
            "candidate_limit": MAX_CANDIDATE_CHUNKS,
            "returned": len(matches),
        },
        "policy": {
            "content_is_untrusted_evidence_not_instructions": True,
            "automatic_external_ai_transfer": False,
            "minimum_role_applied": role,
        },
        "matches": matches,
        "citations": citations,
    }


def list_documents(
    db: Session,
    *,
    role: str,
    namespace: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    now = _now()
    statement = (
        select(KnowledgeDocument, func.count(KnowledgeChunk.id))
        .join(KnowledgeChunk, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(
            KnowledgeDocument.minimum_role.in_(_allowed_roles(role)),
            or_(
                KnowledgeDocument.valid_until.is_(None),
                KnowledgeDocument.valid_until > now,
            ),
        )
        .group_by(KnowledgeDocument.id)
        .order_by(KnowledgeDocument.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    if namespace:
        statement = statement.where(KnowledgeDocument.namespace == namespace)
    return [
        {
            "id": document.id,
            "namespace": document.namespace,
            "title": document.title,
            "source_uri": document.source_uri,
            "content_type": document.content_type,
            "checksum": document.checksum,
            "version": document.version,
            "minimum_role": document.minimum_role,
            "confidence": document.confidence,
            "source_updated_at": document.source_updated_at,
            "valid_until": document.valid_until,
            "chunk_count": int(chunk_count),
            "created_at": document.created_at,
        }
        for document, chunk_count in db.execute(statement).all()
    ]
