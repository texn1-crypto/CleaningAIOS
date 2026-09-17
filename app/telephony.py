from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .capability_flags import external_action_gate
from .chat import redact_sensitive_text
from .config import settings
from .models import AuditLog, BusinessRecord, ContactEvent, Suppression


TERMINAL_CALL_STATUSES = {
    "completed",
    "no_answer",
    "busy",
    "failed",
    "cancelled",
    "opt_out",
}
CALL_STATUS_RANK = {
    "queued": 0,
    "waiting_configuration": 0,
    "rate_limited": 0,
    "reconciliation_required": 1,
    "ringing": 2,
    "answered": 3,
    "completed": 4,
    "no_answer": 4,
    "busy": 4,
    "failed": 4,
    "cancelled": 4,
    "opt_out": 4,
}
TELEPHONY_RATE_LIMIT_LOCK_KEY = 4_850_467_165_561_422_156


class TelephonyError(RuntimeError):
    pass


class TelephonyConfigurationError(TelephonyError):
    pass


class TelephonyUnavailable(TelephonyError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_e164(value: Any) -> str:
    normalized = re.sub(r"[\s()\-]", "", str(value or "").strip())
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized):
        raise ValueError("Lead phone must use E.164 format")
    return normalized


def _validated_gateway_url(value: str) -> str:
    raw = value.strip()
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise TelephonyConfigurationError("Telephony gateway URL is invalid")
    if parsed.scheme == "https" and parsed.port in {None, 443}:
        return raw
    if (
        not settings.production
        and parsed.scheme == "http"
        and hostname in {"127.0.0.1", "localhost", "voice-gateway"}
        and parsed.port is not None
    ):
        return raw
    raise TelephonyConfigurationError(
        "Telephony gateway must use HTTPS; local development may use a named internal service"
    )


def configuration_status() -> str:
    if not settings.telephony_enabled:
        return "disabled"
    if not settings.telephony_api_token.strip() or not settings.telephony_webhook_secret.strip():
        return "credentials_required"
    try:
        _validated_gateway_url(settings.telephony_gateway_url)
        _call_window(now_utc())
        if settings.telephony_calls_per_minute < 1 or settings.telephony_calls_per_day < 1:
            raise TelephonyConfigurationError("Telephony rate limits must be positive")
    except (TelephonyConfigurationError, ZoneInfoNotFoundError):
        return "invalid_configuration"
    return "configured_not_verified"


def _consent_evidence(lead: BusinessRecord) -> dict[str, str] | None:
    data = lead.data or {}
    if data.get("phone_contact_consent") is True:
        source = str(data.get("phone_contact_consent_source") or "").strip()
        recorded_at = str(data.get("phone_contact_consent_at") or "").strip()
        if source and recorded_at:
            return {"kind": "phone_contact_consent", "source": source, "recorded_at": recorded_at}
    if data.get("callback_requested") is True:
        recorded_at = str(data.get("callback_requested_at") or "").strip()
        if recorded_at:
            return {"kind": "customer_requested_callback", "source": "crm", "recorded_at": recorded_at}
    return None


def queue_consented_sales_call(
    db: Session,
    *,
    lead_id: int,
    idempotency_key: str,
    purpose: str,
    script: str,
    scheduled_at: datetime | None,
    task_id: int,
    approval_id: int,
) -> dict[str, Any]:
    lead = db.get(BusinessRecord, lead_id)
    if lead is None or lead.record_type != "lead":
        raise LookupError("CRM lead not found")
    consent = _consent_evidence(lead)
    if consent is None:
        raise ValueError("Verified phone consent or a customer-requested callback is required")
    phone = normalize_e164((lead.data or {}).get("phone"))
    if db.get(Suppression, phone):
        raise ValueError("Lead phone is suppressed")
    safe_purpose = redact_sensitive_text(purpose.strip())[:500]
    safe_script = redact_sensitive_text(script.strip())[:4_000]
    if not safe_purpose or not safe_script:
        raise ValueError("Call purpose and script are required")
    existing = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == "sales_call",
            BusinessRecord.external_id == idempotency_key,
        )
    )
    if existing is not None:
        return {
            "status": existing.status,
            "call_id": existing.id,
            "duplicate": True,
            "evidence": [{"type": "sales_call_queued", "call_id": existing.id}],
        }
    scheduled = scheduled_at or now_utc()
    if scheduled.tzinfo is not None:
        scheduled = scheduled.astimezone(timezone.utc).replace(tzinfo=None)
    row = BusinessRecord(
        record_type="sales_call",
        external_id=idempotency_key,
        title=f"Sales call · {lead.title}"[:255],
        status="queued",
        source="cleaningaios_telephony",
        data={
            "lead_id": lead.id,
            "phone_last4": phone[-4:],
            "purpose": safe_purpose,
            "script": safe_script,
            "scheduled_at": scheduled.isoformat(),
            "consent": consent,
            "task_id": task_id,
            "approval_id": approval_id,
            "recording_enabled": False,
            "ai_disclosure_required": True,
        },
    )
    db.add(row)
    db.flush()
    db.add(
        AuditLog(
            actor="sales",
            action="telephony.call_queued",
            resource_type="sales_call",
            resource_id=str(row.id),
            details={"lead_id": lead.id, "task_id": task_id, "approval_id": approval_id},
        )
    )
    return {
        "status": "queued",
        "call_id": row.id,
        "lead_id": lead.id,
        "recording_enabled": False,
        "evidence": [{"type": "sales_call_queued", "call_id": row.id, "lead_id": lead.id}],
    }


def _scheduled_at(row: BusinessRecord) -> datetime:
    value = str((row.data or {}).get("scheduled_at") or "")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return row.created_at
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _call_window(now: datetime) -> tuple[bool, datetime]:
    if not 0 <= settings.telephony_daily_start_hour < settings.telephony_daily_end_hour <= 23:
        raise TelephonyConfigurationError("Telephony calling hours are invalid")
    zone = ZoneInfo(settings.telephony_timezone)
    aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    local_now = aware.astimezone(zone)
    local_start = local_now.replace(
        hour=settings.telephony_daily_start_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    local_end = local_now.replace(
        hour=settings.telephony_daily_end_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    return (
        local_start <= local_now < local_end,
        local_start.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _acquire_rate_limit_lock(db: Session) -> None:
    """Serialize the PostgreSQL rate check through the reservation commit."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": TELEPHONY_RATE_LIMIT_LOCK_KEY},
        )


def _read_bounded_json(response: httpx.Response) -> dict[str, Any]:
    maximum = max(1_024, settings.telephony_max_response_bytes)
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > maximum:
            raise TelephonyUnavailable(
                "Telephony response exceeded the configured limit"
            )
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelephonyUnavailable("Telephony provider returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise TelephonyUnavailable("Telephony provider returned an invalid object")
    return value


def place_next_sales_call(db: Session, *, now: datetime | None = None) -> bool:
    if not external_action_gate(db, "voice_call")["allowed"]:
        return False
    current = now or now_utc()
    try:
        in_window, window_start = _call_window(current)
    except (TelephonyConfigurationError, ZoneInfoNotFoundError):
        return False
    if not in_window:
        return False
    _acquire_rate_limit_lock(db)
    attempted_statuses = (
        "reconciliation_required",
        "ringing",
        "answered",
        "completed",
        "no_answer",
        "busy",
        "failed",
        "cancelled",
        "opt_out",
    )
    calls_minute = int(
        db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == "sales_call",
                BusinessRecord.status.in_(attempted_statuses),
                BusinessRecord.updated_at >= current - timedelta(minutes=1),
            )
        )
        or 0
    )
    calls_day = int(
        db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == "sales_call",
                BusinessRecord.status.in_(attempted_statuses),
                BusinessRecord.updated_at >= window_start,
            )
        )
        or 0
    )
    if calls_minute >= max(1, settings.telephony_calls_per_minute) or calls_day >= max(
        1, settings.telephony_calls_per_day
    ):
        return False
    candidates = db.scalars(
        select(BusinessRecord)
        .where(
            BusinessRecord.record_type == "sales_call",
            BusinessRecord.status.in_(["queued", "waiting_configuration", "rate_limited"]),
        )
        .order_by(BusinessRecord.id)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    row = next((item for item in candidates if _scheduled_at(item) <= current), None)
    if row is None:
        return False
    if configuration_status() != "configured_not_verified":
        row.status = "waiting_configuration"
        row.data = {**(row.data or {}), "failure_category": configuration_status()}
        db.commit()
        return True
    lead = db.get(BusinessRecord, int((row.data or {}).get("lead_id") or 0))
    if lead is None or lead.record_type != "lead" or _consent_evidence(lead) is None:
        row.status = "blocked_consent"
        row.data = {**(row.data or {}), "failure_category": "consent_unavailable"}
        db.commit()
        return True
    phone = normalize_e164((lead.data or {}).get("phone"))
    if db.get(Suppression, phone):
        row.status = "suppressed"
        db.commit()
        return True
    gateway_url = _validated_gateway_url(settings.telephony_gateway_url)
    public_base = settings.public_base_url.rstrip("/")
    if settings.production and not public_base.startswith("https://"):
        row.status = "waiting_configuration"
        row.data = {**(row.data or {}), "failure_category": "https_callback_required"}
        db.commit()
        return True
    payload = {
        "call_attempt_id": row.external_id,
        "to": phone,
        "purpose": str((row.data or {}).get("purpose") or ""),
        "script": str((row.data or {}).get("script") or ""),
        "callback_url": f"{public_base}/api/telephony/callback",
        "recording_enabled": False,
        "ai_disclosure_required": True,
    }
    row.status = "reconciliation_required"
    row.data = {**(row.data or {}), "attempt_started_at": current.isoformat(), "failure_category": None}
    db.add(
        AuditLog(
            actor="worker",
            action="telephony.call_attempt_started",
            resource_type="sales_call",
            resource_id=str(row.id),
            details={"lead_id": lead.id, "recording_enabled": False},
        )
    )
    db.commit()
    try:
        with httpx.Client(
            timeout=max(1.0, settings.telephony_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                gateway_url,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {settings.telephony_api_token.strip()}",
                    "Idempotency-Key": str(row.external_id),
                },
            ) as response:
                response.raise_for_status()
                body = _read_bounded_json(response)
        provider_call_id = str(body.get("provider_call_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", provider_call_id):
            raise TelephonyUnavailable("Telephony provider did not return a safe call ID")
        row.status = "ringing" if body.get("status") == "ringing" else "queued"
        row.data = {
            **(row.data or {}),
            "provider_call_id": provider_call_id,
            "provider_status": str(body.get("status") or "accepted")[:64],
            "accepted_at": current.isoformat(),
        }
        db.add(
            AuditLog(
                actor="worker",
                action="telephony.call_accepted",
                resource_type="sales_call",
                resource_id=str(row.id),
                details={"provider_status": row.data["provider_status"]},
            )
        )
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        row.status = "waiting_configuration" if status_code in {401, 403} else "rate_limited" if status_code == 429 else "failed"
        row.data = {**(row.data or {}), "failure_category": f"provider_http_{status_code}"}
    except (httpx.HTTPError, ValueError, TelephonyError):
        row.status = "reconciliation_required"
        row.data = {**(row.data or {}), "failure_category": "provider_outcome_unknown"}
    db.commit()
    return True


def apply_call_callback(
    db: Session,
    *,
    call_attempt_id: str,
    provider_call_id: str,
    provider_event_id: str,
    status: str,
    duration_seconds: int,
    outcome_summary: str,
) -> tuple[BusinessRecord, bool]:
    if status not in CALL_STATUS_RANK:
        raise ValueError("Unsupported telephony callback status")
    row = db.scalar(
        select(BusinessRecord)
        .where(
            BusinessRecord.record_type == "sales_call",
            BusinessRecord.external_id == call_attempt_id,
        )
        .with_for_update()
    )
    if row is None:
        raise LookupError("Sales call not found")
    data = dict(row.data or {})
    existing_provider_id = str(data.get("provider_call_id") or "")
    if existing_provider_id and existing_provider_id != provider_call_id:
        raise ValueError("Provider call ID does not match")
    processed = [str(item) for item in data.get("provider_event_ids") or []]
    if provider_event_id in processed:
        return row, True
    if row.status in TERMINAL_CALL_STATUSES:
        data["provider_event_ids"] = [*processed, provider_event_id][-20:]
        row.data = data
        return row, True
    if CALL_STATUS_RANK[status] < CALL_STATUS_RANK.get(row.status, 0):
        data["provider_event_ids"] = [*processed, provider_event_id][-20:]
        row.data = data
        return row, True
    safe_summary = redact_sensitive_text(outcome_summary.strip())[:2_000]
    row.status = status
    row.data = {
        **data,
        "provider_call_id": provider_call_id,
        "provider_event_ids": [*processed, provider_event_id][-20:],
        "duration_seconds": max(0, min(int(duration_seconds), 86_400)),
        "outcome_summary": safe_summary,
        "provider_status": status,
        "last_callback_at": now_utc().isoformat(),
    }
    lead = db.get(BusinessRecord, int(row.data.get("lead_id") or 0))
    if lead is not None and lead.record_type == "lead" and status in TERMINAL_CALL_STATUSES:
        phone = normalize_e164((lead.data or {}).get("phone"))
        if status == "opt_out" and db.get(Suppression, phone) is None:
            db.add(Suppression(address=phone, reason="phone_opt_out"))
        contact = ContactEvent(
            record_id=lead.id,
            channel="phone",
            direction="outbound",
            subject="AI-assisted sales call",
            body=safe_summary,
            outcome=status,
        )
        db.add(contact)
        db.flush()
        lead.data = {
            **(lead.data or {}),
            "last_call_id": row.id,
            "last_call_outcome": status,
            "last_call_at": now_utc().isoformat(),
        }
        row.data = {**row.data, "terminal_contact_event_id": contact.id}
    db.add(
        AuditLog(
            actor="telephony_gateway",
            action="telephony.call_status_received",
            resource_type="sales_call",
            resource_id=str(row.id),
            details={"status": status, "provider_event_id": provider_event_id},
        )
    )
    return row, False
