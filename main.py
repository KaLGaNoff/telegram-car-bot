import os
import logging
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler

import gspread
from google.oauth2.service_account import Credentials
from gspread_formatting import CellFormat, TextFormat, Borders, format_cell_range

# ====== Логування ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ====== ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # наприклад, https://your-app.onrender.com/webhook
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не знайдено у змінних середовища")
if not WEBHOOK_URL:
    raise RuntimeError("❌ WEBHOOK_URL не знайдено у змінних середовища")

# ====== Ініціалізація бота одразу ======
application = Application.builder().token(BOT_TOKEN).build()

# ====== Google Sheets ======
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open("Назва_Твоєї_Таблиці").sheet1

# ====== Функції для Telegram ======
async def start(update: Update, context):
    await update.message.reply_text("✅ Бот працює!")

# Приклад обробки повідомлень
async def echo(update: Update, context):
    await update.message.reply_text(f"Ви написали: {update.message.text}")

# ====== Додаємо хендлери ======
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

# ====== FastAPI ======
app = FastAPI()

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Стартуємо та ставимо вебхук...")
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info(f"🌐 Вебхук встановлено: {WEBHOOK_URL}/webhook")

@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "Bot is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
