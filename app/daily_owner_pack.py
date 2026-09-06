from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.enums import TA_LEFT  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
from reportlab.lib.units import mm  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import BusinessRecord, OwnerNotification
from .notifications import queue_owner_notification
from .reports import build_activity_report


PUBLIC_LEAD_SOURCE = "perplexity_public_business_search"
MAX_REPORT_ROWS = 1_000


def _font_path() -> Path:
    candidates = [
        settings.proposal_font_path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError(
        "Не найден кириллический TTF-шрифт. Установите fonts-dejavu-core "
        "или задайте PROPOSAL_FONT_PATH."
    )


def _register_font() -> str:
    name = "CleaningAIDailyOwnerPack"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, str(_font_path())))
    return name


def _text(value: object, *, limit: int = 1_500) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) > limit:
        return normalized[: limit - 1] + "…"
    return normalized


def _list_text(value: object, *, limit: int = 3) -> str:
    if not isinstance(value, list):
        return ""
    return "; ".join(_text(item, limit=500) for item in value[:limit] if _text(item, limit=500))


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    raw = str(value or "")
    if len(raw) > 8_000:
        raw = raw[:7_999] + "…"
    rendered = "<br/>".join(escape(_text(line)) for line in raw.splitlines())
    return Paragraph(rendered, style)


def _build_pdf(
    path: Path,
    *,
    title: str,
    generated_at: datetime,
    report_day: date,
    summary_rows: list[tuple[str, object]],
    entries: list[dict[str, object]],
) -> str:
    root = Path(settings.document_storage_path).resolve()
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Daily owner report path is outside document storage") from exc
    path.parent.mkdir(parents=True, exist_ok=True)

    font = _register_font()
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DailyOwnerTitle",
        parent=styles["Title"],
        fontName=font,
        fontSize=21,
        leading=27,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#173f32"),
        spaceAfter=5 * mm,
    )
    heading = ParagraphStyle(
        "DailyOwnerHeading",
        parent=styles["Heading2"],
        fontName=font,
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#174f3c"),
        spaceBefore=3 * mm,
        spaceAfter=1.5 * mm,
    )
    body = ParagraphStyle(
        "DailyOwnerBody",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#29332e"),
        spaceAfter=1.5 * mm,
    )
    small = ParagraphStyle(
        "DailyOwnerSmall",
        parent=body,
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#657068"),
    )

    temporary_file = tempfile.NamedTemporaryFile(
        prefix=f".{path.stem}-",
        suffix=".tmp.pdf",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(temporary_file.name)
    temporary_file.close()
    document = SimpleDocTemplate(
        str(temporary),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=17 * mm,
        bottomMargin=16 * mm,
        title=title,
        author=settings.company_name,
    )

    def page(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#174f3c"))
        canvas.rect(0, A4[1] - 6 * mm, A4[0], 6 * mm, fill=1, stroke=0)
        canvas.setFont(font, 7.5)
        canvas.setFillColor(colors.HexColor("#657068"))
        canvas.drawString(16 * mm, 8 * mm, "CleaningAIOS · данные из PostgreSQL")
        canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"Страница {doc.page}")
        canvas.restoreState()

    story: list[Any] = [
        _paragraph(title, title_style),
        _paragraph(
            f"Дата отчёта: {report_day.isoformat()} · Актуальность: "
            f"{generated_at.strftime('%d.%m.%Y %H:%M %Z')} · Источник: PostgreSQL",
            small,
        ),
        Spacer(1, 2 * mm),
        Table(
            [[_paragraph(label, small), _paragraph(value, body)] for label, value in summary_rows],
            colWidths=[50 * mm, 124 * mm],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8f1ed")),
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#b8c9c1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            ),
        ),
        _paragraph("Список", heading),
    ]
    if not entries:
        story.append(_paragraph("За отчётный срез записей нет.", body))
    for index, entry in enumerate(entries, 1):
        label = entry.get("label") or f"Запись {index}"
        details = entry.get("details") or []
        lines = [str(item) for item in details if _text(item)] if isinstance(details, list) else []
        story.append(
            KeepTogether(
                [
                    _paragraph(f"{index}. {label}", heading),
                    _paragraph("\n".join(lines) or "Нет дополнительных данных.", body),
                ]
            )
        )
    story.append(
        _paragraph(
            "Отчёт предназначен владельцу. Публичность контакта не означает согласие на рассылку; "
            "suppression, unsubscribe, дедупликация, лимиты и owner approval остаются обязательными.",
            small,
        )
    )
    try:
        document.build(story, onFirstPage=page, onLaterPages=page)
        raw = temporary.read_bytes()
        if not raw.startswith(b"%PDF"):
            raise RuntimeError("Daily owner report is not a valid PDF")
        if len(raw) > settings.max_attachment_bytes:
            raise RuntimeError("Daily owner report exceeds the Telegram attachment limit")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _public_lead_entries(rows: Sequence[BusinessRecord]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for row in rows:
        data = row.data or {}
        details = [
            f"ID: {row.id} · Регион: {_text(data.get('region')) or 'не указан'} · Статус: {row.status}",
            f"Email: {_list_text(data.get('public_emails')) or 'нет'}",
            f"Телефон: {_list_text(data.get('public_phones')) or 'нет'}",
            f"Сайт: {_text(data.get('website')) or 'нет'}",
            f"Источники: {_list_text(data.get('source_urls')) or 'нет'}",
            f"Проверено: {_text(data.get('last_verified_at')) or 'не указано'} · "
            f"Согласие на рассылку: {_text(data.get('outreach_consent')) or 'не подтверждено'}",
        ]
        entries.append({"label": row.title, "details": details})
    return entries


def _crm_lead_entries(rows: Sequence[BusinessRecord]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for row in rows:
        data = row.data or {}
        email = data.get("email") or _list_text(data.get("public_emails"))
        phone = data.get("phone") or _list_text(data.get("public_phones"))
        details = [
            f"ID: {row.id} · Статус: {row.status} · Источник: {_text(row.source)}",
            f"Компания/контакт: {_text(data.get('company') or data.get('name') or row.title)}",
            f"Email: {_text(email) or 'нет'} · Телефон: {_text(phone) or 'нет'}",
            f"Услуга: {_text(data.get('service')) or 'не указана'} · "
            f"Регион: {_text(data.get('region')) or 'не указан'}",
            f"Оценка: {row.score if row.score is not None else 'нет'} · Обновлено: {row.updated_at.isoformat()}",
        ]
        entries.append({"label": row.title, "details": details})
    return entries


def _operation_entries(report: dict[str, Any]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for task in report.get("recent_completed_tasks") or []:
        entries.append(
            {
                "label": f"Задача #{task.get('id')} · {task.get('title')}",
                "details": [
                    f"Агент: {task.get('agent_type')} · Статус: {task.get('status')}",
                    f"Обновлено: {task.get('updated_at')}",
                ],
            }
        )
    for task in report.get("upcoming_tasks") or []:
        entries.append(
            {
                "label": f"В очереди #{task.get('id')} · {task.get('title')}",
                "details": [
                    f"Агент: {task.get('agent_type')} · Запуск: {task.get('run_after')}",
                ],
            }
        )
    return entries


def _existing_notification_artifact(
    notification: OwnerNotification,
) -> dict[str, str] | None:
    data = notification.data or {}
    path_value = str(data.get("document_path") or "")
    checksum = str(data.get("document_sha256") or "")
    if not path_value or len(checksum) != 64:
        return None
    root = Path(settings.document_storage_path).resolve()
    path = Path(path_value).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file() or path.suffix.lower() != ".pdf":
        return None
    raw = path.read_bytes()
    if (
        not raw.startswith(b"%PDF")
        or len(raw) > settings.max_attachment_bytes
        or hashlib.sha256(raw).hexdigest() != checksum
    ):
        return None
    return {
        "path": str(path),
        "filename": Path(str(data.get("document_filename") or path.name)).name,
        "sha256": checksum,
    }


def run_daily_owner_pack(
    db: Session,
    *,
    report_day: str | None = None,
    now: datetime | None = None,
    notify_owner: bool = True,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(ZoneInfo(settings.daily_owner_pack_timezone))
    day = date.fromisoformat(report_day) if report_day else local_now.date()

    public_total = int(
        db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == "lead",
                BusinessRecord.source == PUBLIC_LEAD_SOURCE,
            )
        )
        or 0
    )
    public_rows = db.scalars(
        select(BusinessRecord)
        .where(
            BusinessRecord.record_type == "lead",
            BusinessRecord.source == PUBLIC_LEAD_SOURCE,
        )
        .order_by(BusinessRecord.updated_at.desc(), BusinessRecord.id.desc())
        .limit(MAX_REPORT_ROWS)
    ).all()
    crm_total = int(
        db.scalar(
            select(func.count(BusinessRecord.id)).where(
                BusinessRecord.record_type == "lead",
                BusinessRecord.source != PUBLIC_LEAD_SOURCE,
            )
        )
        or 0
    )
    crm_rows = db.scalars(
        select(BusinessRecord)
        .where(
            BusinessRecord.record_type == "lead",
            BusinessRecord.source != PUBLIC_LEAD_SOURCE,
        )
        .order_by(BusinessRecord.updated_at.desc(), BusinessRecord.id.desc())
        .limit(MAX_REPORT_ROWS)
    ).all()
    operational = build_activity_report(db, period_hours=24)
    summary = operational.get("summary") or {}

    output_dir = Path(settings.document_storage_path) / "reports" / "daily-owner-pack" / day.isoformat()
    definitions: list[dict[str, Any]] = [
        {
            "kind": "public-prospects",
            "title": "Публичные потенциальные заказчики",
            "summary": [
                ("Найдено в PostgreSQL", public_total),
                ("Включено в PDF", len(public_rows)),
                ("Правило контактов", "Только публичные организационные контакты"),
            ],
            "entries": _public_lead_entries(public_rows),
        },
        {
            "kind": "crm-leads",
            "title": "CRM-лиды CleaningAIOS",
            "summary": [
                ("Лидов в PostgreSQL", crm_total),
                ("Включено в PDF", len(crm_rows)),
                ("Период актуальности", day.isoformat()),
            ],
            "entries": _crm_lead_entries(crm_rows),
        },
        {
            "kind": "operations",
            "title": "Ежедневный операционный отчёт CleaningAIOS",
            "summary": [
                ("Выполнено задач за 24 часа", summary.get("tasks_completed", 0)),
                ("В работе и очереди", summary.get("tasks_active", 0)),
                ("Ошибок", summary.get("tasks_failed", 0)),
                ("Заблокировано", summary.get("tasks_blocked", 0)),
                ("Улучшений в очереди", summary.get("queued_improvements", 0)),
                ("Ожидают решения владельца", summary.get("pending_approvals", 0)),
                ("Требуют внимания", "; ".join(operational.get("blockers") or []) or "нет"),
            ],
            "entries": _operation_entries(operational),
        },
    ]

    artifacts: list[dict[str, Any]] = []
    notifications: list[dict[str, Any]] = []
    for definition in definitions:
        kind = str(definition["kind"])
        idempotency_key = f"daily-owner-pack:{day.isoformat()}:{kind}:telegram"
        existing_notification = db.scalar(
            select(OwnerNotification).where(
                OwnerNotification.idempotency_key == idempotency_key
            )
        )
        existing_artifact = (
            _existing_notification_artifact(existing_notification)
            if existing_notification is not None
            else None
        )
        if existing_notification is not None and existing_artifact is not None:
            artifacts.append({"kind": kind, **existing_artifact})
            if notify_owner:
                notifications.append(
                    {
                        "id": existing_notification.id,
                        "kind": kind,
                        "status": existing_notification.status,
                    }
                )
            continue
        path = output_dir / f"{kind}-{day.isoformat()}.pdf"
        checksum = _build_pdf(
            path,
            title=str(definition["title"]),
            generated_at=local_now,
            report_day=day,
            summary_rows=list(definition["summary"]),
            entries=list(definition["entries"]),
        )
        artifact = {
            "kind": kind,
            "path": str(path),
            "filename": path.name,
            "sha256": checksum,
        }
        artifacts.append(artifact)
        if notify_owner:
            notification = queue_owner_notification(
                db,
                idempotency_key=idempotency_key,
                channel="telegram",
                resource_type="daily_owner_report",
                resource_id=f"{day.isoformat()}:{kind}",
                subject=str(definition["title"]),
                body=f"Ежедневный PDF за {day.isoformat()}. Данные сформированы из PostgreSQL.",
                data={
                    "report_kind": kind,
                    "report_day": day.isoformat(),
                    "document_path": str(path),
                    "document_filename": path.name,
                    "document_sha256": checksum,
                    "document_content_type": "application/pdf",
                },
            )
            if existing_notification is not None:
                notification.data = {
                    **(notification.data or {}),
                    "report_kind": kind,
                    "report_day": day.isoformat(),
                    "document_path": str(path),
                    "document_filename": path.name,
                    "document_sha256": checksum,
                    "document_content_type": "application/pdf",
                }
            notifications.append(
                {"id": notification.id, "kind": kind, "status": notification.status}
            )

    return {
        "status": "completed",
        "report_kind": "daily_owner_pdf_pack",
        "report_day": day.isoformat(),
        "generated_at": local_now.isoformat(),
        "artifacts": artifacts,
        "notifications": notifications,
        "notifications_created": len(notifications),
        "external_business_actions_executed": False,
        "evidence": [
            {
                "type": "daily_owner_pdf_pack",
                "report_day": day.isoformat(),
                "artifact_count": len(artifacts),
                "notification_count": len(notifications),
                "checksums": [artifact["sha256"] for artifact in artifacts],
            }
        ],
    }
