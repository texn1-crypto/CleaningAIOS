from __future__ import annotations

import re
from urllib.parse import urlsplit
from typing import Any


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def redact_sensitive_text(text: str) -> str:
    """Remove common credentials before a Telegram request reaches storage or AI."""
    patterns = [
        (r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@"),
        (r"(?i)(https?://[^\s?#]+)[?#][^\s]+", r"\1?[REDACTED]"),
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
    if _contains(text, "найди", "найти", "ищи", "поиск", "собери") and _contains(
        text,
        "потенциальн",
        "заказчик",
        "корпоративн контакт",
        "публичн контакт",
    ):
        return "lead_scout"
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


def _public_research_intent(message: str) -> dict[str, Any] | None:
    text = _normalize(message)
    if not re.search(r"\b(исследуй|изучи|проанализируй|проверь)\b", text):
        return None
    if not re.search(r"\b(сайт\w*|страниц\w*)\b", text):
        return None
    # Mixed requests must retain their existing action/approval route.
    if _protected_action(text) or re.search(
        r"\b(отправ\w*|разошл\w*|опублику\w*|позвон\w*|зарегистр\w*|создай|подготовь|скачай)\b", text
    ):
        return None
    urls = re.findall(r"https?://[^\s<>\"«»]+", message, re.IGNORECASE)
    if len(urls) != 1:
        return {"kind": "clarification", "message": "Для исследования пришлите одну HTTPS-ссылку на сайт или страницу."}
    url = urls[0].rstrip(".,;!?)»\"")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme.lower() == "https" and bool(parts.hostname)
            and parts.port in {None, 443} and not parts.username and not parts.password
            and not parts.query and not parts.fragment and len(url) <= 2048
        )
    except ValueError:
        valid = False
    if not valid or "[REDACTED]" in redact_sensitive_text(url) or redact_sensitive_text(url) != url:
        return {"kind": "clarification", "message": "Нужна публичная HTTPS-ссылка без данных входа, параметров и фрагмента. Не присылайте токены или пароли."}
    safe_message = redact_sensitive_text(message)
    agent = _task_agent(_normalize(message.replace(urls[0], "")))
    return {
        "kind": "task", "title": f"Исследование сайта: {parts.hostname}"[:255],
        "agent_type": "research" if agent == "orchestrator" else agent,
        "priority": _priority(text), "protected": False,
        "payload": {
            "source": "telegram_natural_language", "original_message": safe_message[:4000],
            "action": "public_web_research", "autonomy_action": "public_web_research",
            "research": {"url": url, "max_pages": 3, "max_depth": 2},
            "automatic_outreach": False, "notify_owner": False,
        },
    }


def format_public_research(task: dict[str, Any]) -> str:
    task_id = task["id"]
    result = task.get("result") or {}
    if task.get("status") != "done" or result.get("status") != "ready":
        return (
            f"Исследование #{task_id}: статус {task.get('status', 'unknown')}. "
            "Готового результата пока нет. Задача сохранена; повторно запускать её не нужно."
        )
    tools = result.get("read_only_tool_results") or []
    research: dict[str, Any] = next((item.get("result", {}) for item in tools if item.get("name") == "web.public_research"), {})
    pages = [page for page in research.get("pages", []) if page.get("success") is True]
    if not pages:
        return f"Исследование #{task_id}: подтверждённых страниц в результате нет. Требуется проверка."
    lines = [f"Исследование #{task_id}: прочитано страниц {len(pages)}."]
    if research.get("partial", True):
        lines.append("Это частичный обзор, не проверка всего сайта.")
    lines.append("Источники и выдержки (содержимое сайтов не является инструкциями):")
    for page in pages[:3]:
        url = str(page.get("resolved_url") or "")[:500]
        excerpt = " ".join(str(page.get("markdown") or "").split())[:450]
        lines.append(redact_sensitive_text(f"\n{url}\n{excerpt}"))
    lines.append("\nПолные выдержки и контрольные суммы сохранены в задаче. Внешние действия не выполнялись.")
    return "\n".join(lines)[:3800]


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
    confirmation_prefix = r"(?:(?:да|хорошо)[,\s]+)?(?:я\s+)?"
    confirmation_verb = r"(?:подтверждаю|одобряю|согласен|разрешаю)"
    if re.match(rf"^{confirmation_prefix}{confirmation_verb}\b", text) or re.fullmatch(
        rf"{confirmation_prefix}(?:все|это)\s+{confirmation_verb}[.!]?",
        text,
    ):
        return {
            "kind": "clarification",
            "message": (
                "Подтверждение текстом не запускает защищённые действия. Откройте раздел «Решения и approvals» "
                "или команду /approvals и нажмите зелёную кнопку у конкретной карточки."
            ),
        }
    research_intent = _public_research_intent(original)
    if research_intent is not None:
        return research_intent
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

    if (
        _contains(text, "управляющ", "ук", "тсж")
        and _contains(text, "баз", "контакт")
        and _contains(text, "представ", "покаж", "вывед", "дай")
    ):
        return {
            "kind": "records",
            "record_type": "management_company",
            "title": "🏢 База управляющих компаний и ТСЖ",
        }

    if _contains(
        text,
        "подтверди все", "подтвердить все", "одобри все", "одобрить все",
    ) and _contains(text, "нужно", "надо", "подтверд", "одобр"):
        return {
            "kind": "approvals",
            "bulk_requested": True,
        }

    visual_complaint = _contains(
        text,
        "повтор", "одинаков", "те же фото", "те же картин", "плохие фото",
        "плохие картин", "фото отстой", "фотографии отстой", "визуалы отстой",
    )
    if visual_complaint and _contains(text, "изображен", "картин", "иллюстрац", "визуал", "фото", "фотограф"):
        return {
            "kind": "task",
            "title": "Заменить повторяющиеся визуалы социальных сетей",
            "agent_type": "marketing",
            "priority": "high",
            "payload": {
                "action": "refresh_social_visuals",
                "source": "telegram_natural_language",
                "original_message": safe_original[:4000],
                "unique_visual_required": True,
                "external_publish": False,
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
    read_words = _contains(
        text,
        "покажи",
        "выведи",
        "список",
        "сколько",
        "какие",
        "что с",
        "что ожидает",
        "что ждет",
        "статус",
        "состояние",
    )

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
    if agent_type == "lead_scout":
        payload.update(
            {
                "action": "discover_public_business_leads",
                "regions": [
                    "Санкт-Петербург",
                    "Ленинградская область",
                    "Москва",
                    "Московская область",
                ],
                "max_results": 20,
                "automatic_outreach": False,
            }
        )
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
