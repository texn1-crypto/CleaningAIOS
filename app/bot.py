from __future__ import annotations

import asyncio
import base64
from difflib import SequenceMatcher
import hashlib
import io
import logging
import re
import secrets
import tempfile
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from urllib.parse import unquote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .chat import understand_russian_message
from .config import settings
from .recipient_import import EMAIL_PATTERN, SUPPORTED_RECIPIENT_SUFFIXES, extract_recipient_emails

logging.basicConfig(level=logging.INFO)
# httpx logs full request URLs at INFO. Telegram embeds the bot token in those
# URLs, so INFO-level HTTP logs would leak a production credential.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
BASE = (settings.internal_api_url or settings.public_base_url or "http://web:8000").rstrip("/")
HEADERS = {"X-API-Key": settings.api_key, "X-Actor": "telegram-owner", "X-Role": "owner"}
MAILING_MAX_RECIPIENTS = 1000
MAILING_BATCH_SIZE = 100
_telegram_identity: ContextVar[dict | None] = ContextVar(
    "telegram_identity", default=None
)


def allowed(update: Update) -> bool:
    if _telegram_identity.get() is not None:
        return True
    user_id = update.effective_user.id if update.effective_user else 0
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", user_id)
    owner_chat_id = settings.owner_telegram_chat_id or settings.owner_telegram_id
    return (
        bool(settings.owner_telegram_id)
        and str(user_id) == str(settings.owner_telegram_id)
        and str(chat_id) == str(owner_chat_id)
    )


async def api(method: str, path: str, **kwargs):
    if method.upper() == "POST" and path == "/api/tasks" and isinstance(kwargs.get("json"), dict):
        identity = _telegram_identity.get()
        if identity and not kwargs["json"].get("assigned_to"):
            kwargs = {
                **kwargs,
                "json": {**kwargs["json"], "assigned_to": identity["subject"]},
            }
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        response = await client.request(method, f"{BASE}{path}", **kwargs)
        response.raise_for_status()
        return response.json()


def _identity_payload(update: Update) -> dict[str, int]:
    user = update.effective_user
    chat = getattr(update, "effective_chat", None)
    if user is None or chat is None:
        raise RuntimeError("Telegram update has no user/chat identity")
    return {"user_id": int(user.id), "chat_id": int(chat.id)}


async def _authorize_update(update: Update, minimum_role: str) -> dict | None:
    try:
        result = await api(
            "POST",
            "/api/telegram/control/authorize",
            json={**_identity_payload(update), "minimum_role": minimum_role},
        )
    except (httpx.HTTPError, RuntimeError):
        await update.effective_message.reply_text(
            "Не удалось проверить права доступа. Действие не выполнено."
        )
        return None
    if not result.get("authorized"):
        await update.effective_message.reply_text("Доступ не разрешён.")
        return None
    return result


def _secured(handler, minimum_role: str):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        identity = await _authorize_update(update, minimum_role)
        if identity is None:
            return None
        token = _telegram_identity.set(identity)
        try:
            return await handler(update, context)
        finally:
            _telegram_identity.reset(token)

    return wrapped


async def _approval_markup(update: Update, approval_id: int) -> InlineKeyboardMarkup:
    card = await api(
        "POST",
        f"/api/telegram/control/approvals/{approval_id}/card",
        json={**_identity_payload(update), "minimum_role": "owner"},
    )
    callbacks = card.get("callbacks") or {}
    if set(callbacks) != {"approve", "reject", "request_changes"}:
        raise RuntimeError("Approval callback tokens are unavailable")
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=callbacks["approve"]),
            InlineKeyboardButton("❌ Отклонить", callback_data=callbacks["reject"]),
        ],
        [
            InlineKeyboardButton(
                "✏️ Запросить изменения",
                callback_data=callbacks["request_changes"],
            )
        ],
    ])


async def api_file(path: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
        response = await client.get(f"{BASE}{path}")
        response.raise_for_status()
        disposition = response.headers.get("content-disposition", "")
        encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
        plain = re.search(r'filename="?([^";]+)', disposition, flags=re.IGNORECASE)
        filename = unquote(encoded.group(1)) if encoded else (plain.group(1) if plain else "commercial-proposal.pdf")
        return response.content, filename


def _safe_document_filename(filename: str) -> str:
    name = Path(filename or "proposal.docx").name
    stem = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "-", Path(name).stem).strip(" .-_")[:120] or "proposal"
    return f"{stem}{Path(name).suffix.lower()}"


async def _create_revision_task(update: Update, *, source_path: str = "", filename: str, caption: str, credentials_required: bool = False) -> dict:
    payload = {
        "action": "revise_proposal",
        "source": "telegram_document",
        "source_path": source_path,
        "source_filename": filename,
        "request_text": caption[:4000],
        "original_message": caption[:4000],
    }
    if credentials_required:
        payload["document_status"] = "credentials_required"
    await api("POST", "/api/request-analysis", json={
        "message": caption,
        "intent": {
            "kind": "task",
            "title": caption[:255] or f"Обновить коммерческое предложение {filename}",
            "agent_type": "orchestrator",
            "priority": "high",
            "payload": payload,
            "protected": False,
        },
        "source_channel": "telegram",
        "source_user": str(update.effective_user.id if update.effective_user else "owner"),
    })
    task = await api("POST", "/api/tasks", json={
        "title": f"Профессионально обновить коммерческое предложение: {filename}"[:255],
        "agent_type": "orchestrator",
        "priority": "high",
        "payload": payload,
        "max_attempts": 1,
    })
    return await api("POST", f"/api/tasks/{task['id']}/run")


async def proposal_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Доступ не разрешён.")
        return
    if await mailing_document(update, context):
        return
    message = update.effective_message
    document = message.document
    filename = _safe_document_filename(document.file_name or "proposal.docx")
    suffix = Path(filename).suffix.lower()
    caption = " ".join((message.caption or "").split())
    normalized = caption.lower().replace("ё", "е")
    is_outreach_request = any(token in normalized for token in ("рассыл", "разошл", "разосл", "отправь базе", "разослать"))
    if is_outreach_request:
        if suffix not in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".odt"}:
            await message.reply_text("Для рассылки можно приложить PDF, Word, Excel или ODT-документ.")
            return
        if int(document.file_size or 0) > settings.max_attachment_bytes:
            await message.reply_text(f"Вложение больше безопасного лимита {settings.max_attachment_bytes // 1_000_000} МБ. Сожмите файл и пришлите снова.")
            return
        try:
            directory = Path(settings.document_storage_path) / "telegram-inbox"
            directory.mkdir(parents=True, exist_ok=True)
            stored = directory / f"{secrets.token_hex(10)}-{filename}"
            telegram_file = await context.bot.get_file(document.file_id)
            await telegram_file.download_to_drive(custom_path=str(stored))
            raw = stored.read_bytes()
            if not raw or len(raw) > settings.max_attachment_bytes:
                raise RuntimeError("Файл пуст или превышает лимит")
            draft = await api("POST", "/api/outreach/campaigns/management-companies/draft", json={
                "filename": filename,
                "content_type": document.mime_type or "application/octet-stream",
                "content_base64": base64.b64encode(raw).decode(),
            })
            if draft.get("status") == "no_verified_recipients":
                await message.reply_text(
                    "Документ получен, но рассылка не создана: в базе пока нет адресов УК с зафиксированным согласием на рекламную рассылку. "
                    "Публичный email с сайта сам по себе не считается согласием."
                )
                return
            approval_id = draft.get("approval_id")
            keyboard = await _approval_markup(update, int(approval_id))
            await message.reply_text(
                f"Черновик рассылки создан как задача #{draft['task_id']}.\n"
                f"Получателей с подтверждённым согласием: {draft['recipient_count']}.\n"
                f"Тема: {draft['subject']}\nТекст: {draft['body']}\n\n"
                "До вашего нажатия ни одно письмо не будет поставлено в очередь.",
                reply_markup=keyboard,
            )
            return
        except (httpx.HTTPError, TelegramError, OSError, RuntimeError) as exc:
            await message.reply_text(f"Документ получен, но черновик рассылки не создан: {type(exc).__name__}: {str(exc)[:300]}.")
            return
    if suffix not in {".docx", ".pdf"}:
        await message.reply_text("Файл получен, но редактор КП принимает DOCX или PDF. Пришлите документ в одном из этих форматов.")
        return
    is_proposal_request = bool(
        re.search(r"\bкп\b", normalized)
        or "коммерческ" in normalized
        or ("предложен" in normalized and any(word in normalized for word in ("красив", "профессион", "обнов", "измен", "редакт")))
    )
    if not is_proposal_request:
        await message.reply_text(
            "Файл получен и не проигнорирован. Чтобы запустить редакцию, подпишите его, например: «Сделай это коммерческое предложение профессиональнее и представь мне на утверждение»."
        )
        return
    file_size = int(document.file_size or 0)
    if file_size > settings.max_document_bytes:
        await message.reply_text(
            f"Файл получен, но его размер превышает безопасный лимит {settings.max_document_bytes // 1_000_000} МБ. "
            "Уменьшите размер DOCX/PDF и пришлите повторно."
        )
        return
    try:
        if file_size > settings.telegram_cloud_download_limit_bytes and not settings.telegram_bot_api_base_url:
            completed = await _create_revision_task(
                update,
                filename=filename,
                caption=caption,
                credentials_required=True,
            )
            result = completed.get("result") or {}
            await message.reply_text(
                f"Файл и запрос зарегистрированы как задача #{completed['id']}, но Telegram Cloud Bot API не даёт скачать документ больше 20 МБ. "
                f"Создано улучшение #{result.get('improvement_id', '—')}; CEO получил отчёт #{result.get('ceo_incident_task_id', '—')}. "
                "Нужно настроить локальный Telegram Bot API server и TELEGRAM_BOT_API_BASE_URL — после этого пришлите файл повторно."
            )
            return
        directory = Path(settings.document_storage_path) / "telegram-inbox"
        directory.mkdir(parents=True, exist_ok=True)
        stored = directory / f"{secrets.token_hex(10)}-{filename}"
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=str(stored))
        if not stored.is_file() or not stored.stat().st_size:
            raise RuntimeError("Telegram вернул пустой документ")
        if stored.stat().st_size > settings.max_document_bytes:
            stored.unlink(missing_ok=True)
            raise RuntimeError("Скачанный документ превышает безопасный лимит")
        stored.chmod(0o600)
        completed = await _create_revision_task(update, source_path=str(stored), filename=filename, caption=caption)
        result = completed.get("result") or {}
        if completed.get("status") != "done" or result.get("status") != "ready_for_owner_review":
            await message.reply_text(
                f"Редакция не завершена. Задача #{completed['id']}: {completed.get('status')}. "
                f"Улучшение: #{result.get('improvement_id', '—')}; отчёт CEO: #{result.get('ceo_incident_task_id', '—')}. "
                f"Причина: {result.get('execution_gap') or result.get('error') or result.get('reason') or 'неизвестна'}."
            )
            return
        urls = result.get("download_urls") or {}
        approval_id = result.get("approval_id")
        keyboard = await _approval_markup(update, int(approval_id)) if approval_id else None
        for kind in ("docx", "pdf"):
            if not urls.get(kind):
                continue
            content, output_name = await api_file(urls[kind])
            stream = io.BytesIO(content)
            stream.name = output_name
            await message.reply_document(
                document=stream,
                filename=output_name,
                caption=(
                    f"Обновлённое КП {result['proposal_number']} · {kind.upper()}. "
                    "Текст подготовил Copywriter Agent, оформление — Creative Agent. Клиенту ничего не отправлено. "
                    "Проверьте факты и условия; утверждение фиксирует только ваше решение по проекту."
                ),
                reply_markup=keyboard if kind == "pdf" else None,
            )
    except (httpx.HTTPError, TelegramError, OSError, RuntimeError) as exc:
        await message.reply_text(f"Файл получен, но обработка остановилась: {type(exc).__name__}: {str(exc)[:300]}. Запрос не считается выполненным.")


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Доступ не разрешён."); return
    rows = [["🏢 Mission Control", "dashboard"], ["🤖 AI CEO", "ceo"], ["🧠 E-агенты", "agents"], ["✅ Решения и approvals", "approvals"], ["👥 CRM и продажи", "crm"], ["🏗 Тендеры", "tenders"], ["🧹 Кандидаты и HR", "hr"], ["💰 Финансы", "finance"], ["📊 Маркетинг", "marketing"], ["🧾 Счета рекламы", "marketing_invoices"], ["🧪 Симулятор", "simulator"], ["🧾 Задачи", "tasks"], ["🧬 Meta Brain", "meta_brain"], ["🛠 Улучшения", "improvements"], ["📣 Рассылки", "outreach"]]
    keyboard = [[InlineKeyboardButton(label, callback_data=key)] for label, key in rows]
    await update.effective_message.reply_text("CleaningAI OS · выберите раздел:", reply_markup=InlineKeyboardMarkup(keyboard))


async def dashboard(update: Update, _: ContextTypes.DEFAULT_TYPE):
    data = await api("GET", "/api/dashboard")
    await update.effective_message.reply_text(f"🏢 Здоровье: {data['company_health']}%\n🧾 Открытые задачи: {data['open_tasks']}\n✅ Решения: {data['pending_decisions']}\n🔐 Подтверждения: {data['pending_approvals']}\n⚠️ Ошибки: {data['failed_tasks']}")


def format_ceo_brief(data: dict) -> str:
    facts = data.get("facts") or {}
    task_facts = facts.get("tasks") or {}
    approval_facts = facts.get("approvals") or {}
    alert_facts = facts.get("critical_alerts") or {}
    finance = facts.get("finance") or {}
    recommendations = data.get("recommendations") or []
    lines = [
        "🤖 AI CEO · Brief",
        f"Актуально на: {data.get('generated_at')}",
        "",
        "ФАКТЫ ИЗ БД",
        (
            f"• Задачи: active {task_facts.get('active', 0)}, "
            f"failed {task_facts.get('failed', 0)}, blocked {task_facts.get('blocked', 0)}"
        ),
        f"  source task IDs: {(task_facts.get('failed_ids') or []) + (task_facts.get('blocked_ids') or [])}",
        f"• Ожидают owner approval: {approval_facts.get('pending', 0)} · IDs {approval_facts.get('ids') or []}",
        (
            f"• Неподтверждённые alerts: {alert_facts.get('unacknowledged', 0)} "
            f"· dead-letter {alert_facts.get('dead_letter', 0)} · IDs {alert_facts.get('ids') or []}"
        ),
        (
            f"• Просроченные платежи: {finance.get('overdue_payments', 0)} "
            f"на {finance.get('overdue_amount', 0)} ₽ · IDs {finance.get('payment_ids') or []}"
        ),
        "",
        "РЕКОМЕНДАЦИИ (НЕ ВЫПОЛНЕНЫ)",
    ]
    lines.extend(
        f"• [{item.get('priority', 'normal')}] {item.get('text')} · source IDs {item.get('source_ids') or []}"
        for item in recommendations
    )
    if not recommendations:
        lines.append("• Срочных рекомендаций по текущему snapshot нет.")
    lines.append("\nКритические действия автоматически не выполнялись.")
    return "\n".join(lines)


async def ceo_brief(update: Update, _: ContextTypes.DEFAULT_TYPE):
    data = await api("GET", "/api/ceo/brief")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить brief", callback_data="ceo:refresh")],
        [InlineKeyboardButton("🧾 Создать задачу на разбор", callback_data="ceo:create_review_task")],
    ])
    await update.effective_message.reply_text(
        format_ceo_brief(data), reply_markup=keyboard
    )


async def tasks(
    update: Update,
    _: ContextTypes.DEFAULT_TYPE,
    *,
    view: str = "all",
    page: int = 1,
):
    data = await api(
        "POST",
        "/api/telegram/control/tasks/query",
        json={**_identity_payload(update), "view": view, "page": page, "page_size": 10},
    )
    items = data.get("items") or []
    view_labels = {
        "all": "Все задачи",
        "mine": "Мои задачи",
        "overdue": "Просроченные",
        "critical": "Критические задачи",
        "critical_events": "Критические события",
    }
    lines = [
        f"🧾 {view_labels.get(view, view)} · стр. {data['page']}/{data['total_pages']} · всего {data['total']}"
    ]
    for item in items:
        if item["item_type"] == "critical_event":
            acknowledgement = "принято" if item.get("acknowledged_at") else "не подтверждено"
            lines.append(
                f"#{item['id']} [{item['priority']}] {item['title']} — {item['status']}, {acknowledgement}\n"
                f"  source {item['source']['resource_type']} #{item['source']['resource_id']} · corr {item.get('correlation_id') or '—'}"
            )
        else:
            due = f" · срок {item['due_at']}" if item.get("due_at") else ""
            lines.append(
                f"#{item['id']} [{item['agent_type']}/{item['priority']}] {item['title']} — {item['status']}{due}\n"
                f"  workflow corr {item.get('correlation_id') or '—'}"
            )
    if not items:
        lines.append("Записей в этом представлении нет.")
    keyboard = [
        [
            InlineKeyboardButton("Все", callback_data="tasks:all:1"),
            InlineKeyboardButton("Мои", callback_data="tasks:mine:1"),
        ],
        [
            InlineKeyboardButton("Просроченные", callback_data="tasks:overdue:1"),
            InlineKeyboardButton("Критические", callback_data="tasks:critical:1"),
        ],
        [InlineKeyboardButton("События", callback_data="tasks:critical_events:1")],
    ]
    navigation = []
    if data.get("has_previous"):
        navigation.append(
            InlineKeyboardButton("←", callback_data=f"tasks:{view}:{data['page'] - 1}")
        )
    if data.get("has_next"):
        navigation.append(
            InlineKeyboardButton("→", callback_data=f"tasks:{view}:{data['page'] + 1}")
        )
    if navigation:
        keyboard.append(navigation)
    keyboard.append([InlineKeyboardButton("🏢 Mission Control", callback_data="dashboard")])
    await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def decisions(update: Update, _: ContextTypes.DEFAULT_TYPE):
    rows = await api("GET", "/api/decisions")
    text = "✅ Решения:\n" + "\n".join(f"#{x['id']} [{x['kind']}] {x['title']} — {x['status']}" for x in rows[:20]) if rows else "Решений нет."
    await update.effective_message.reply_text(text)


async def approvals(update: Update, _: ContextTypes.DEFAULT_TYPE):
    data = await api(
        "POST",
        "/api/telegram/control/approvals",
        json={**_identity_payload(update), "minimum_role": "owner"},
    )
    pending = data.get("items") or []
    if not pending:
        await update.effective_message.reply_text("Подтверждений, ожидающих владельца, нет."); return
    for row in pending[:10]:
        callbacks = row["callbacks"]
        keyboard = [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=callbacks["approve"]),
                InlineKeyboardButton("❌ Отклонить", callback_data=callbacks["reject"]),
            ],
            [
                InlineKeyboardButton(
                    "✏️ Запросить изменения",
                    callback_data=callbacks["request_changes"],
                )
            ],
        ]
        amount = f"\nСумма: {row['amount']}" if row.get("amount") is not None else ""
        await update.effective_message.reply_text(
            f"🔐 #{row['id']} · {row['action_kind']}\n"
            f"Объект: {row['resource_type']} #{row['resource_id']}\n"
            f"Риск: {row['risk']}{amount}\n"
            f"Причина: {row['rationale']}\n"
            f"Действует до: {row.get('expires_at') or 'не указано'}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


def format_outreach_summary(data: dict) -> str:
    messages = data.get("messages") or {}
    statuses = messages.get("statuses") or {}
    mailboxes = data.get("mailboxes") or {}
    consents = data.get("consents") or {}
    limits = data.get("limits") or {}
    inbound = data.get("inbound") or {}
    ready = "готова к отправке" if data.get("delivery_ready") else "нужна настройка SMTP"
    inbound_ready = (
        "готовы"
        if inbound.get("enabled", 0) > 0
        and inbound.get("forwarding_ready", 0) == inbound.get("enabled", 0)
        else "нужна настройка IMAP/основной почты"
    )
    return (
        "📣 Сервис рассылок\n"
        f"Система: {ready}\n"
        f"Почтовые ящики: {mailboxes.get('ready', 0)} готовы из {mailboxes.get('active', 0)} активных\n"
        f"Входящие ответы: {inbound_ready} ({inbound.get('forwarding_ready', 0)} из {inbound.get('enabled', 0)})\n"
        f"Подтверждённые согласия: {consents.get('verified', 0)}\n"
        f"Suppression / отписки: {data.get('suppressed', 0)}\n"
        f"Ожидают approval: {data.get('pending_approvals', 0)}\n"
        f"Очередь: {statuses.get('queued', 0)} · ждут настройки: {statuses.get('waiting_configuration', 0)}\n"
        f"Отправлено: {statuses.get('sent', 0)} · ошибки: {statuses.get('failed', 0)}\n"
        f"Лимиты: {limits.get('per_minute', 0)}/мин, {limits.get('per_day', 0)}/день\n\n"
        "Рассылка запускается только по адресам с зафиксированным согласием и после отдельного подтверждения владельца."
    )


def format_outreach_campaigns(data: dict) -> str:
    recent = ((data.get("campaigns") or {}).get("recent") or [])
    if not recent:
        return "📣 Кампаний пока нет. Ни одно письмо не отправлялось."
    status_labels = {
        "queued": "в очереди",
        "waiting_configuration": "ждут настройки",
        "sent": "отправлено",
        "delivered": "доставлено",
        "bounced": "отклонено",
        "complained": "жалобы",
        "unsubscribed": "отписки",
        "failed": "ошибки",
        "retry": "повтор",
    }
    lines = ["📨 Последние кампании"]
    for campaign in recent:
        counts = ", ".join(
            f"{status_labels.get(status, status)}: {count}"
            for status, count in (campaign.get("statuses") or {}).items()
        )
        lines.append(
            f"• {campaign.get('subject') or campaign.get('campaign_key')}\n"
            f"  {campaign.get('message_count', 0)} писем · {counts or 'нет сообщений'}"
        )
    return "\n".join(lines)


async def outreach_dashboard(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Доступ не разрешён.")
        return
    data = await api("GET", "/api/outreach/summary")
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Обновить", callback_data="outreach"),
            InlineKeyboardButton("📨 Кампании", callback_data="outreach:campaigns"),
        ],
        [InlineKeyboardButton("➕ Как создать рассылку", callback_data="outreach:help")],
    ])
    await update.effective_message.reply_text(format_outreach_summary(data), reply_markup=keyboard)


async def outreach_campaigns(update: Update, _: ContextTypes.DEFAULT_TYPE):
    data = await api("GET", "/api/outreach/summary")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← К панели", callback_data="outreach")]])
    await update.effective_message.reply_text(format_outreach_campaigns(data), reply_markup=keyboard)


async def outreach_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("← К панели", callback_data="outreach")]])
    await update.effective_message.reply_text(
        "➕ Новая рассылка\n\n"
        "Если клиенты сами передали email и попросили писать им, отправьте команду `/mailing`.\n"
        "Бот попросит XLSX/XLSM, DOCX или текстовый PDF, локально извлечёт до 1000 уникальных адресов, "
        "разобьёт их на партии по 100 и последовательно запросит основание согласия, тему и текст, затем покажет preview.\n"
        "Адреса также можно передать прямо в команде для обратной совместимости.\n\n"
        "Для рассылки документа по базе УК можно прислать PDF, Word, Excel или ODT с подписью «Разошли по базе УК».\n\n"
        "До подтверждения письма не ставятся в очередь. Отписки, suppression, дедупликация и лимиты применяются автоматически.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


def _mailing_addresses(text: str) -> list[str]:
    return sorted(set(match.group(0).lower() for match in EMAIL_PATTERN.finditer(text or "")))


def _has_close_word(text: str, candidates: tuple[str, ...], *, cutoff: float = 0.72) -> bool:
    words = re.findall(r"[a-zа-я0-9-]+", text.lower().replace("ё", "е"))
    return any(SequenceMatcher(None, word, candidate).ratio() >= cutoff for word in words for candidate in candidates)


def _suggested_bot_action(text: str, intent: dict) -> dict | None:
    """Infer only a small set of reversible bot actions from an ambiguous message."""
    normalized = " ".join((text or "").lower().replace("ё", "е").split())
    action_requested = any(
        word.startswith(("запуст", "начн", "созда", "сдела", "отправ", "разошл", "разосл"))
        for word in re.findall(r"[a-zа-я0-9-]+", normalized)
    )
    read_requested = any(word in normalized for word in ("покажи", "выведи", "открой", "какие", "что с"))
    ambiguous_task = intent.get("kind") == "task" and intent.get("agent_type") == "orchestrator"

    if _has_close_word(normalized, ("рассылка", "рассылку", "рассылки", "разослать")) and (action_requested or ambiguous_task):
        return {
            "action": "mailing",
            "question": "Вы хотели запустить рассылку?",
            "recipients": _mailing_addresses(text),
        }
    if read_requested and _has_close_word(normalized, ("задачи", "задачу", "задания")):
        return {"action": "tasks", "question": "Вы хотели открыть список задач?"}
    if read_requested and _has_close_word(normalized, ("одобрения", "подтверждения", "согласования")):
        return {"action": "approvals", "question": "Вы хотели открыть список подтверждений?"}
    if _has_close_word(normalized, ("дашборд", "панель")):
        return {"action": "dashboard", "question": "Вы хотели открыть главную панель?"}
    return None


async def _offer_action_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, suggestion: dict):
    nonce = secrets.token_urlsafe(6)
    context.user_data["pending_action_confirmation"] = {**suggestion, "nonce": nonce}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да", callback_data=f"intent:{nonce}:yes"),
        InlineKeyboardButton("❌ Нет", callback_data=f"intent:{nonce}:no"),
    ]])
    await update.effective_message.reply_text(
        f"Я не уверен, что правильно понял запрос.\n\n{suggestion['question']}",
        reply_markup=keyboard,
    )


async def _begin_mailing(update: Update, context: ContextTypes.DEFAULT_TYPE, addresses: list[str]):
    if len(addresses) > MAILING_MAX_RECIPIENTS:
        await update.effective_message.reply_text(
            f"За один запрос можно добавить не более {MAILING_MAX_RECIPIENTS} уникальных адресов. Разделите исходный файл."
        )
        return
    context.user_data["mailing_draft"] = {
        "step": "evidence" if addresses else "document",
        "recipients": addresses,
    }
    if addresses:
        batch_count = (len(addresses) + MAILING_BATCH_SIZE - 1) // MAILING_BATCH_SIZE
        await update.effective_message.reply_text(
            f"Получено адресов: {len(addresses)}. Внутренних партий по {MAILING_BATCH_SIZE}: {batch_count}. "
            "Теперь опишите основание согласия: когда и каким способом клиенты передали адреса и попросили получать письма.\n\n"
            "Пример: «12 августа 2026 года заказчики передали адреса в договорной переписке и попросили присылать предложения». Для отмены: /cancel"
        )
    else:
        await update.effective_message.reply_text(
            "Пришлите одним файлом базу получателей: Excel XLSX/XLSM, Word DOCX или текстовый PDF. "
            f"Бот локально найдёт до {MAILING_MAX_RECIPIENTS} уникальных email и разобьёт их на партии по {MAILING_BATCH_SIZE}. "
            "Сканированный PDF без текстового слоя не подойдёт. Для отмены: /cancel"
        )


async def mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Доступ не разрешён.")
        return
    await _begin_mailing(update, context, _mailing_addresses(" ".join(getattr(context, "args", ()) or ())))


async def mailing_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_data = getattr(context, "user_data", None)
    draft = user_data.get("mailing_draft") if isinstance(user_data, dict) else None
    if not isinstance(draft, dict):
        return False
    if draft.get("step") != "document":
        await update.effective_message.reply_text("Файл получен, но сейчас мастер ожидает текстовый ответ. Для отмены: /cancel")
        return True
    document = update.effective_message.document
    filename = _safe_document_filename(document.file_name or "recipients.xlsx")
    if Path(filename).suffix.lower() not in SUPPORTED_RECIPIENT_SUFFIXES:
        await update.effective_message.reply_text(
            "Не могу разобрать этот формат. Пришлите Excel XLSX/XLSM, Word DOCX или PDF с текстовым слоем."
        )
        return True
    if int(document.file_size or 0) > settings.max_attachment_bytes:
        await update.effective_message.reply_text(
            f"Файл больше безопасного лимита {settings.max_attachment_bytes // 1_000_000} МБ. Уменьшите его и пришлите повторно."
        )
        return True
    try:
        with tempfile.TemporaryDirectory(prefix="cleaningai-mailing-") as directory:
            path = Path(directory) / filename
            telegram_file = await context.bot.get_file(document.file_id)
            await telegram_file.download_to_drive(custom_path=str(path))
            content = path.read_bytes()
        if not content or len(content) > settings.max_attachment_bytes:
            raise ValueError("Файл пуст или превышает безопасный лимит")
        recipients = extract_recipient_emails(filename, content)
    except (TelegramError, OSError, ValueError) as exc:
        await update.effective_message.reply_text(
            f"Не удалось извлечь адреса: {str(exc)[:300]}. Пришлите исправленный XLSX/XLSM, DOCX или текстовый PDF."
        )
        return True
    if not recipients:
        await update.effective_message.reply_text(
            "В файле не найдено ни одного корректного email. Проверьте файл; для PDF нужен доступный для выделения текст."
        )
        return True
    if len(recipients) > MAILING_MAX_RECIPIENTS:
        await update.effective_message.reply_text(
            f"Найдено {len(recipients)} уникальных адресов — больше лимита {MAILING_MAX_RECIPIENTS} на один запрос. "
            "Разделите файл; адреса не были добавлены и рассылка не создана."
        )
        return True
    draft.update({
        "step": "evidence",
        "recipients": recipients,
        "source_filename": filename,
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "batch_size": MAILING_BATCH_SIZE,
    })
    batch_count = (len(recipients) + MAILING_BATCH_SIZE - 1) // MAILING_BATCH_SIZE
    await update.effective_message.reply_text(
        f"Из файла найдено {len(recipients)} уникальных email. Будет {batch_count} внутренних партий по максимум {MAILING_BATCH_SIZE}.\n\n"
        "Теперь опишите, когда и каким способом эти клиенты передали адреса и попросили получать рассылку. Для отмены: /cancel"
    )
    return True


async def mailing_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Доступ не разрешён.")
        return
    existed = bool(context.user_data.pop("mailing_draft", None))
    await update.effective_message.reply_text("Черновик рассылки отменён." if existed else "Активного черновика рассылки нет.")


async def mailing_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_data = getattr(context, "user_data", None)
    if not isinstance(user_data, dict):
        return False
    draft = user_data.get("mailing_draft")
    if not isinstance(draft, dict):
        return False
    text = " ".join((update.effective_message.text or "").split()).strip()
    step = draft.get("step")
    if step in {"document", "recipients"}:
        addresses = _mailing_addresses(text)
        if not addresses:
            await update.effective_message.reply_text(
                "Сейчас нужен файл получателей: XLSX/XLSM, DOCX или текстовый PDF. "
                "Для обратной совместимости можно также прислать адреса текстом. Для отмены: /cancel"
            )
            return True
        if len(addresses) > MAILING_MAX_RECIPIENTS:
            await update.effective_message.reply_text(
                f"За один запрос можно добавить не более {MAILING_MAX_RECIPIENTS} уникальных адресов."
            )
            return True
        draft.update({"recipients": addresses, "step": "evidence"})
        batch_count = (len(addresses) + MAILING_BATCH_SIZE - 1) // MAILING_BATCH_SIZE
        await update.effective_message.reply_text(
            f"Получено адресов: {len(addresses)}; партий по {MAILING_BATCH_SIZE}: {batch_count}. "
            "Опишите, когда и каким способом клиенты попросили получать рассылку."
        )
        return True
    if step == "evidence":
        if len(text) < 10:
            await update.effective_message.reply_text("Основание слишком короткое. Укажите дату или период, канал и просьбу клиента получать письма.")
            return True
        draft.update({"consent_evidence": text[:4000], "step": "subject"})
        await update.effective_message.reply_text("Введите тему письма (до 255 символов).")
        return True
    if step == "subject":
        if not text or len(text) > 255:
            await update.effective_message.reply_text("Тема должна содержать от 1 до 255 символов.")
            return True
        draft.update({"subject": text, "step": "body"})
        await update.effective_message.reply_text("Введите полный текст письма.")
        return True
    if step == "body":
        if not text:
            await update.effective_message.reply_text("Текст письма не может быть пустым.")
            return True
        draft.update({"body": text[:10000], "step": "preview"})
        addresses = draft.get("recipients") or []
        address_preview = ", ".join(addresses)
        if len(address_preview) > 1200:
            address_preview = address_preview[:1200] + "…"
        body_preview = draft["body"] if len(draft["body"]) <= 1800 else draft["body"][:1800] + "…"
        batch_count = (len(addresses) + MAILING_BATCH_SIZE - 1) // MAILING_BATCH_SIZE
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Создать на согласование", callback_data="mailing:create"),
            InlineKeyboardButton("❌ Отменить", callback_data="mailing:cancel"),
        ]])
        await update.effective_message.reply_text(
            f"📨 Preview рассылки\nПолучатели: {len(addresses)} · партии по {MAILING_BATCH_SIZE}: {batch_count}\n"
            f"Файл: {draft.get('source_filename', 'адреса введены текстом')}\n"
            f"Адреса: {address_preview}\n\n"
            f"Тема: {draft['subject']}\n\n{body_preview}\n\n"
            "Адреса и основание согласия будут сохранены только во внутренней базе. Письма пока не отправляются.",
            reply_markup=keyboard,
        )
        return True
    await update.effective_message.reply_text("Используйте кнопки под preview или /cancel.")
    return True


async def mailing_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data.get("mailing_draft")
    if not isinstance(draft, dict) or draft.get("step") != "preview":
        await update.effective_message.reply_text("Черновик устарел. Начните заново командой /mailing.")
        return
    result = await api("POST", "/api/outreach/campaigns/customer-requested/draft", json={
        "recipients": draft["recipients"],
        "consent_evidence": draft["consent_evidence"],
        "subject": draft["subject"],
        "body": draft["body"],
        "source_filename": draft.get("source_filename"),
        "source_sha256": draft.get("source_sha256"),
    })
    context.user_data.pop("mailing_draft", None)
    approval_id = result.get("approval_id")
    keyboard = await _approval_markup(update, int(approval_id)) if approval_id else None
    replay = " Ранее созданный черновик найден повторно; новая копия не создавалась." if result.get("idempotent_replay") else ""
    await update.effective_message.reply_text(
        f"Защищённый черновик создан как задача #{result['task_id']} для {result['recipient_count']} получателей "
        f"в {result.get('batch_count', 1)} партиях по максимум {result.get('batch_size', MAILING_BATCH_SIZE)}.{replay}\n"
        "Адреса имеют зафиксированное владельцем основание согласия. До отдельного одобрения письма не ставятся в очередь.",
        reply_markup=keyboard,
    )


async def improvement_queue(update: Update, _: ContextTypes.DEFAULT_TYPE):
    rows = await api("GET", "/api/improvements?status=queued")
    if not rows:
        await update.effective_message.reply_text("🛠 Очередь улучшений пуста.")
        return
    text = "🛠 Запросы на улучшение:\n" + "\n".join(
        f"#{x['id']} {x['suggested_function']} — {x['status']}" for x in rows[:20]
    )
    await update.effective_message.reply_text(text)


def format_activity_report(result: dict) -> str:
    from .reports import format_activity_report as render_activity_report

    return render_activity_report(result)


async def activity_report(update: Update, intent: dict):
    task = await api("POST", "/api/tasks", json={
        "title": "Сформировать отчёт о проделанной работе",
        "agent_type": "orchestrator",
        "priority": "high",
        "payload": {
            "action": "system_activity_report",
            "period_hours": intent.get("period_hours", 24),
            "source": "telegram_natural_language",
        },
        "max_attempts": 1,
    })
    completed = await api("POST", f"/api/tasks/{task['id']}/run")
    result = completed.get("result") or {}
    if completed.get("status") != "done" or result.get("outcome") != "completed":
        await update.effective_message.reply_text(
            f"Не удалось сформировать отчёт. Задача #{task['id']} сохранена с фактическим статусом "
            f"{completed.get('status', 'unknown')}."
        )
        return
    await update.effective_message.reply_text(format_activity_report(result))


def format_system_self_check(result: dict) -> str:
    icons = {
        "ready": "✅",
        "configuration_required": "⚙️",
        "credentials_required": "🔑",
        "unavailable": "❌",
    }
    summary = result.get("summary") or {}
    lines = [
        "🧪 Самопроверка CleaningAI OS",
        f"Общий статус: {'готово' if result.get('overall_status') == 'ready' else 'частично готово'}",
        f"Готово модулей: {summary.get('ready', 0)} из {summary.get('total', 0)}",
        "",
    ]
    for item in result.get("checks") or []:
        lines.append(f"{icons.get(item['status'], '•')} {item['name']}: {item['detail']}")
    required = result.get("credentials_required") or []
    if required:
        lines.append("\nНужны настройки: " + ", ".join(required))
    lines.append("\nПлатежи, договоры, заявки, массовые рассылки и кадровые решения не выполнялись.")
    return "\n".join(lines)


async def system_self_check(update: Update):
    task = await api("POST", "/api/tasks", json={
        "title": "Безопасная самопроверка функционала чат-бота",
        "agent_type": "orchestrator",
        "priority": "high",
        "payload": {"action": "system_self_check", "source": "telegram_natural_language"},
        "max_attempts": 1,
    })
    completed = await api("POST", f"/api/tasks/{task['id']}/run")
    result = completed.get("result") or {}
    if completed.get("status") != "done" or result.get("outcome") != "completed":
        await update.effective_message.reply_text(
            f"Самопроверка не завершилась. Задача #{task['id']} имеет фактический статус "
            f"{completed.get('status', 'unknown')}."
        )
        return
    await update.effective_message.reply_text(format_system_self_check(result))


def format_task_timing(result: dict) -> str:
    if result.get("outcome") == "needs_clarification":
        return result.get("message", "Укажите номер задачи.")
    task = result.get("task") or {}
    lines = [
        f"⏱ Задача #{task.get('id')}: {task.get('title')}",
        f"Фактический статус: {task.get('status')}",
    ]
    if result.get("timing_status") == "completed":
        lines.append("Осталось: 0 — подтверждённый результат уже готов.")
    elif result.get("timing_status") == "result_unverified":
        lines.append("Честный срок сейчас определить нельзя: задача помечена выполненной, но результата или файла нет.")
    else:
        lines.append("Честный срок пока определить нельзя.")
    actual = result.get("actual_runtime_seconds")
    if actual is not None:
        lines.append(f"Техническая попытка заняла {actual:g} сек.; это не равно времени выполнения бизнес-задачи.")
    estimate = result.get("planning_estimate") or {}
    if estimate:
        lines.append(
            f"Предварительный ориентир: {estimate['min_hours']:g}–{estimate['max_hours']:g} ч. "
            f"после выполнения условий запуска (точность: {estimate['confidence']})."
        )
        lines.append("До старта нужно: " + "; ".join(estimate.get("starts_after") or []))
    lines.append("Причина: " + result.get("reason", "нет данных"))
    return "\n".join(lines)


async def task_timing(update: Update, intent: dict):
    task = await api("POST", "/api/tasks", json={
        "title": "Оценить срок выполнения задачи",
        "agent_type": "orchestrator",
        "priority": "high",
        "payload": {
            "action": "task_timing_report",
            "task_id": intent.get("task_id"),
            "source": "telegram_read_request",
        },
        "max_attempts": 1,
    })
    completed = await api("POST", f"/api/tasks/{task['id']}/run")
    result = completed.get("result") or {}
    if completed.get("status") != "done":
        await update.effective_message.reply_text(
            f"Не удалось оценить срок. Проверка #{task['id']} завершилась со статусом "
            f"{completed.get('status', 'unknown')}."
        )
        return
    await update.effective_message.reply_text(format_task_timing(result))


def format_social_account_setup(result: dict, task_id: int) -> str:
    if result.get("status") != "setup_in_progress":
        return (
            f"Оформление соцсетей не началось. Задача #{task_id} сохранила фактическую ошибку: "
            f"{result.get('error') or result.get('reason') or 'неизвестная ошибка'}."
        )
    labels = {"vk": "VK", "odnoklassniki": "Одноклассники", "telegram": "Telegram", "instagram": "Instagram"}
    lines = [
        "📱 Оформление социальных сетей начато",
        f"Задача #{task_id}: сохранено новых карточек {result.get('records_created', 0)}, обновлено {result.get('records_updated', 0)}.",
    ]
    for platform in result.get("platforms") or []:
        missing = platform.get("missing_configuration") or []
        lines.append(
            f"• {labels.get(platform.get('channel'), platform.get('channel'))}: "
            f"{platform.get('status')}" + (f"; нужно: {', '.join(missing)}" if missing else "")
        )
    lines.extend(
        [
            "",
            "Внешних аккаунтов автоматически создано: 0. Телефонную проверку, CAPTCHA и подтверждение владельца нельзя имитировать.",
            "Публикации не запускались; финальные изображения и текст потребуют отдельного визуального согласования.",
        ]
    )
    return "\n".join(lines)


async def module_summary(update: Update, module: str, title: str):
    data = (await api("GET", "/api/modules/summary"))[module]
    await update.effective_message.reply_text(title + "\n" + "\n".join(f"{key}: {value}" for key, value in data.items()))


async def records(update: Update, record_type: str, title: str):
    rows = await api("GET", f"/api/records?record_type={record_type}")
    text = title + "\n" + "\n".join(f"#{x['id']} {x['title']} — {x['status']}" for x in rows[:20]) if rows else title + "\nДанных пока нет."
    await update.effective_message.reply_text(text)


async def addtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update): return
    title = " ".join(context.args).strip()
    if not title: await update.effective_message.reply_text("Использование: /addtask Текст задачи"); return
    data = await api("POST", "/api/tasks", json={"title": title})
    await update.effective_message.reply_text(f"Задача #{data['id']} создана: {data['title']}")


async def natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Доступ не разрешён.")
        return
    if await mailing_input(update, context):
        return
    message = update.effective_message
    replied = getattr(message, "reply_to_message", None)
    referenced_text = (getattr(replied, "text", None) or getattr(replied, "caption", None) or "") if replied else ""
    intent = understand_russian_message(message.text or "", referenced_text=referenced_text)
    suggestion = _suggested_bot_action(message.text or "", intent)
    expected_kind = {"tasks": "tasks", "approvals": "approvals", "dashboard": "dashboard"}
    if suggestion and intent.get("kind") != expected_kind.get(suggestion["action"]):
        await _offer_action_confirmation(update, context, suggestion)
        return
    try:
        try:
            analysis = await api("POST", "/api/request-analysis", json={
                "message": update.effective_message.text or "",
                "intent": intent,
                "source_channel": "telegram",
                "source_user": str(update.effective_user.id if update.effective_user else "owner"),
            })
        except httpx.HTTPError:
            analysis = {"classification": "analysis_unavailable", "improvement_id": None}
        if isinstance(analysis.get("resolved_intent"), dict):
            intent = analysis["resolved_intent"]
        kind = intent["kind"]
        if kind == "greeting":
            await update.effective_message.reply_text("Здравствуйте! Напишите обычным русским текстом, что нужно сделать или показать.")
        elif kind == "acknowledgement":
            await update.effective_message.reply_text("Хорошо. Я готов к следующему запросу.")
        elif kind == "clarification":
            await update.effective_message.reply_text(intent["message"])
        elif kind == "help":
            await update.effective_message.reply_text(
                "Пишите без команд, например:\n"
                "• Покажи текущие задачи\n"
                "• Что с тендерами?\n"
                "• Найди тендеры по уборке БЦ\n"
                "• Создай задачу связаться с новым клиентом\n"
                "• Проанализируй финансы\n\n"
                "Request Analyst проверяет каждый запрос. Если функции не хватает, он создаёт техническое задание для Codex с критериями и тест-планом.\n\n"
                "Оплата, договоры, подача тендеров, окончательные кадровые решения и массовые рассылки всегда потребуют вашего подтверждения."
            )
        elif kind == "dashboard":
            await dashboard(update, context)
        elif kind == "tasks":
            await tasks(update, context)
        elif kind == "decisions":
            await decisions(update, context)
        elif kind == "approvals":
            await approvals(update, context)
        elif kind == "records":
            await records(update, intent["record_type"], intent["title"])
        elif kind == "summary":
            await module_summary(update, intent["module"], intent["title"])
        elif kind == "inbox":
            rows = await api("GET", "/api/inbox")
            text = "📥 Входящие:\n" + "\n".join(f"#{x['id']} [{x['channel']}] {x['subject'] or x['sender']} — {x['status']}" for x in rows[:20]) if rows else "Входящих сообщений пока нет."
            await update.effective_message.reply_text(text)
        elif kind == "outreach":
            await outreach_dashboard(update, context)
        elif kind == "improvements":
            await improvement_queue(update, context)
        elif kind == "activity_report":
            await activity_report(update, intent)
        elif kind == "system_self_check":
            await system_self_check(update)
        elif kind == "task_eta":
            await task_timing(update, intent)
        else:
            action = intent["payload"].get("action")
            if action == "review_previous_text" and not intent["payload"].get("referenced_text"):
                await update.effective_message.reply_text(
                    "Не нашёл предыдущий текст в сохранённой истории. Перешлите письмо или ответьте на него фразой "
                    "«дай обратную связь», и я сразу подготовлю разбор и новый черновик."
                )
                return
            if analysis.get("classification") == "capability_gap":
                title = intent["title"]
                await _offer_action_confirmation(update, context, {
                    "action": "create_task",
                    "question": f"Вы хотели создать задачу «{title[:180]}»?",
                    "intent": intent,
                    "improvement_id": analysis.get("improvement_id"),
                })
                return
            data = await api("POST", "/api/tasks", json={
                "title": intent["title"],
                "agent_type": intent["agent_type"],
                "priority": intent["priority"],
                "payload": intent["payload"],
                "max_attempts": 1
                if action in {"generate_proposal", "improve_referenced_text", "review_previous_text", "prepare_social_account_setup"}
                else 3,
            })
            protection = " Критическое действие будет остановлено до вашего подтверждения." if intent["protected"] else ""
            if action == "review_previous_text":
                completed = await api("POST", f"/api/tasks/{data['id']}/run")
                result = completed.get("result") or {}
                if completed.get("status") == "done" and result.get("status") == "ready":
                    await update.effective_message.reply_text(
                        f"Обратная связь:\n{result['feedback_text']}\n\nПредлагаемый вариант:\n\n{result['revised_text']}\n\n"
                        "Это черновик: он сохранён в задаче и никуда не отправлен."
                    )
                else:
                    await update.effective_message.reply_text(
                        f"Не удалось разобрать текст: {result.get('error') or result.get('reason') or completed.get('status', 'неизвестная ошибка')}. "
                        "Ошибка сохранена в задаче и audit log."
                    )
            elif action == "improve_referenced_text":
                completed = await api("POST", f"/api/tasks/{data['id']}/run")
                result = completed.get("result") or {}
                if completed.get("status") == "done" and result.get("status") == "ready":
                    changes = "\n".join(f"• {item}" for item in result.get("changes", []))
                    await update.effective_message.reply_text(
                        f"Обновлённый черновик:\n\n{result['improved_text']}\n\nЧто изменено:\n{changes}\n\n"
                        "Текст никуда не отправлен — проверьте его перед использованием."
                    )
                else:
                    await update.effective_message.reply_text(
                        f"Не удалось улучшить текст: {result.get('error') or result.get('reason') or completed.get('status', 'неизвестная ошибка')}. "
                        "Ошибка сохранена в задаче и audit log."
                    )
            elif intent["payload"].get("action") == "generate_proposal":
                completed = await api("POST", f"/api/tasks/{data['id']}/run")
                result = completed.get("result") or {}
                if completed.get("status") == "done" and result.get("download_url"):
                    content, filename = await api_file(result["download_url"])
                    document = io.BytesIO(content)
                    document.name = filename
                    await update.effective_message.reply_document(
                        document=document,
                        filename=filename,
                        caption=(
                            f"Готов проект коммерческого предложения {result['proposal_number']}. "
                            "Он создан из CRM, не отправлен клиенту и требует вашей проверки перед использованием."
                        ),
                    )
                else:
                    await update.effective_message.reply_text(
                        f"Не удалось подготовить КП: {result.get('error', 'неизвестная ошибка')}. "
                        "Результат записан в задаче и audit log."
                    )
            elif intent["payload"].get("action") == "prepare_social_account_setup":
                completed = await api("POST", f"/api/tasks/{data['id']}/run")
                result = completed.get("result") or {}
                if completed.get("status") != "done":
                    result = {
                        **result,
                        "status": "failed",
                        "error": result.get("error") or f"статус задачи {completed.get('status', 'unknown')}",
                    }
                await update.effective_message.reply_text(format_social_account_setup(result, data["id"]))
            elif analysis.get("improvement_id"):
                await update.effective_message.reply_text(
                    f"Я сохранил запрос как задачу #{data['id']}, но текущая версия не может гарантировать полный результат. "
                    f"Request Analyst создал улучшение #{analysis['improvement_id']} для Codex и приложил обязательный тест-план."
                )
            elif analysis.get("classification") == "configuration_required":
                await update.effective_message.reply_text(
                    f"Создана задача #{data['id']} для агента {data['agent_type']}, но для полного выполнения нужны внешние credentials или источники."
                )
            else:
                await update.effective_message.reply_text(
                    f"Понял запрос. Создана задача #{data['id']} для агента {data['agent_type']}.{protection}"
                )
    except httpx.HTTPError:
        await update.effective_message.reply_text("Не удалось связаться с CleaningAI OS. Проверьте состояние сервисов в Mission Control.")


async def confirmed_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = (q.data or "").split(":", 2)
    pending = context.user_data.get("pending_action_confirmation")
    if len(parts) != 3 or not isinstance(pending, dict) or pending.get("nonce") != parts[1]:
        await update.effective_message.reply_text("Это уточнение уже устарело. Напишите запрос ещё раз.")
        return
    context.user_data.pop("pending_action_confirmation", None)
    if parts[2] == "no":
        await update.effective_message.reply_text("Действие отменено. Напишите, что вы хотели сделать, другими словами.")
        return

    action = pending.get("action")
    if action == "mailing":
        await _begin_mailing(update, context, pending.get("recipients") or [])
    elif action == "tasks":
        await tasks(update, context)
    elif action == "approvals":
        await approvals(update, context)
    elif action == "dashboard":
        await dashboard(update, context)
    elif action == "create_task":
        intent = pending["intent"]
        data = await api("POST", "/api/tasks", json={
            "title": intent["title"],
            "agent_type": intent["agent_type"],
            "priority": intent["priority"],
            "payload": intent["payload"],
            "max_attempts": 3,
        })
        improvement = (
            f" Request Analyst также зафиксировал улучшение #{pending['improvement_id']}."
            if pending.get("improvement_id") else ""
        )
        await update.effective_message.reply_text(
            f"✅ Выполнено: создана задача #{data['id']} для агента {data['agent_type']}.{improvement}"
        )
    else:
        await update.effective_message.reply_text("Не удалось выполнить подтверждённое действие. Напишите запрос ещё раз.")


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.startswith("ta1."):
        try:
            result = await api(
                "POST",
                "/api/telegram/control/alert-acknowledgement",
                json={
                    **_identity_payload(update),
                    "callback_token": q.data,
                },
            )
        except (httpx.HTTPError, RuntimeError):
            await update.effective_message.reply_text(
                "Оповещение не подтверждено: кнопка недействительна, устарела или доступ запрещён."
            )
            return
        message = (
            f"Оповещение #{result['id']} уже было принято ранее."
            if result.get("idempotent_replay")
            else f"✅ Получение оповещения #{result['id']} подтверждено."
        )
        await update.effective_message.reply_text(message)
        return
    if q.data.startswith("tc1."):
        try:
            result = await api(
                "POST",
                "/api/telegram/control/approval-decision",
                json={
                    **_identity_payload(update),
                    "callback_token": q.data,
                    "note": "Решение принято владельцем в Telegram",
                },
            )
        except (httpx.HTTPError, RuntimeError):
            await update.effective_message.reply_text(
                "Кнопка недействительна, устарела или у вас нет права на это решение. Действие не выполнено."
            )
            return
        approval_id = result["id"]
        if result.get("idempotent_replay"):
            message = f"Подтверждение #{approval_id} уже было обработано; повторное действие не выполнялось."
        elif result.get("action_kind") == "bulk_outreach" and result.get("execution") == "queued":
            message = (
                f"Подтверждение #{approval_id}: approved. Защищённая задача поставлена в очередь; "
                "worker применит SMTP-настройки, suppression, дедупликацию и лимиты."
            )
        elif result.get("action_kind") == "bulk_outreach":
            message = f"Подтверждение #{approval_id}: {result['status']}. Рассылка не поставлена в очередь."
        else:
            message = (
                f"Подтверждение #{approval_id}: {result['status']}. Решение записано; "
                "автоматическое подписание или списание денег не выполнялось."
            )
        await update.effective_message.reply_text(message)
        return
    if q.data.startswith("approve:") or q.data.startswith("reject:"):
        await update.effective_message.reply_text(
            "Эта кнопка относится к старой версии и больше не действует. Откройте approvals заново."
        )
        return

    minimum_role = (
        "owner"
        if q.data == "approvals"
        else "manager"
        if q.data in {"meta_brain", "simulator", "marketing_invoices", "improvements"}
        or q.data == "ceo"
        or q.data.startswith("ceo:")
        else "operator"
        if q.data.startswith("intent:") or q.data.startswith("mailing:")
        else "viewer"
    )
    identity = await _authorize_update(update, minimum_role)
    if identity is None:
        return
    token = _telegram_identity.set(identity)
    try:
        if q.data.startswith("intent:"): await confirmed_action(update, context)
        elif q.data == "dashboard": await dashboard(update, context)
        elif q.data == "tasks": await tasks(update, context)
        elif q.data.startswith("tasks:"):
            match = re.fullmatch(
                r"tasks:(all|mine|overdue|critical|critical_events):([1-9][0-9]{0,3})",
                q.data,
            )
            if not match:
                await update.effective_message.reply_text("Навигационная кнопка недействительна.")
            else:
                await tasks(update, context, view=match.group(1), page=int(match.group(2)))
        elif q.data == "decisions": await decisions(update, context)
        elif q.data == "approvals": await approvals(update, context)
        elif q.data == "crm": await records(update, "lead", "👥 CRM и продажи")
        elif q.data == "tenders": await records(update, "tender", "🏗 Тендеры")
        elif q.data == "hr": await records(update, "candidate", "🧹 Кандидаты и HR")
        elif q.data == "finance": await module_summary(update, "finance", "💰 Финансы")
        elif q.data == "marketing": await module_summary(update, "marketing", "📊 Маркетинг")
        elif q.data == "marketing_invoices": await records(update, "marketing_invoice", "🧾 Счета рекламы · одобрение не выполняет оплату")
        elif q.data == "simulator":
            data = await api("POST", "/api/simulations", json={"payroll_change_percent": 10})
            await update.effective_message.reply_text(f"🧪 Сценарий +10% к фонду оплаты\nТекущая прибыль: {data['current']['profit']} ₽\nБазовый прогноз: {data['base']['profit']} ₽\nОптимистичный: {data['optimistic']['profit']} ₽")
        elif q.data == "mailing:create": await mailing_create(update, context)
        elif q.data == "mailing:cancel": await mailing_cancel(update, context)
        elif q.data in {"ceo", "ceo:refresh"}:
            await ceo_brief(update, context)
        elif q.data == "ceo:create_review_task":
            data = await api("POST", "/api/tasks", json={
                "title": "AI CEO · Разобрать текущий brief и назначить следующие безопасные действия",
                "agent_type": "ceo",
                "priority": "high",
                "payload": {
                    "action": "ceo_brief_review",
                    "source": "telegram_control_center",
                    "automatic_critical_action": False,
                },
                "max_attempts": 1,
            })
            await update.effective_message.reply_text(
                f"Создана аналитическая задача #{data['id']}. Критические действия не запускались."
            )
        elif q.data == "meta_brain":
            data = await api("POST", "/api/tasks", json={"title": f"{q.data} on-demand review", "agent_type": q.data})
            await update.effective_message.reply_text(f"Задача #{data['id']} поставлена агенту {q.data}.")
        elif q.data == "agents": await dashboard(update, context)
        elif q.data == "improvements": await improvement_queue(update, context)
        elif q.data == "outreach": await outreach_dashboard(update, context)
        elif q.data == "outreach:campaigns": await outreach_campaigns(update, context)
        elif q.data == "outreach:help": await outreach_help(update, context)
    finally:
        _telegram_identity.reset(token)


def build_application() -> Application:
    if not settings.telegram_bot_token: raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    if not settings.owner_telegram_id: raise RuntimeError("OWNER_TELEGRAM_ID is empty")
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    builder = Application.builder().token(settings.telegram_bot_token)
    if settings.telegram_bot_api_base_url:
        local_base = settings.telegram_bot_api_base_url.rstrip("/")
        builder = builder.base_url(f"{local_base}/bot").base_file_url(f"{local_base}/file/bot").local_mode(True)
    application = builder.build()
    commands = [
        ("start", start, "viewer"),
        ("dashboard", dashboard, "viewer"),
        ("ceo", ceo_brief, "manager"),
        ("tasks", tasks, "viewer"),
        ("decisions", decisions, "viewer"),
        ("outreach", outreach_dashboard, "viewer"),
        ("mailing", mailing_start, "operator"),
        ("cancel", mailing_cancel, "operator"),
        ("addtask", addtask, "operator"),
    ]
    for command, handler, minimum_role in commands:
        application.add_handler(CommandHandler(command, _secured(handler, minimum_role)))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(
        MessageHandler(filters.Document.ALL, _secured(proposal_document, "operator"))
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            _secured(natural_language, "operator"),
        )
    )
    return application


def main():
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__": main()
