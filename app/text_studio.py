from __future__ import annotations

import hashlib
import re
from typing import Any

from .chat import redact_sensitive_text


PROFESSIONAL_REPLACEMENTS = (
    (r"(?i)\bмы хотим предложить вам\b", "Предлагаем"),
    (r"(?i)\bнаша компания оказывает услуги по\b", "Мы выполняем"),
    (r"(?i)\bкачественно и быстро\b", "по согласованному регламенту и с контролем качества"),
    (r"(?i)\bесли вам интересно\b", "Если задача актуальна"),
    (r"(?i)\bочень выгодн(?:о|ые|ый|ая)\b", "с прозрачным расчётом стоимости"),
)


def _clean_sentence(value: str) -> str:
    value = re.sub(r"[ \t]+", " ", value).strip(" -")
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    value = re.sub(r"([!?.,])\1+", r"\1", value)
    if not value:
        return ""
    value = value[0].upper() + value[1:]
    if value[-1] not in ".!?":
        value += "."
    return value


def improve_referenced_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a safe, deterministic business-copy draft from replied Telegram text."""
    original = redact_sensitive_text(str(payload.get("referenced_text") or ""))[:4000].strip()
    if not original:
        raise ValueError("Referenced text is required")

    revised = original
    changes: list[str] = []
    for pattern, replacement in PROFESSIONAL_REPLACEMENTS:
        revised, count = re.subn(pattern, replacement, revised)
        if count:
            changes.append("Убраны разговорные и неподтверждённые формулировки.")
    paragraphs = []
    for block in re.split(r"\n{2,}", revised):
        sentences = [_clean_sentence(part) for part in re.split(r"(?<=[.!?])\s+|\n+", block)]
        cleaned = " ".join(sentence for sentence in sentences if sentence)
        if cleaned:
            paragraphs.append(cleaned)
    revised = "\n\n".join(paragraphs)
    if revised != original:
        changes.append("Исправлены пробелы, пунктуация и структура предложений.")
    if not re.search(r"(?i)\b(свяжитесь|ответьте|уточните|оставьте|напишите|позвоните|обсудим|подготовим расч[её]т)\b", revised):
        revised += "\n\nЕсли задача актуальна, уточните тип объекта, площадь и желаемый график — подготовим расчёт."
        changes.append("Добавлен конкретный и нейтральный следующий шаг.")

    digest = hashlib.sha256(revised.encode()).hexdigest()
    return {
        "status": "ready",
        "improved_text": revised,
        "changes": list(dict.fromkeys(changes)) or ["Текст нормализован без изменения подтверждённых фактов."],
        "draft_only": True,
        "external_send": False,
        "owner_review_required": True,
        "evidence": [
            {
                "type": "text_revision",
                "provider": "deterministic_copy_editor",
                "input_characters": len(original),
                "output_characters": len(revised),
                "checksum_sha256": digest,
            }
        ],
    }


def review_referenced_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Review a stored or replied text and return actionable feedback plus a safe draft."""
    original = redact_sensitive_text(str(payload.get("referenced_text") or ""))[:4000].strip()
    if not original:
        raise ValueError("Referenced text is required")

    revised = improve_referenced_text({"referenced_text": original})
    observations: list[str] = []
    if len(original) < 80:
        observations.append("Текст короткий: полезно добавить предмет предложения и ожидаемый следующий шаг.")
    if not re.search(r"(?i)\b(здравствуйте|добрый\s+(?:день|вечер)|уважаем)\b", original):
        observations.append("Нет персонального делового приветствия.")
    if re.search(r"(?i)\b(качественно|быстро|лучший|выгодн)\w*\b", original):
        observations.append("Есть общие обещания без проверяемого критерия или регламента.")
    if not re.search(r"(?i)\b(ответьте|уточните|напишите|позвоните|обсудим|подготовим|свяжитесь)\b", original):
        observations.append("Не указан конкретный следующий шаг для получателя.")
    if not observations:
        observations.append("Структура понятна; редактура сделала формулировки короче и деловее.")

    feedback = "\n".join(f"• {item}" for item in observations)
    digest = hashlib.sha256((original + "\n" + feedback).encode()).hexdigest()
    return {
        "status": "ready",
        "feedback_text": feedback,
        "observations": observations,
        "revised_text": revised["improved_text"],
        "changes": revised["changes"],
        "draft_only": True,
        "external_send": False,
        "owner_review_required": True,
        "evidence": [
            {
                "type": "text_review",
                "provider": "deterministic_copy_editor",
                "input_characters": len(original),
                "checksum_sha256": digest,
            }
        ],
    }
