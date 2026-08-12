import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .config import settings

logging.basicConfig(level=logging.INFO)
BASE = (settings.internal_api_url or settings.public_base_url or "http://web:8000").rstrip("/")
HEADERS = {"X-API-Key": settings.api_key, "X-Actor": "telegram-owner", "X-Role": "owner"}


def allowed(update: Update) -> bool:
    return bool(settings.owner_telegram_id) and str(update.effective_user.id if update.effective_user else 0) == str(settings.owner_telegram_id)


async def api(method: str, path: str, **kwargs):
    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
        response = await client.request(method, f"{BASE}{path}", **kwargs)
        response.raise_for_status()
        return response.json()


async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.effective_message.reply_text("Доступ не разрешён."); return
    rows = [["🏢 Mission Control", "dashboard"], ["🤖 AI CEO", "ceo"], ["🧠 E-агенты", "agents"], ["✅ Решения и approvals", "approvals"], ["👥 CRM и продажи", "crm"], ["🏗 Тендеры", "tenders"], ["🧹 Кандидаты и HR", "hr"], ["💰 Финансы", "finance"], ["📊 Маркетинг", "marketing"], ["🧪 Симулятор", "simulator"], ["🧾 Задачи", "tasks"], ["🧬 Meta Brain", "meta_brain"], ["📣 Рассылки", "outreach"]]
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
    elif q.data == "simulator":
        data = await api("POST", "/api/simulations", json={"payroll_change_percent": 10})
        await update.effective_message.reply_text(f"🧪 Сценарий +10% к фонду оплаты\nТекущая прибыль: {data['current']['profit']} ₽\nБазовый прогноз: {data['base']['profit']} ₽\nОптимистичный: {data['optimistic']['profit']} ₽")
    elif q.data.startswith("approve:") or q.data.startswith("reject:"):
        action, approval_id = q.data.split(":", 1)
        result = await api("POST", f"/api/approvals/{approval_id}/{action}", json={"note": "Решение принято владельцем в Telegram"})
        await update.effective_message.reply_text(f"Подтверждение #{approval_id}: {result['status']}")
    elif q.data in {"ceo", "meta_brain"}:
        data = await api("POST", "/api/tasks", json={"title": f"{q.data} on-demand review", "agent_type": q.data})
        await update.effective_message.reply_text(f"Задача #{data['id']} поставлена агенту {q.data}.")
    elif q.data == "agents": await dashboard(update, context)
    elif q.data == "outreach": await update.effective_message.reply_text("📣 Рассылки управляются через /docs. Отправка требует настроенного SMTP и owner approval для bulk-кампаний.")


def build_application() -> Application:
    if not settings.telegram_bot_token: raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    if not settings.owner_telegram_id: raise RuntimeError("OWNER_TELEGRAM_ID is empty")
    application = Application.builder().token(settings.telegram_bot_token).build()
    for command, handler in [("start", start), ("dashboard", dashboard), ("tasks", tasks), ("decisions", decisions), ("addtask", addtask)]: application.add_handler(CommandHandler(command, handler))
    application.add_handler(CallbackQueryHandler(callback))
    return application


def main():
    build_application().run_polling(drop_pending_updates=True)


if __name__ == "__main__": main()
