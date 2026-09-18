from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import settings
from .models import AuditLog, BusinessRecord
from .notifications import queue_owner_notification


TWENTY_STATE_RECORD_TYPE = "integration_state"
TWENTY_STATE_EXTERNAL_ID = "twenty_crm"
TWENTY_SYNC_STATUSES = {"owner_review", "qualified", "sales_ready", "won"}
_LOCAL_HTTP_HOSTS = {
    "127.0.0.1",
    "localhost",
    "host.docker.internal",
    "twenty-server",
}
TWENTY_SYNC_LOCK_KEY = 8_421_179_203_031_677_101
_REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class TwentyCRMError(RuntimeError):
    pass


class TwentyCRMConfigurationError(TwentyCRMError):
    pass


class TwentyCRMProviderError(TwentyCRMError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _validated_base_url(value: str) -> str:
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
        raise TwentyCRMConfigurationError("Twenty base URL is invalid")
    if parsed.scheme == "https":
        return raw
    if parsed.scheme == "http" and hostname in _LOCAL_HTTP_HOSTS and parsed.port is not None:
        return raw
    raise TwentyCRMConfigurationError(
        "Twenty must use HTTPS; only the named local self-hosted service may use HTTP"
    )


def configuration_status() -> str:
    if not settings.twenty_enabled:
        return "disabled"
    if not settings.twenty_api_key.strip():
        return "credentials_required"
    try:
        _validated_base_url(settings.twenty_base_url)
    except TwentyCRMConfigurationError:
        return "invalid_configuration"
    if settings.twenty_sync_batch_size < 1 or settings.twenty_sync_interval_minutes < 1:
        return "invalid_configuration"
    return "configured_not_verified"


def _safe_public_website(value: object) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or parsed.fragment:
        return ""
    return raw[:1_024]


def _verified_organization_lead(lead: BusinessRecord) -> bool:
    data = lead.data or {}
    website = _safe_public_website(data.get("website"))
    return bool(
        data.get("contact_scope") == "organization"
        and website.startswith("https://")
        and str(data.get("last_verified_at") or "").strip()
    )


def _company_payload(lead: BusinessRecord) -> dict[str, Any]:
    data = lead.data or {}
    website = _safe_public_website(data.get("website"))
    payload: dict[str, Any] = {"name": " ".join(lead.title.split())[:255]}
    if website:
        hostname = (urlparse(website).hostname or "").lower()
        payload["domainName"] = {
            "primaryLinkLabel": hostname[:255],
            "primaryLinkUrl": website,
        }
    return payload


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _state_record(db: Session) -> BusinessRecord:
    row = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == TWENTY_STATE_RECORD_TYPE,
            BusinessRecord.external_id == TWENTY_STATE_EXTERNAL_ID,
        )
    )
    if row is None:
        row = BusinessRecord(
            record_type=TWENTY_STATE_RECORD_TYPE,
            external_id=TWENTY_STATE_EXTERNAL_ID,
            title="Twenty CRM",
            status="not_configured",
            owner="sales",
            source="twenty_rest_api",
            data={},
        )
        db.add(row)
        db.flush()
    return row


def _update_state(
    db: Session,
    *,
    status: str,
    attempted_at: datetime,
    summary: dict[str, Any],
) -> BusinessRecord:
    row = _state_record(db)
    old = row.data or {}
    row.status = status
    row.data = {
        "last_attempt_at": attempted_at.isoformat(),
        "last_success_at": (
            attempted_at.isoformat() if status == "ready" else old.get("last_success_at")
        ),
        "summary": summary,
        "secret_stored_in_database": False,
        "automatic_outreach": False,
    }
    return row


def _read_bounded_json(response: httpx.Response) -> dict[str, Any]:
    body = bytearray()
    maximum = max(1_024, settings.twenty_max_response_bytes)
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > maximum:
            raise TwentyCRMProviderError("response_too_large")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TwentyCRMProviderError("invalid_response") from exc
    if not isinstance(value, dict):
        raise TwentyCRMProviderError("invalid_response")
    return value


def _remote_id(value: dict[str, Any]) -> str:
    candidates: list[object] = [value.get("id")]
    data = value.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("id"))
        for key in ("company", "createCompany", "updateCompany"):
            nested = data.get(key)
            if isinstance(nested, dict):
                candidates.append(nested.get("id"))
    for candidate in candidates:
        remote_id = str(candidate or "").strip()
        if _REMOTE_ID.fullmatch(remote_id):
            return remote_id
    raise TwentyCRMProviderError("invalid_response")


def _send_json(
    client: httpx.Client,
    *,
    method: str,
    url: str,
    lead_id: int,
    fingerprint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request = client.build_request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {settings.twenty_api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": f"cleaningaios-lead-{lead_id}-{fingerprint[:20]}",
        },
        json=payload,
    )
    try:
        response = client.send(request, stream=True, follow_redirects=False)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise TwentyCRMProviderError("unavailable") from exc
    try:
        if response.status_code in {401, 403}:
            raise TwentyCRMProviderError("credentials_required")
        if response.status_code == 429:
            raise TwentyCRMProviderError("rate_limited")
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            raise TwentyCRMProviderError("unavailable")
        if response.status_code < 200 or response.status_code >= 300:
            raise TwentyCRMProviderError("schema_rejected")
        return _read_bounded_json(response)
    finally:
        response.close()


def _site_host(value: object) -> str:
    return (urlparse(str(value or "")).hostname or "").lower().rstrip(".")


def _find_existing_company(
    client: httpx.Client,
    *,
    base_url: str,
    lead_id: int,
    fingerprint: str,
    payload: dict[str, Any],
) -> str:
    name = str(payload["name"])
    expected_host = _site_host((payload.get("domainName") or {}).get("primaryLinkUrl"))
    if not expected_host:
        return ""
    response = _send_json(
        client,
        method="POST",
        url=urljoin(f"{base_url}/", "graphql"),
        lead_id=lead_id,
        fingerprint=f"reconcile-{fingerprint}",
        payload={
            "query": (
                "query FindCleaningAIOSCompany($filter: CompanyFilterInput) { "
                "companies(filter: $filter) { edges { node { id name domainName { "
                "primaryLinkUrl } } } } }"
            ),
            "variables": {"filter": {"name": {"eq": name}}},
        },
    )
    if response.get("errors"):
        raise TwentyCRMProviderError("schema_rejected")
    data = response.get("data")
    companies = data.get("companies") if isinstance(data, dict) else None
    edges = companies.get("edges") if isinstance(companies, dict) else None
    if not isinstance(edges, list):
        raise TwentyCRMProviderError("invalid_response")
    matching_ids: list[str] = []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict) or str(node.get("name") or "") != name:
            continue
        domain = node.get("domainName")
        remote_host = _site_host(
            domain.get("primaryLinkUrl") if isinstance(domain, dict) else ""
        )
        if remote_host != expected_host:
            continue
        matching_ids.append(_remote_id(node))
    unique_ids = sorted(set(matching_ids))
    if len(unique_ids) > 1:
        raise TwentyCRMProviderError("reconciliation_ambiguous")
    return unique_ids[0] if unique_ids else ""


def _request_company(
    client: httpx.Client,
    *,
    base_url: str,
    lead_id: int,
    fingerprint: str,
    payload: dict[str, Any],
    remote_id: str,
) -> tuple[str, str]:
    reconciled_id = remote_id
    operation = "updated" if remote_id else "created"
    if not reconciled_id:
        reconciled_id = _find_existing_company(
            client,
            base_url=base_url,
            lead_id=lead_id,
            fingerprint=fingerprint,
            payload=payload,
        )
        if reconciled_id:
            operation = "reconciled"
    path = f"rest/companies/{reconciled_id}" if reconciled_id else "rest/companies"
    response_data = _send_json(
        client,
        method="PATCH" if reconciled_id else "POST",
        url=urljoin(f"{base_url}/", path),
        lead_id=lead_id,
        fingerprint=fingerprint,
        payload=payload,
    )
    response_id = _remote_id(response_data)
    if reconciled_id and response_id != reconciled_id:
        raise TwentyCRMProviderError("invalid_response")
    return response_id, operation


def _acquire_sync_lock(db: Session) -> bool:
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(
        db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": TWENTY_SYNC_LOCK_KEY},
        )
    )


def _mark_lead(
    lead: BusinessRecord,
    *,
    status: str,
    fingerprint: str,
    attempted_at: datetime,
    remote_id: str = "",
    error_category: str = "",
) -> None:
    previous = (lead.data or {}).get("twenty_sync")
    if not isinstance(previous, dict):
        previous = {}
    lead.data = {
        **(lead.data or {}),
        "twenty_sync": {
            "status": status,
            "fingerprint": fingerprint,
            "remote_id": remote_id or str(previous.get("remote_id") or ""),
            "attempts": int(previous.get("attempts") or 0) + 1,
            "consecutive_failures": (
                0 if status == "synced" else int(previous.get("consecutive_failures") or 0) + 1
            ),
            "last_attempt_at": attempted_at.isoformat(),
            "last_success_at": (
                attempted_at.isoformat()
                if status == "synced"
                else previous.get("last_success_at")
            ),
            "error_category": error_category,
        },
    }


def _notify_configuration(db: Session, *, status: str, now: datetime) -> None:
    if status not in {
        "credentials_required",
        "invalid_configuration",
        "unavailable",
        "partial",
    }:
        return
    next_step = {
        "credentials_required": (
            "Создайте отдельный API-ключ в Twenty → Настройки → API & Webhooks "
            "и сохраните его только в окружении сервера."
        ),
        "invalid_configuration": "Проверьте адрес Twenty и параметры интервала синхронизации.",
        "unavailable": "Проверьте доступность локального Twenty и повторите контрольный запуск.",
        "partial": (
            "Проверьте схему объекта Company в Twenty: часть карточек отклонена API."
        ),
    }[status]
    queue_owner_notification(
        db,
        idempotency_key=f"twenty-crm:{status}:{now.date().isoformat()}:telegram",
        channel="telegram",
        resource_type=TWENTY_STATE_RECORD_TYPE,
        resource_id=TWENTY_STATE_EXTERNAL_ID,
        subject="Twenty CRM требует настройки",
        body=(
            "Синхронизация подтверждённых лидов с Twenty CRM не выполнена. "
            f"Безопасная категория: {status}. {next_step}"
        ),
        data={"integration": "twenty_crm", "status": status, "secret_included": False},
        severity="high",
        correlation_id=f"twenty-crm:{now.date().isoformat()}",
    )


def sync_verified_leads(
    db: Session,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project verified leads into Twenty without changing CRM authority or outreach state."""

    current = now or utcnow()
    if not _acquire_sync_lock(db):
        return {
            "status": "already_running",
            "eligible": 0,
            "attempted": 0,
            "synced": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "external_messages_sent": False,
            "evidence": [{"type": "twenty_crm_sync", "status": "already_running"}],
        }
    configured = configuration_status()
    if configured != "configured_not_verified":
        summary = {
            "status": configured,
            "eligible": 0,
            "synced": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
        }
        _update_state(db, status=configured, attempted_at=current, summary=summary)
        _notify_configuration(db, status=configured, now=current)
        return {
            **summary,
            "external_messages_sent": False,
            "evidence": [{"type": "twenty_crm_sync", "status": configured}],
        }

    base_url = _validated_base_url(settings.twenty_base_url)
    batch_size = min(45, max(1, settings.twenty_sync_batch_size))
    status_rows = db.scalars(
        select(BusinessRecord)
        .where(
            BusinessRecord.record_type == "lead",
            BusinessRecord.status.in_(TWENTY_SYNC_STATUSES),
        )
        .order_by(BusinessRecord.id)
        .limit(500)
    ).all()
    rows = [lead for lead in status_rows if _verified_organization_lead(lead)]
    candidates: list[tuple[BusinessRecord, dict[str, Any], str, str, int, str]] = []
    skipped = 0
    for lead in rows:
        payload = _company_payload(lead)
        fingerprint = _fingerprint(payload)
        sync = (lead.data or {}).get("twenty_sync")
        if not isinstance(sync, dict):
            sync = {}
        if sync.get("fingerprint") == fingerprint and sync.get("status") in {
            "synced",
            "schema_rejected",
        }:
            skipped += 1
            continue
        candidates.append(
            (
                lead,
                payload,
                fingerprint,
                str(sync.get("remote_id") or ""),
                int(sync.get("consecutive_failures") or 0),
                str(sync.get("last_attempt_at") or ""),
            )
        )
    candidates.sort(key=lambda item: (item[4], item[5], item[0].id))
    pending = candidates[:batch_size]

    owned_client = client is None
    http_client = client or httpx.Client(timeout=max(1.0, settings.twenty_timeout_seconds))
    synced = updated = failed = attempted = 0
    failure_category = ""
    synced_ids: list[int] = []
    try:
        for lead, payload, fingerprint, existing_remote_id, _, _ in pending:
            attempted += 1
            try:
                remote_id, operation = _request_company(
                    http_client,
                    base_url=base_url,
                    lead_id=lead.id,
                    fingerprint=fingerprint,
                    payload=payload,
                    remote_id=existing_remote_id,
                )
            except TwentyCRMProviderError as exc:
                failed += 1
                failure_category = exc.category
                _mark_lead(
                    lead,
                    status=(
                        "schema_rejected"
                        if exc.category in {"schema_rejected", "reconciliation_ambiguous"}
                        else "retry_required"
                    ),
                    fingerprint=fingerprint,
                    attempted_at=current,
                    remote_id=existing_remote_id,
                    error_category=exc.category,
                )
                if exc.category in {
                    "credentials_required",
                    "rate_limited",
                    "unavailable",
                    "response_too_large",
                    "invalid_response",
                }:
                    break
                continue
            _mark_lead(
                lead,
                status="synced",
                fingerprint=fingerprint,
                attempted_at=current,
                remote_id=remote_id,
            )
            db.add(
                AuditLog(
                    actor="sales",
                    action="twenty_crm.lead_projected",
                    resource_type="lead",
                    resource_id=str(lead.id),
                    details={
                        "operation": operation,
                        "remote_id": remote_id,
                        "fingerprint": fingerprint,
                        "contacts_transmitted": False,
                        "automatic_outreach": False,
                    },
                )
            )
            synced += int(operation == "created")
            updated += int(operation in {"updated", "reconciled"})
            synced_ids.append(lead.id)
    finally:
        if owned_client:
            http_client.close()

    status = "ready"
    if failure_category:
        status = (
            "credentials_required"
            if failure_category == "credentials_required"
            else "unavailable"
            if failure_category
            in {"rate_limited", "unavailable", "response_too_large", "invalid_response"}
            else "partial"
        )
    summary = {
        "status": status,
        "eligible": len(rows),
        "attempted": attempted,
        "synced": synced,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "failure_category": failure_category,
    }
    _update_state(db, status=status, attempted_at=current, summary=summary)
    _notify_configuration(db, status=status, now=current)
    db.add(
        AuditLog(
            actor="sales",
            action="twenty_crm.sync_completed",
            resource_type=TWENTY_STATE_RECORD_TYPE,
            resource_id=TWENTY_STATE_EXTERNAL_ID,
            details={**summary, "synced_lead_ids": synced_ids},
        )
    )
    return {
        **summary,
        "synced_lead_ids": synced_ids,
        "external_messages_sent": False,
        "evidence": [
            {
                "type": "twenty_crm_sync",
                "status": status,
                "synced_lead_ids": synced_ids,
            }
        ],
    }


def sync_status(db: Session) -> dict[str, Any]:
    row = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == TWENTY_STATE_RECORD_TYPE,
            BusinessRecord.external_id == TWENTY_STATE_EXTERNAL_ID,
        )
    )
    return {
        "configuration_status": configuration_status(),
        "runtime_status": row.status if row is not None else "not_measured",
        "last_attempt_at": (row.data or {}).get("last_attempt_at") if row is not None else None,
        "last_success_at": (row.data or {}).get("last_success_at") if row is not None else None,
        "summary": (row.data or {}).get("summary", {}) if row is not None else {},
        "system_of_record": "cleaningaios",
        "projection_target": "twenty_crm",
        "automatic_outreach": False,
    }
