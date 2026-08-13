import asyncio
import base64
import io
import logging
import re
import secrets
from pathlib import Path
from urllib.parse import unquote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .chat import understand_russian_message
from .config import settings

logging.basicConfig(level=logging.INFO)
# httpx logs full request URLs at INFO. Telegram embeds the bot token in those
# URLs, so INFO-level HTTP logs would leak a production credential.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
BASE = (settings.internal_api_url or settings.public_base_url or "http://web:8000").rstrip("/")
HEADERS = {"X-API-Key": settings.api_key, "X-Actor": "telegram-owner", "X-Role": "owner"}
EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])")


def allowed(update: Update) -> bool:
    return bool(settings.owner_telegram_id) and str(update.effective_user.id if update.effective_user else 0) == str(settings.owner_telegram_id)


async def api(method: str, path: str, **kwargs):
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        response = await client.request(method, f"{BASE}{path}", **kwargs)
        response.raise_for_status()
        return response.json()


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
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Одобрить рассылку", callback_data=f"approve:{approval_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{approval_id}"),
            ]])
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
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Утвердить", callback_data=f"approve:{approval_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{approval_id}"),
        ]]) if approval_id else None
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


async def tasks(update: Update, _: ContextTypes.DEFAULT_TYPE):
    rows = await api("GET", "/api/tasks")
    text = "🧾 Задачи:\n" + "\n".join(f"#{x['id']} [{x['agent_type']}] {x['title']} — {x['status']}" for x in rows[:20]) if rows else "Задач пока нет."
    await update.effective_message.reply_text(text)


async def decisions(update: Update, _: ContextTypes.DEFAULT_TYPE):
    rows = await api("GET", "/api/decisions")
    text = "✅ Решения:\n" + "\n".join(f"#{x['id']} [{x['kind']}] {x['title']} — {x['status']}" for x in rows[:20]) if rows else "Решений нет."
    await update.effective_message.reply_text(text)


async def approvals(update: Update, _: ContextTypes.DEFAULT_TYPE):
    rows = await api("GET", "/api/approvals")
    pending = [x for x in rows if x["status"] == "pending"]
    if not pending:
        await update.effective_message.reply_text("Подтверждений, ожидающих владельца, нет."); return
    for row in pending[:10]:
        keyboard = [[InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{row['id']}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{row['id']}")]]
        await update.effective_message.reply_text(f"🔐 #{row['id']} · {row['action_kind']}\n{row['resource_type']} #{row['resource_id']}\n{row['rationale']}", reply_markup=InlineKeyboardMarkup(keyboard))


def format_outreach_summary(data: dict) -> str:
    messages = data.get("messages") or {}
    statuses = messages.get("statuses") or {}
    mailboxes = data.get("mailboxes") or {}
    consents = data.get("consents") or {}
    limits = data.get("limits") or {}
    ready = "готова к отправке" if data.get("delivery_ready") else "нужна настройка SMTP"
    return (
        "📣 Сервис рассылок\n"
        f"Система: {ready}\n"
        f"Почтовые ящики: {mailboxes.get('ready', 0)} готовы из {mailboxes.get('active', 0)} активных\n"
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
        "Если клиенты сами передали email и попросили писать им, отправьте команду:\n"
        "`/mailing client@example.com second@example.com`\n"
        "Бот последовательно запросит основание согласия, тему и текст, затем покажет preview.\n\n"
        "Для рассылки документа по базе УК можно прислать PDF, Word, Excel или ODT с подписью «Разошли по базе УК».\n\n"
        "До подтверждения письма не ставятся в очередь. Отписки, suppression, дедупликация и лимиты применяются автоматически.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


def _mailing_addresses(text: str) -> list[str]:
    return sorted(set(match.group(0).lower() for match in EMAIL_PATTERN.finditer(text or "")))


async def mailing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Доступ не разрешён.")
        return
    addresses = _mailing_addresses(" ".join(context.args))
    if len(addresses) > 100:
        await update.effective_message.reply_text("За один черновик можно добавить не более 100 адресов.")
        return
    context.user_data["mailing_draft"] = {
        "step": "evidence" if addresses else "recipients",
        "recipients": addresses,
    }
    if addresses:
        await update.effective_message.reply_text(
            f"Получено адресов: {len(addresses)}. Теперь опишите основание согласия: когда и каким способом клиенты передали адреса и попросили получать письма.\n\n"
            "Пример: «12 августа 2026 года заказчики передали адреса в договорной переписке и попросили присылать предложения». Для отмены: /cancel"
        )
    else:
        await update.effective_message.reply_text(
            "Пришлите email-адреса одним сообщением, через пробел или запятую. Максимум 100. Для отмены: /cancel"
        )


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
    if step == "recipients":
        addresses = _mailing_addresses(text)
        if not addresses:
            await update.effective_message.reply_text("Не нашёл корректных email-адресов. Пришлите их через пробел или запятую либо нажмите /cancel.")
            return True
        if len(addresses) > 100:
            await update.effective_message.reply_text("За один черновик можно добавить не более 100 адресов.")
            return True
        draft.update({"recipients": addresses, "step": "evidence"})
        await update.effective_message.reply_text(
            f"Получено адресов: {len(addresses)}. Опишите, когда и каким способом клиенты попросили получать рассылку."
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
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Создать на согласование", callback_data="mailing:create"),
            InlineKeyboardButton("❌ Отменить", callback_data="mailing:cancel"),
        ]])
        await update.effective_message.reply_text(
            f"📨 Preview рассылки\nПолучатели ({len(addresses)}): {address_preview}\n\n"
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
    })
    context.user_data.pop("mailing_draft", None)
    approval_id = result.get("approval_id")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить рассылку", callback_data=f"approve:{approval_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{approval_id}"),
    ]]) if approval_id else None
    replay = " Ранее созданный черновик найден повторно; новая копия не создавалась." if result.get("idempotent_replay") else ""
    await update.effective_message.reply_text(
        f"Защищённый черновик создан как задача #{result['task_id']} для {result['recipient_count']} получателей.{replay}\n"
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


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not allowed(update): return
    if q.data == "dashboard": await dashboard(update, context)
    elif q.data == "tasks": await tasks(update, context)
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
    elif q.data.startswith("approve:") or q.data.startswith("reject:"):
        action, approval_id = q.data.split(":", 1)
        result = await api("POST", f"/api/approvals/{approval_id}/{action}", json={"note": "Решение принято владельцем в Telegram"})
        if result.get("action_kind") == "bulk_outreach" and result.get("execution") == "queued":
            message = (
                f"Подтверждение #{approval_id}: approved. Защищённая задача поставлена в очередь; "
                "worker отправит только допустимые письма с учётом SMTP-настроек, suppression, дедупликации и лимитов."
            )
        elif result.get("action_kind") == "bulk_outreach":
            message = f"Подтверждение #{approval_id}: {result['status']}. Рассылка не поставлена в очередь."
        else:
            message = f"Подтверждение #{approval_id}: {result['status']}. Решение записано; автоматическое подписание или списание денег не выполнялось."
        await update.effective_message.reply_text(message)
    elif q.data == "mailing:create": await mailing_create(update, context)
    elif q.data == "mailing:cancel": await mailing_cancel(update, context)
    elif q.data in {"ceo", "meta_brain"}:
        data = await api("POST", "/api/tasks", json={"title": f"{q.data} on-demand review", "agent_type": q.data})
        await update.effective_message.reply_text(f"Задача #{data['id']} поставлена агенту {q.data}.")
    elif q.data == "agents": await dashboard(update, context)
    elif q.data == "improvements": await improvement_queue(update, context)
    elif q.data == "outreach": await outreach_dashboard(update, context)
    elif q.data == "outreach:campaigns": await outreach_campaigns(update, context)
    elif q.data == "outreach:help": await outreach_help(update, context)


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
    for command, handler in [("start", start), ("dashboard", dashboard), ("tasks", tasks), ("decisions", decisions), ("outreach", outreach_dashboard), ("mailing", mailing_start), ("cancel", mailing_cancel), ("addtask", addtask)]: application.add_handler(CommandHandler(command, handler))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.Document.ALL, proposal_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language))
    return application


def main():
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__": main()
