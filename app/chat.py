from __future__ import annotations

import re
from typing import Any


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def redact_sensitive_text(text: str) -> str:
    """Remove common credentials before a Telegram request reaches storage or AI."""
    patterns = [
        (
            r"(?i)(https://api\.telegram\.org/bot)[^/\s\"']+",
            r"\1[TELEGRAM_TOKEN_REDACTED]",
        ),
        (r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{20,}", "[TELEGRAM_TOKEN_REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{16,}\b", "[API_KEY_REDACTED]"),
        (r"(?i)\b(api[_ -]?key|token|пароль|password)\s*[:=]\s*\S+", r"\1=[REDACTED]"),
    ]
    safe = text
    for pattern, replacement in patterns:
        safe = re.sub(pattern, replacement, safe)
    return safe


def _task_agent(text: str) -> str:
    if _contains(text, "тендер", "закупк", "конкурс"):
        if _contains(text, "найди", "найти", "ищи", "поиск", "собери", "монитор"):
            return "research"
        return "tender"
    if _contains(text, "лид", "клиент", "продаж", "crm", "коммерческ", "follow-up", "фоллоу"):
        return "sales"
    if _contains(text, "кандидат", "сотрудник", "уборщик", "вакан", "смен", "персонал", "кадр", "увол", "наня"):
        return "hr"
    if _contains(text, "финанс", "платеж", "оплат", "счет", "деньг", "марж", "прибыл", "расход", "бюджет"):
        return "finance"
    if _contains(
        text,
        "маркет", "реклам", "контент", "публикац", "пост", "smm", "рассыл",
        "соцсет", "социальн", "вконтакт", "одноклас", " вк ", "vk",
    ):
        return "marketing"
    if _contains(text, "исслед", "собери дан", "найди информац", "проведи поиск"):
        return "research"
    if _contains(text, "мета", "meta brain", "качество агент", "улучшить агент"):
        return "meta_brain"
    if _contains(text, "директор", "ceo", "состояние бизнеса", "проанализируй бизнес", "стратег"):
        return "ceo"
    return "orchestrator"


def _protected_action(text: str) -> str | None:
    if re.search(r"\b(оплати|оплатить|переведи|перевести|заплати|заплатить|спиши|списать)\b", text):
        return "financial"
    if re.search(r"\b(подпиши|подписать|заключи|заключить)\b.*\b(договор|контракт|соглашение)\b", text):
        return "contract"
    if re.search(r"\b(подай|подать|отправь|отправить)\b.*\b(заявк|предложен)\w*", text) and _contains(text, "тендер", "закупк", "конкурс"):
        return "tender_submission"
    if re.search(r"\b(найми|нанять|прими|принять|уволь|уволить)\w*\b", text) and _contains(text, "работ", "сотрудник", "кандидат", "уборщик", "персонал"):
        return "hr_final"
    if _contains(text, "массовая рассылка", "массовую рассылку") or re.search(r"\b(разошли|разослать|отправь|отправить)\b.*\b(всем|базе|клиентам|адресам)\b", text):
        return "bulk_outreach"
    if re.search(r"\b(подай|подать)\b.*\b(иск|жалоб|претензи)\w*", text):
        return "legal"
    return None


def _priority(text: str) -> str:
    if _contains(text, "критично", "немедленно", "прямо сейчас"):
        return "critical"
    if _contains(text, "срочно", "важно", "приоритет"):
        return "high"
    if _contains(text, "не срочно", "когда будет время", "низкий приоритет"):
        return "low"
    return "normal"


def _proposal_client_query(message: str) -> str:
    match = re.search(
        r"(?:\bдля\b|\bклиент(?:у|а|ом)?\b)\s+(?:тестов\w+\s+)?(?:клиент\w*\s+)?(.+)$",
        message,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip(" .,:;\"")[:255] if match else ""


def _referenced_task_id(text: str) -> int | None:
    match = re.search(r"(?:#|№|(?:задач(?:а|и|у|е)?|номер)\s*)(\d+)\b", text)
    return int(match.group(1)) if match else None


def understand_russian_message(message: str, *, referenced_text: str = "") -> dict[str, Any]:
    """Map a Russian free-form Telegram message to a safe application intent.

    The parser handles common read requests locally and turns other business
    instructions into auditable tasks. It never bypasses protected-action policy.
    """
    original = " ".join(message.split()).strip()
    safe_original = redact_sensitive_text(original)
    text = _normalize(original)
    if not text:
        return {"kind": "help"}

    if text in {"привет", "здравствуй", "здравствуйте", "добрый день", "добрый вечер", "доброе утро"}:
        return {"kind": "greeting"}
    if text in {"меню", "главное меню", "покажи меню", "открой меню", "меню бота", "меню системы"}:
        return {"kind": "menu"}
    if _contains(text, "что ты умеешь", "что умеешь", "как пользоваться", "помощь", "помоги"):
        return {"kind": "help"}
    if text in {"спасибо", "благодарю", "понял", "понятно", "хорошо", "ок", "готово"}:
        return {"kind": "acknowledgement"}
    if re.fullmatch(r"(?:улучши|доработай|перепиши|отредактируй)(?:\s+(?:это|текст|сообщение))?", text):
        safe_reference = redact_sensitive_text(" ".join(referenced_text.split()).strip())[:4000]
        if not safe_reference:
            return {
                "kind": "clarification",
                "message": (
                    "Ответьте фразой «улучши это» именно на сообщение с исходным текстом. "
                    "Тогда текстовый агент обработает его и вернёт новый черновик."
                ),
            }
        return {
            "kind": "task",
            "title": f"Улучшить текст: {safe_reference[:220]}",
            "agent_type": "copywriter",
            "priority": "normal",
            "payload": {
                "action": "improve_referenced_text",
                "source": "telegram_natural_language",
                "original_message": safe_original[:4000],
                "referenced_text": safe_reference,
                "draft_only": True,
                "external_send": False,
            },
            "protected": False,
        }
    if (
        "обратн" in text
        and "связ" in text
        and _contains(text, "предыдущ", "письм", "сообщен", "текст")
    ):
        safe_reference = redact_sensitive_text(" ".join(referenced_text.split()).strip())[:4000]
        return {
            "kind": "task",
            "title": "Подготовить обратную связь по предыдущему тексту",
            "agent_type": "copywriter",
            "priority": "normal",
            "payload": {
                "action": "review_previous_text",
                "source": "telegram_natural_language",
                "original_message": safe_original[:4000],
                "referenced_text": safe_reference,
                "draft_only": True,
                "external_send": False,
            },
            "protected": False,
        }
    if (
        "отчет" in text
        and _contains(text, "проделан", "сделано", "выполнен", "работ")
    ) or _contains(text, "что было сделано", "что уже сделано", "результаты работы системы") or (
        "работ" in text
        and _contains(text, "проделан", "сделан", "выполнен")
        and _contains(text, "скажи", "покажи", "какая", "какую", "что")
    ):
        return {"kind": "activity_report", "period_hours": 24}
    if (
        (_contains(text, "системн", "сисадмин") and _contains(text, "администратор", "отчет", "ошиб", "сбой"))
        or _contains(
            text,
            "что сломалось",
            "ошибки бота",
            "сбои бота",
            "динамика реконструкции",
            "что не получилось у бота",
        )
    ):
        return {"kind": "system_admin_report"}
    if _contains(
        text,
        "сколько нужно времени",
        "сколько времени",
        "сколько займет",
        "когда будет готов",
        "когда закончится",
        "срок выполнения",
        "оценка времени",
    ) and _contains(text, "задач", "поручен", "работ"):
        return {"kind": "task_eta", "task_id": _referenced_task_id(text)}
    if _contains(text, "весь функционал", "все функции", "полную проверку") and _contains(
        text, "бот", "систем"
    ):
        return {"kind": "system_self_check"}
    if _contains(
        text,
        "запусти все возможные процессы",
        "запусти все доступные процессы",
        "делай все возможное без меня",
        "сделай все возможное без меня",
        "выполни все что можешь без меня",
    ):
        return {
            "kind": "task",
            "title": "Запустить все безопасные процессы CleaningAIOS",
            "agent_type": "orchestrator",
            "priority": "high",
            "payload": {
                "action": "run_safe_operations_cycle",
                "source": "telegram_natural_language",
                "original_message": safe_original[:4000],
                "protected_scope": "excluded",
            },
            "protected": False,
        }

    image_action = _contains(
        text,
        "создай", "сделай", "сгенерируй", "нарисуй", "подготовь",
    ) and _contains(text, "изображен", "картин", "иллюстрац", "визуал", "фото")
    if image_action:
        return {
            "kind": "task",
            "title": safe_original[:255],
            "agent_type": "marketing",
            "priority": _priority(text),
            "payload": {
                "action": "generate_image",
                "source": "telegram_natural_language",
                "original_message": safe_original[:4000],
                "prompt": safe_original[:4000],
                "external_publish": False,
            },
            "protected": False,
        }

    action_words = _contains(
        text,
        "создай", "поставь", "добавь", "запусти", "проведи", "проанализируй",
        "найди", "собери", "подготовь", "сделай", "оплати", "переведи", "подпиши",
        "подай", "отправь", "найми", "уволь", "разошли", "начни", "начните",
        "оформи", "оформляй", "оформлять", "настрой", "зарегистрируй",
    )
    read_words = _contains(text, "покажи", "выведи", "список", "сколько", "какие", "что с", "статус", "состояние")

    if not action_words and (_contains(text, "как дела", "здоровье бизнеса") or read_words and _contains(text, "бизнес", "систем", "компани", "общий")):
        return {"kind": "dashboard"}
    if not action_words and read_words and _contains(text, "задач", "поручен"):
        return {"kind": "tasks"}
    if not action_words and read_words and _contains(text, "подтвержден", "согласован", "одобрени", "approval"):
        return {"kind": "approvals"}
    if not action_words and read_words and _contains(text, "решени"):
        return {"kind": "decisions"}
    if not action_words and read_words and _contains(text, "лид", "клиент", "продаж", "crm"):
        return {"kind": "records", "record_type": "lead", "title": "👥 CRM и продажи"}
    if not action_words and read_words and _contains(text, "тендер", "закупк", "конкурс"):
        return {"kind": "records", "record_type": "tender", "title": "🏗 Тендеры"}
    if not action_words and read_words and _contains(text, "кандидат", "кадр", "резерв", "персонал", "вакан"):
        return {"kind": "records", "record_type": "candidate", "title": "🧹 Кандидаты и HR"}
    if not action_words and read_words and _contains(text, "финанс", "платеж", "расход", "прибыл", "деньг"):
        return {"kind": "summary", "module": "finance", "title": "💰 Финансы"}
    if not action_words and read_words and _contains(text, "маркет", "реклам", "контент", "smm"):
        if _contains(text, "счет", "счета", "инвойс"):
            return {"kind": "records", "record_type": "marketing_invoice", "title": "🧾 Счета рекламы"}
        return {"kind": "summary", "module": "marketing", "title": "📊 Маркетинг"}
    if not action_words and read_words and _contains(text, "агент"):
        return {"kind": "dashboard"}
    if not action_words and read_words and _contains(text, "входящ", "inbox", "сообщени"):
        return {"kind": "inbox"}
    if not action_words and _contains(text, "рассыл", "email-кампан", "email кампан") and (
        read_words or text in {"рассылки", "рассылка", "email-кампании", "email кампании"}
    ):
        return {"kind": "outreach"}
    if not action_words and (read_words or _contains(text, "что бот не умеет", "чего не хватает")) and _contains(text, "улучш", "не уме", "не хватает", "доработ"):
        return {"kind": "improvements"}

    action_kind = _protected_action(text)
    agent_type = _task_agent(text)
    payload: dict[str, Any] = {
        "source": "telegram_natural_language",
        "original_message": safe_original[:4000],
    }
    if action_kind:
        payload["action_kind"] = action_kind
    if agent_type == "research" and _contains(text, "тендер", "закупк", "конкурс"):
        payload.update({"collection": "tenders", "query": safe_original[:1000]})
    if agent_type == "sales" and ("коммерческ" in text or re.search(r"\bкп\b", text)) and "предлож" in text:
        payload.update({"action": "generate_proposal", "client_query": _proposal_client_query(safe_original)})
    if agent_type == "marketing" and _contains(
        text,
        "оформ", "настрой", "зарегистр", "создай", "начни", "начните",
    ) and _contains(text, "соцсет", "социальн", "вконтакт", "одноклас", " вк ", "vk"):
        channels: list[str] = []
        if re.search(r"(?:^|\s)(?:вк|vk)(?:\s|$)", text) or "вконтакт" in text:
            channels.append("vk")
        if "одноклас" in text:
            channels.append("odnoklassniki")
        if "телеграм" in text or "telegram" in text:
            channels.append("telegram")
        if "инстаграм" in text or "instagram" in text:
            channels.append("instagram")
        payload.update({"action": "prepare_social_account_setup", "channels": channels or ["vk", "odnoklassniki"]})
    return {
        "kind": "task",
        "title": safe_original[:255],
        "agent_type": agent_type,
        "priority": _priority(text),
        "payload": payload,
        "protected": bool(action_kind),
    }
