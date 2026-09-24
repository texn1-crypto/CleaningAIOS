from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import settings
from .daily_owner_pack import _build_pdf, _public_lead_entries
from .models import AuditLog, BusinessRecord
from .notifications import queue_owner_notification


LEAD_REPORT_RECORD_TYPE = "lead_discovery_report"
SCOUT_LABELS = {
    "management_lead_scout": "УК, ТСЖ и управляющие организации",
    "commercial_lead_scout": "коммерческие объекты и корпоративные сайты",
    "tender_lead_scout": "тендеры и закупки клининга",
    "social_lead_scout": "публичные бизнес-страницы и сообщества",
    "lead_scout": "открытый поиск потенциальных заказчиков",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _material_snapshot(row: BusinessRecord) -> dict[str, Any]:
    data = row.data or {}
    return {
        "id": row.id,
        "title": row.title,
        "region": str(data.get("region") or ""),
        "city": str(data.get("city") or ""),
        "organization_type": str(data.get("organization_type") or ""),
        "inn": str(data.get("inn") or ""),
        "website": str(data.get("website") or ""),
        "public_emails": sorted(str(value) for value in data.get("public_emails") or []),
        "public_phones": sorted(str(value) for value in data.get("public_phones") or []),
        "source_urls": sorted(str(value) for value in data.get("source_urls") or []),
    }


def _snapshot_signature(snapshot: dict[str, Any]) -> str:
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _safe_report_path(value: object) -> Path:
    root = Path(settings.document_storage_path).resolve()
    path = Path(str(value or "")).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Lead report path is outside document storage") from exc
    return path


def verify_lead_report_artifact(report: BusinessRecord) -> tuple[Path, dict[str, str]]:
    artifact = (report.data or {}).get("artifact") or {}
    path = _safe_report_path(artifact.get("storage_path"))
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise FileNotFoundError("Lead report PDF is unavailable")
    raw = path.read_bytes()
    checksum = str(artifact.get("sha256") or "")
    if not raw.startswith(b"%PDF"):
        raise RuntimeError("Lead report is not a valid PDF")
    if len(checksum) != 64 or hashlib.sha256(raw).hexdigest() != checksum:
        raise RuntimeError("Lead report checksum mismatch")
    return path, {
        "filename": Path(str(artifact.get("filename") or path.name)).name,
        "content_type": "application/pdf",
    }


def _queue_report_delivery(db: Session, report: BusinessRecord) -> int:
    verify_lead_report_artifact(report)
    artifact = report.data["artifact"]
    label = SCOUT_LABELS.get(str(report.data["scout_role"]), SCOUT_LABELS["lead_scout"])
    notification = queue_owner_notification(
        db,
        idempotency_key=f"{report.external_id}:telegram",
        channel="telegram",
        resource_type=LEAD_REPORT_RECORD_TYPE,
        resource_id=str(report.id),
        subject=f"Найдены новые лиды: {report.data['lead_count']}",
        body=(
            f"Канал: {label}. Подробности, контакты и ссылки на публичные источники находятся в PDF. "
            "Рассылка автоматически не запускалась."
        ),
        data={
            "report_id": report.id,
            "document_path": artifact["storage_path"],
            "document_filename": artifact["filename"],
            "document_sha256": artifact["sha256"],
            "document_content_type": artifact["content_type"],
        },
    )
    if not report.data.get("notification_id"):
        report.data = {**report.data, "notification_id": notification.id}
        db.add(AuditLog(
            actor=str(report.data["scout_role"]),
            action="lead_discovery_report.delivery_queued",
            resource_type=LEAD_REPORT_RECORD_TYPE,
            resource_id=str(report.id),
            details={"notification_id": notification.id, "automatic_outreach": False},
        ))
    return int(notification.id)


def _is_unsent_preview(db: Session, lead: BusinessRecord) -> bool:
    report_id = str((lead.data or {}).get("instant_lead_report_id") or "")
    if not report_id.isdigit():
        return False
    report = db.get(BusinessRecord, int(report_id))
    return bool(
        report is not None
        and report.record_type == LEAD_REPORT_RECORD_TYPE
        and lead.id in (report.data or {}).get("lead_ids", [])
        and not (report.data or {}).get("notification_id")
    )


def build_instant_lead_report(
    db: Session,
    *,
    lead_ids: list[int],
    scout_role: str,
    generated_at: datetime | None = None,
    notify_owner: bool = True,
) -> dict[str, Any]:
    """Create one idempotent PDF for newly discovered or materially changed leads."""

    if db.get_bind().dialect.name == "postgresql":
        # Serialize preview promotion with normal publication across worker replicas.
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 761943821})
    current = generated_at or _utcnow()
    if current.tzinfo is None:
        current_aware = current.replace(tzinfo=timezone.utc)
    else:
        current_aware = current.astimezone(timezone.utc)
    local = current_aware.astimezone(ZoneInfo(settings.lead_report_timezone))
    rows: list[BusinessRecord] = []
    snapshots: list[tuple[BusinessRecord, dict[str, Any], str]] = []
    for lead_id in sorted(set(lead_ids)):
        row = db.get(BusinessRecord, lead_id)
        if row is None or row.record_type != "lead":
            continue
        snapshot = _material_snapshot(row)
        signature = _snapshot_signature(snapshot)
        if str((row.data or {}).get("instant_lead_report_signature") or "") == signature:
            if not notify_owner or not _is_unsent_preview(db, row):
                continue
        rows.append(row)
        snapshots.append((row, snapshot, signature))
    if not rows:
        return {
            "status": "no_new_information",
            "report_id": None,
            "lead_count": 0,
            "notification_id": None,
            "external_messages_sent": False,
            "evidence": [],
        }

    batch_payload = {
        "scout_role": scout_role,
        "leads": [{"id": snapshot["id"], "signature": signature} for _, snapshot, signature in snapshots],
    }
    digest = hashlib.sha256(
        json.dumps(batch_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    external_id = f"lead-report:{digest}"
    existing = db.scalar(
        select(BusinessRecord).where(
            BusinessRecord.record_type == LEAD_REPORT_RECORD_TYPE,
            BusinessRecord.external_id == external_id,
        )
    )
    if existing is not None:
        verify_lead_report_artifact(existing)
        if notify_owner:
            _queue_report_delivery(db, existing)
        for row, _, signature in snapshots:
            row.data = {
                **(row.data or {}),
                "instant_lead_report_signature": signature,
                "instant_lead_report_id": existing.id,
                "instant_lead_reported_at": local.isoformat(),
            }
        db.flush()
        return {
            "status": "completed",
            "reused": True,
            "report_id": existing.id,
            "lead_count": int((existing.data or {}).get("lead_count") or 0),
            "notification_id": (existing.data or {}).get("notification_id"),
            "external_messages_sent": False,
            "evidence": [{"type": LEAD_REPORT_RECORD_TYPE, "report_id": existing.id, "reused": True}],
        }

    label = SCOUT_LABELS.get(scout_role, SCOUT_LABELS["lead_scout"])
    directory = Path(settings.document_storage_path) / "reports" / "leads" / local.date().isoformat()
    path = directory / f"new-leads-{digest[:16]}.pdf"
    checksum = _build_pdf(
        path,
        title=f"Новые лиды: {label}",
        generated_at=local,
        report_day=local.date(),
        summary_rows=[
            ("Новых или обновлённых лидов", len(rows)),
            ("Канал поиска", label),
            ("Публичных источников", len({url for _, snapshot, _ in snapshots for url in snapshot["source_urls"]})),
            ("Автоматическая рассылка", "не выполнялась"),
        ],
        entries=_public_lead_entries(rows),
    )
    artifact = {
        "storage_path": str(path),
        "filename": path.name,
        "sha256": checksum,
        "content_type": "application/pdf",
    }
    report = BusinessRecord(
        record_type=LEAD_REPORT_RECORD_TYPE,
        external_id=external_id,
        title=f"Новые лиды · {label} · {local.strftime('%d.%m.%Y %H:%M')}",
        status="completed",
        owner="marketing",
        source="centralized_lead_intelligence",
        data={
            "lead_count": len(rows),
            "lead_ids": [row.id for row in rows],
            "scout_role": scout_role,
            "source_count": len({url for _, snapshot, _ in snapshots for url in snapshot["source_urls"]}),
            "generated_at": local.isoformat(),
            "artifact": artifact,
            "automatic_outreach": False,
        },
    )
    db.add(report)
    db.flush()
    notification_id = None
    if notify_owner:
        notification_id = _queue_report_delivery(db, report)
    else:
        report.data = {**report.data, "notification_id": None}
    for row, _, signature in snapshots:
        row.data = {
            **(row.data or {}),
            "instant_lead_report_signature": signature,
            "instant_lead_report_id": report.id,
            "instant_lead_reported_at": local.isoformat(),
        }
    db.add(
        AuditLog(
            actor=scout_role,
            action="lead_discovery_report.created",
            resource_type=LEAD_REPORT_RECORD_TYPE,
            resource_id=str(report.id),
            details={
                "lead_count": len(rows),
                "source_count": report.data["source_count"],
                "automatic_outreach": False,
            },
        )
    )
    db.flush()
    return {
        "status": "completed",
        "reused": False,
        "report_id": report.id,
        "lead_count": len(rows),
        "notification_id": notification_id,
        "artifact": artifact,
        "external_messages_sent": False,
        "evidence": [
            {
                "type": LEAD_REPORT_RECORD_TYPE,
                "report_id": report.id,
                "lead_count": len(rows),
                "sha256": checksum,
            }
        ],
    }
