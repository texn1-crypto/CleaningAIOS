import io
import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
        filename = disposition.split("filename=", 1)[-1].strip('"') if "filename=" in disposition else "commercial-proposal.pdf"
        return response.content, filename


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
    summary = result.get("summary") or {}
    lines = [
        f"📋 Отчёт CleaningAI OS за {result.get('period_hours', 24)} ч.",
        f"✅ Выполнено задач: {summary.get('tasks_completed', 0)}",
        f"🔄 В работе и очереди: {summary.get('tasks_active', 0)}",
        f"⚠️ Ошибок: {summary.get('tasks_failed', 0)}",
        f"⛔ Заблокировано: {summary.get('tasks_blocked', 0)}",
        f"🛠 Улучшений в очереди: {summary.get('queued_improvements', 0)}",
        f"🔐 Ожидают подтверждения: {summary.get('pending_approvals', 0)}",
    ]
    recent = result.get("recent_completed_tasks") or []
    if recent:
        lines.append("\nПоследние результаты:")
        lines.extend(
            f"• #{row['id']} [{row['agent_type']}] {row['title']}"
            for row in recent[:5]
        )
    blockers = result.get("blockers") or []
    lines.append("\nТребуют внимания: " + ("; ".join(blockers) if blockers else "нет."))
    return "\n".join(lines)


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
    intent = understand_russian_message(update.effective_message.text or "")
    kind = intent["kind"]
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
        if kind == "greeting":
            await update.effective_message.reply_text("Здравствуйте! Напишите обычным русским текстом, что нужно сделать или показать.")
        elif kind == "acknowledgement":
            await update.effective_message.reply_text("Хорошо. Я готов к следующему запросу.")
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
        elif kind == "improvements":
            await improvement_queue(update, context)
        elif kind == "activity_report":
            await activity_report(update, intent)
        else:
            data = await api("POST", "/api/tasks", json={
                "title": intent["title"],
                "agent_type": intent["agent_type"],
                "priority": intent["priority"],
                "payload": intent["payload"],
                "max_attempts": 1 if intent["payload"].get("action") == "generate_proposal" else 3,
            })
            protection = " Критическое действие будет остановлено до вашего подтверждения." if intent["protected"] else ""
            if intent["payload"].get("action") == "generate_proposal":
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
        await update.effective_message.reply_text(f"Подтверждение #{approval_id}: {result['status']}. Решение записано; автоматическое подписание или списание денег не выполнялось.")
    elif q.data in {"ceo", "meta_brain"}:
        data = await api("POST", "/api/tasks", json={"title": f"{q.data} on-demand review", "agent_type": q.data})
        await update.effective_message.reply_text(f"Задача #{data['id']} поставлена агенту {q.data}.")
    elif q.data == "agents": await dashboard(update, context)
    elif q.data == "improvements": await improvement_queue(update, context)
    elif q.data == "outreach": await update.effective_message.reply_text("📣 Рассылки управляются через /docs. Отправка требует настроенного SMTP и owner approval для bulk-кампаний.")


def build_application() -> Application:
    if not settings.telegram_bot_token: raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    if not settings.owner_telegram_id: raise RuntimeError("OWNER_TELEGRAM_ID is empty")
    application = Application.builder().token(settings.telegram_bot_token).build()
    for command, handler in [("start", start), ("dashboard", dashboard), ("tasks", tasks), ("decisions", decisions), ("addtask", addtask)]: application.add_handler(CommandHandler(command, handler))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_language))
    return application


def main():
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__": main()
