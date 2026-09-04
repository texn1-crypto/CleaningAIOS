from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import BusinessRecord


SERVICE_LABELS = {
    "mcd": "Уборка ЖК / МКД",
    "business_center": "Клининг бизнес-центра",
    "commercial": "Уборка коммерческого объекта",
    "general": "Генеральная уборка",
    "other": "Профессиональный клининг",
}


def _normal(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("ё", "е").split())


def _find_lead(db: Session, query: str) -> BusinessRecord:
    if not query.strip():
        raise ValueError("Не указано имя клиента. Добавьте в запрос: «для клиента Название».")
    rows = db.scalars(select(BusinessRecord).where(BusinessRecord.record_type == "lead").order_by(BusinessRecord.id)).all()
    needle = _normal(query)
    exact = [row for row in rows if needle in {_normal(row.title), _normal(row.data.get("name")), _normal(row.data.get("company"))}]
    if len(exact) == 1:
        return exact[0]
    partial = [
        row
        for row in rows
        if any(needle in value for value in (_normal(row.title), _normal(row.data.get("name")), _normal(row.data.get("company"))) if value)
    ]
    matches = exact or partial
    if not matches:
        raise ValueError(f"Клиент «{query}» не найден в CRM. Сначала создайте карточку лида с типом объекта и контактными данными.")
    if len(matches) > 1:
        raise ValueError(f"В CRM найдено несколько клиентов по запросу «{query}». Укажите точное название компании или имя.")
    return matches[0]


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
    raise RuntimeError("Не найден кириллический TTF-шрифт. Установите fonts-dejavu-core или задайте PROPOSAL_FONT_PATH.")


def _register_font() -> str:
    name = "CleaningAIProposal"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, str(_font_path())))
    return name


def _money(value: Any) -> str:
    try:
        return f"{float(value):,.0f} RUB".replace(",", " ")
    except (TypeError, ValueError):
        return "Уточняется после аудита объекта"


def _safe_filename(value: str) -> str:
    result = re.sub(r"[^A-Za-zА-Яа-я0-9_-]+", "-", value).strip("-").lower()
    return result[:60] or "client"


def _page(canvas, doc, font_name: str) -> None:
    canvas.saveState()
    canvas.setFillColor(colors.HexColor("#174f3c"))
    canvas.rect(0, A4[1] - 8 * mm, A4[0], 8 * mm, fill=1, stroke=0)
    canvas.setFont(font_name, 8)
    canvas.setFillColor(colors.HexColor("#657068"))
    canvas.drawString(18 * mm, 10 * mm, settings.company_name)
    canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Страница {doc.page}")
    canvas.restoreState()


def _build_pdf(path: Path, proposal_number: str, lead: BusinessRecord) -> None:
    font = _register_font()
    styles = getSampleStyleSheet()
    title = ParagraphStyle("TitleRU", parent=styles["Title"], fontName=font, fontSize=25, leading=30, textColor=colors.HexColor("#173f32"), alignment=TA_LEFT, spaceAfter=7 * mm)
    h2 = ParagraphStyle("H2RU", parent=styles["Heading2"], fontName=font, fontSize=14, leading=18, textColor=colors.HexColor("#173f32"), spaceBefore=5 * mm, spaceAfter=3 * mm)
    body = ParagraphStyle("BodyRU", parent=styles["BodyText"], fontName=font, fontSize=10, leading=15, textColor=colors.HexColor("#29332e"), spaceAfter=2.5 * mm)
    small = ParagraphStyle("SmallRU", parent=body, fontSize=8, leading=11, textColor=colors.HexColor("#657068"))
    badge = ParagraphStyle("BadgeRU", parent=small, fontSize=9, leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#ffffff"))
    data = lead.data or {}
    client = data.get("company") or data.get("name") or lead.title
    service = SERVICE_LABELS.get(str(data.get("service", "other")), "Профессиональный клининг")
    area = f"{float(data['object_area']):,.0f} м²".replace(",", " ") if data.get("object_area") else "уточняется"
    contact = data.get("name") or lead.title
    generated = datetime.now(timezone.utc).strftime("%d.%m.%Y")

    doc = SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm, topMargin=22 * mm, bottomMargin=18 * mm,
        title=f"Коммерческое предложение {proposal_number}", author=settings.company_legal_name or settings.company_name,
    )
    story = [
        Table([[Paragraph("ПРОЕКТ - ТРЕБУЕТ ПРОВЕРКИ ВЛАДЕЛЬЦА", badge)]], colWidths=[174 * mm], style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#174f3c")), ("BOX", (0, 0), (-1, -1), 0, colors.HexColor("#174f3c")), ("TOPPADDING", (0, 0), (-1, -1), 3 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm)])),
        Spacer(1, 8 * mm),
        Paragraph(escape(settings.company_name), small),
        Paragraph("Коммерческое предложение", title),
        Paragraph(f"Для: <b>{escape(str(client))}</b><br/>Контакт: {escape(str(contact))}<br/>Номер: {escape(proposal_number)} · Дата: {generated}", body),
        Spacer(1, 4 * mm),
        Paragraph("Предлагаемая услуга", h2),
        Paragraph(f"<b>{escape(service)}</b>", body),
        Table([
            [Paragraph("Тип объекта", small), Paragraph("Площадь", small), Paragraph("Ориентир бюджета клиента", small)],
            [Paragraph(escape(service), body), Paragraph(escape(area), body), Paragraph(escape(_money(data.get("budget"))), body)],
        ], colWidths=[70 * mm, 40 * mm, 64 * mm], style=TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf2ee")), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5cf")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm), ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm)])),
        Paragraph("Что входит в подготовку решения", h2),
        Paragraph("1. Аудит объекта и фиксация зон обслуживания.<br/>2. Расчёт состава смен, материалов и оборудования.<br/>3. Регламент работ и контрольные точки качества.<br/>4. Назначение ответственного и ведение обращений в едином контуре.<br/>5. Отчётность по выполнению и замечаниям.", body),
        Paragraph("Порядок запуска", h2),
        Paragraph("После осмотра объекта мы уточним объём, график, SLA и стоимость. Финальные условия фиксируются только в отдельном договоре после согласования владельцем.", body),
        KeepTogether([
            Paragraph("Важно", h2),
            Paragraph("Документ автоматически подготовлен по данным CRM. Он является проектом, не является публичной офертой, не подтверждает цену и не создаёт финансовых или договорных обязательств. Перед отправкой клиенту владелец должен проверить данные и одобрить финальную редакцию.", body),
        ]),
        Spacer(1, 6 * mm),
        Paragraph(f"{escape(settings.company_legal_name or settings.company_name)} · ИНН {escape(settings.company_inn or 'не указан')}<br/>{escape(settings.company_service_area)}<br/>{escape(settings.company_phone or settings.company_email or 'Контакт предоставляется после согласования')}", small),
    ]
    doc.build(story, onFirstPage=lambda canvas, d: _page(canvas, d, font), onLaterPages=lambda canvas, d: _page(canvas, d, font))


def generate_proposal(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    lead = _find_lead(db, str(payload.get("client_query", "")))
    proposal = BusinessRecord(record_type="proposal", title=f"КП для {lead.title}", status="draft", source="sales_agent", data={"lead_id": lead.id, "owner_approval_required_before_sending": True})
    db.add(proposal)
    db.flush()
    number = f"KP-{datetime.now(timezone.utc):%Y%m%d}-{proposal.id:05d}"
    directory = Path(settings.document_storage_path) / "proposals"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{number.lower()}-{_safe_filename(lead.title)}.pdf"
    _build_pdf(path, number, lead)
    os.chmod(path, 0o600)
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    proposal.status = "ready"
    proposal.external_id = number
    proposal.data = {**proposal.data, "filename": path.name, "storage_path": str(path), "checksum_sha256": checksum, "generated_at": datetime.now(timezone.utc).isoformat()}
    return {
        "status": "ready",
        "proposal_id": proposal.id,
        "proposal_number": number,
        "client_record_id": lead.id,
        "filename": path.name,
        "download_url": f"/api/proposals/{proposal.id}/download",
        "owner_approval_required_before_sending": True,
        "sent_to_client": False,
        "evidence": [{"type": "proposal_pdf", "record_id": proposal.id, "lead_id": lead.id, "checksum_sha256": checksum}],
    }
