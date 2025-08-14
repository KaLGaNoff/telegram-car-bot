import os
import json
import gspread
from datetime import datetime
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler, ContextTypes
)
from gspread_formatting import CellFormat, TextFormat, Borders, format_cell_range

# === Змінні з середовища ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

if not TELEGRAM_TOKEN or not GOOGLE_SHEET_ID or not SERVICE_ACCOUNT_JSON:
    raise RuntimeError("❌ Не знайдено одну або кілька змінних середовища")

OWNER_ID = 270380991

# === Google Sheets підключення ===
credentials = json.loads(SERVICE_ACCOUNT_JSON)
client = gspread.service_account_from_dict(credentials)
sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

# === Стан розмови ===
WAITING_FOR_ODOMETER, WAITING_FOR_DISTRIBUTION, CONFIRMATION = range(3)
user_data_store = {}

# === Хендлери ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У тебе немає доступу до цього бота.")
        return

    keyboard = [
        [InlineKeyboardButton("➕ Додати пробіг", callback_data="add")],
        [InlineKeyboardButton("🗑 Видалити останній запис", callback_data="delete")],
        [InlineKeyboardButton("📊 Звіт", callback_data="report")],
        [InlineKeyboardButton("🧾 Останній запис", callback_data="last")],
        [InlineKeyboardButton("♻️ Скинути", callback_data="reset")],
        [InlineKeyboardButton("ℹ️ Допомога", callback_data="help")]
    ]
    await update.message.reply_text("👋 Обери дію:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🔧 Обробка кнопки... (заглушка)")

async def handle_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📍 Введення одометра (заглушка)")
    return WAITING_FOR_DISTRIBUTION

async def handle_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📍 Введення розподілу (заглушка)")
    return CONFIRMATION

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "confirm_no":
        user_data_store.pop(user_id, None)
        await query.edit_message_text("❌ Скасовано.")
        return ConversationHandler.END

    data = user_data_store.pop(user_id, {})
    if not data:
        await query.edit_message_text("⚠️ Дані не знайдено.")
        return ConversationHandler.END

    today = datetime.now().strftime("%d.%m.%Y")
    row = [
        today,
        str(data.get("odometer", "")),
        str(data.get("diff", "")),
        str(int(data.get("city_km", 0))),
        str(data.get("city_exact", 0)).replace('.', ','),
        str(data.get("city_rounded", 0)),
        str(int(data.get("district_km", 0))),
        str(data.get("district_exact", 0)).replace('.', ','),
        str(data.get("district_rounded", 0)),
        str(int(data.get("highway_km", 0))),
        str(data.get("highway_exact", 0)).replace('.', ','),
        str(data.get("highway_rounded", 0)),
        str(data.get("total_exact", 0)).replace('.', ','),
        str(data.get("total_rounded", 0))
    ]
    sheet.append_row(row)

    row_index = len(sheet.get_all_values())
    cell_format = CellFormat(
        horizontalAlignment='CENTER',
        textFormat=TextFormat(bold=False),
        borders=Borders(
            top={'style': 'SOLID'},
            bottom={'style': 'SOLID'},
            left={'style': 'SOLID'},
            right={'style': 'SOLID'}
        )
    )
    format_cell_range(sheet, f"A{row_index}:N{row_index}", cell_format)

    await query.edit_message_text("✅ Запис збережено.")
    return ConversationHandler.END


# === Налаштування FastAPI + Telegram Webhook ===
app = FastAPI()
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(handle_button)],
    states={
        WAITING_FOR_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_odometer)],
        WAITING_FOR_DISTRIBUTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_distribution)],
        CONFIRMATION: [CallbackQueryHandler(handle_confirmation)]
    },
    fallbacks=[]
)

telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(conv_handler)

WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"

@app.on_event("startup")
async def on_startup():
    await telegram_app.bot.set_webhook(WEBHOOK_URL)

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "Bot is running"}
