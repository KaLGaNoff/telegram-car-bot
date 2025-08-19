import os
import json
import logging
import asyncio
from fastapi import FastAPI, Request, Response
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz
import httpx

app = FastAPI()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-app-name.onrender.com/webhook")
bot = Bot(token=TOKEN)
application = Application.builder().token(TOKEN).build()

# Налаштування Google Sheets
def get_gspread_client():
    credentials = Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(credentials)

# Періодичний пінг для Render Free
async def keep_alive():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await client.get("https://your-app-name.onrender.com/health")
                logging.info("Пінг виконано для підтримки активності")
            except Exception as e:
                logging.error(f"Помилка пінгу: {e}")
            await asyncio.sleep(300)  # Кожні 5 хвилин

# Обробники команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущено! Команди: /add, /report")

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введіть пробіг (наприклад, 53200):")
    context.user_data["state"] = "awaiting_mileage"

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        data = sheet.get_all_records()
        if not data:
            await update.message.reply_text("Немає даних у таблиці.")
            return
        response = "Звіт:\n"
        for row in data:
            response += f"Дата: {row['date']}, Пробіг: {row['mileage']}, Місто: {row['city']}, Район: {row['district']}, Траса: {row['highway']}\n"
        await update.message.reply_text(response)
    except Exception as e:
        logging.error(f"Помилка в /report: {e}")
        await update.message.reply_text("Помилка при отриманні звіту.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    text = update.message.text
    chat_id = update.message.chat_id

    if state == "awaiting_mileage":
        try:
            mileage = int(text)
            context.user_data["mileage"] = mileage
            context.user_data["state"] = "awaiting_breakdown"
            await update.message.reply_text("Введіть розподіл (наприклад, city 50 district 30 highway 20):")
        except ValueError:
            await update.message.reply_text("Введіть коректний пробіг (ціле число)!")
    elif state == "awaiting_breakdown":
        try:
            parts = text.lower().split()
            if len(parts) != 6 or parts[0] not in ["city", "місто"] or parts[2] not in ["district", "район"] or parts[4] not in ["highway", "траса"]:
                raise ValueError("Неправильний формат. Приклад: city 50 district 30 highway 20")
            city = int(parts[1])
            district = int(parts[3])
            highway = int(parts[5])
            if city + district + highway != 100:
                raise ValueError("Сума (місто + район + траса) має бути 100%!")
            
            # Збереження в Google Sheets
            client = get_gspread_client()
            sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
            date = datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
            mileage = context.user_data["mileage"]
            sheet.append_row([date, mileage, city, district, highway])
            
            await update.message.reply_text(f"Додано: Пробіг {mileage}, Місто: {city}%, Район: {district}%, Траса: {highway}%")
            context.user_data.clear()
        except Exception as e:
            logging.error(f"Помилка в обробці розподілу: {e}")
            await update.message.reply_text(f"Помилка: {str(e)}. Спробуйте ще раз.")

# Реєстрація обробників
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("add", add))
application.add_handler(CommandHandler("report", report))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

@app.post("/webhook")
async def webhook(request: Request):
    update = Update.de_json(json.loads(await request.body()), bot)
    await application.process_update(update)
    return Response(status_code=200)

@app.get("/health")
async def health():
    return {"status": "ok"}

async def main():
    logging.info(f"Бот запущено о {datetime.now(pytz.UTC)}")
    await bot.set_webhook(url=WEBHOOK_URL)
    logging.info(f"Вебхук встановлено: {WEBHOOK_URL}")
    # Запускаємо пінг для Render Free
    asyncio.create_task(keep_alive())

if __name__ == "__main__":
    import uvicorn
    asyncio.run(main())
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
