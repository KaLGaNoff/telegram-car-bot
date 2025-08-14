import os
import json
import gspread
from datetime import datetime
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ConversationHandler, ContextTypes
)
from gspread_formatting import CellFormat, TextFormat, Borders, format_cell_range

# === Налаштування ===
OWNER_ID = 270380991
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

if not TELEGRAM_TOKEN or not GOOGLE_SHEET_ID or not SERVICE_ACCOUNT_JSON:
    raise RuntimeError("❌ Не знайдено змінні середовища TELEGRAM_TOKEN, GOOGLE_SHEET_ID або SERVICE_ACCOUNT_JSON")

credentials = json.loads(SERVICE_ACCOUNT_JSON)
client = gspread.service_account_from_dict(credentials)
sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

WAITING_FOR_ODOMETER, WAITING_FOR_DISTRIBUTION, CONFIRMATION = range(3)
user_data_store = {}

# === Логіка бота ===
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
    query = update.callback_query
    await query.answer()

    if query.data == "add":
        await query.edit_message_text("📍 Введи показник одометра:")
        return WAITING_FOR_ODOMETER

    elif query.data == "delete":
        all_rows = sheet.get_all_values()
        if len(all_rows) > 1:
            sheet.delete_rows(len(all_rows))
            await query.edit_message_text("✅ Останній запис видалено.")
        else:
            await query.edit_message_text("⚠️ Немає даних для видалення.")
        return ConversationHandler.END

    elif query.data == "last":
        all_rows = sheet.get_all_values()
        if len(all_rows) > 1:
            last = all_rows[-1]
            await query.edit_message_text(f"🧾 Останній запис:\n{last}")
        else:
            await query.edit_message_text("⚠️ Немає записів.")
        return ConversationHandler.END

    elif query.data == "help":
        await query.edit_message_text("ℹ️ Бот для обліку пробігу авто та витрат пального.")
        return ConversationHandler.END

async def handle_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        odometer = int(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ Введи число!")
        return WAITING_FOR_ODOMETER

    user_data_store[update.effective_user.id] = {"odometer": odometer}
    await update.message.reply_text("🚗 Введи розподіл (місто/район/траса) у форматі: км_місто км_район км_траса")
    return WAITING_FOR_DISTRIBUTION

async def handle_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        await update.message.reply_text("❌ Введи три числа: км_місто км_район км_траса")
        return WAITING_FOR_DISTRIBUTION

    city_km, district_km, highway_km = map(int, parts)
    total_km = city_km + district_km + highway_km
    data = user_data_store.get(update.effective_user.id, {})
    prev_rows = sheet.get_all_values()
    prev_odometer = int(prev_rows[-1][1]) if len(prev_rows) > 1 else 0
    diff = data["odometer"] - prev_odometer

    data.update({
        "city_km": city_km,
        "district_km": district_km,
        "highway_km": highway_km,
        "diff": diff,
        "city_exact": round(city_km * 0.05, 4),
        "city_rounded": round(city_km * 0.05),
        "district_exact": round(district_km * 0.05, 4),
        "district_rounded": round(district_km * 0.05),
        "highway_exact": round(highway_km * 0.05, 4),
        "highway_rounded": round(highway_km * 0.05),
        "total_exact": round(total_km * 0.05, 4),
        "total_rounded": round(total_km * 0.05)
    })

    user_data_store[update.effective_user.id] = data
    keyboard = [
        [InlineKeyboardButton("✅ Так", callback_data="confirm_yes"),
         InlineKeyboardButton("❌ Ні", callback_data="confirm_no")]
    ]
    await update.message.reply_text(f"📊 Підтвердити запис?\n\nOdometer: {data['odometer']}\nПробіг: {diff} км", reply_markup=InlineKeyboardMarkup(keyboard))
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
    today = datetime.now().strftime("%d.%m.%Y")
    row = [
        today,
        str(data["odometer"]),
        str(data["diff"]),
        str(data["city_km"]),
        str(data["city_exact"]).replace('.', ','),
        str(data["city_rounded"]),
        str(data["district_km"]),
        str(data["district_exact"]).replace('.', ','),
        str(data["district_rounded"]),
        str(data["highway_km"]),
        str(data["highway_exact"]).replace('.', ','),
        str(data["highway_rounded"]),
        str(data["total_exact"]).replace('.', ','),
        str(data["total_rounded"])
    ]
    sheet.append_row(row)

    # Форматування комірок
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

# === Telegram App ===
telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

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

# === FastAPI для Render ===
app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Bot is running"}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

# Локальний запуск
if __name__ == "__main__":
    import asyncio
    async def main():
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        await telegram_app.updater.idle()
    asyncio.run(main())
