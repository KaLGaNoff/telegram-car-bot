import os
import json
import time
import logging
import warnings
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ConversationHandler, ContextTypes
)
import gspread
from gspread_formatting import CellFormat, TextFormat, Borders, format_cell_range
import telegram
from logging.handlers import TimedRotatingFileHandler
from quart import Quart, request, Response
import urllib.request
import asyncio

# Придушення PTBUserWarning
warnings.filterwarnings("ignore", category=telegram.warnings.PTBUserWarning)

# Налаштування логування
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Фільтр для ігнорування favicon-запитів
class FaviconFilter(logging.Filter):
    def filter(self, record):
        return '/favicon' not in record.getMessage()

log_handler = TimedRotatingFileHandler(
    filename="bot.log",
    when="midnight",
    interval=1,
    backupCount=7
)
log_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
))
log_handler.addFilter(FaviconFilter())
logger.addHandler(log_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
))
console_handler.addFilter(FaviconFilter())
logger.addHandler(console_handler)

logger.info(f"Версія python-telegram-bot: {telegram.__version__}")

# Змінні оточення
OWNER_ID = 270380991
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook")

if not all([TELEGRAM_TOKEN, GOOGLE_SHEET_ID, SERVICE_ACCOUNT_JSON, WEBHOOK_URL]):
    logger.error("Відсутні обов’язкові змінні оточення")
    raise ValueError("Відсутні обов’язкові змінні оточення")

# Перевірка Telegram API
try:
    response = urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe")
    logger.info(f"Telegram API check: {response.getcode()} OK")
except Exception as e:
    logger.error(f"Помилка перевірки Telegram API: {e}")
    raise

# Ініціалізація Google Sheets
try:
    credentials = json.loads(SERVICE_ACCOUNT_JSON)
    client = gspread.service_account_from_dict(credentials)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
except Exception as e:
    logger.error(f"Помилка ініціалізації Google Sheets: {e}")
    raise

# Кешування даних таблиці
sheet_cache = None

def update_sheet_cache():
    global sheet_cache
    try:
        start_time = time.time()
        sheet_cache = sheet.get_all_values()
        logger.info(f"Кеш таблиці оновлено за {time.time() - start_time:.3f} сек")
    except Exception as e:
        logger.error(f"Помилка оновлення кешу: {e}")
        sheet_cache = []

update_sheet_cache()

# Стани для ConversationHandler
WAITING_FOR_ODOMETER, WAITING_FOR_DISTRIBUTION, CONFIRMATION = range(3)
user_data_store = {}

# Quart сервер
app = Quart(__name__)
telegram_app = None

async def init_telegram_app():
    global telegram_app
    try:
        telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        logger.info("Telegram Application ініціалізовано")

        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(handle_button)],
            states={
                WAITING_FOR_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_odometer)],
                WAITING_FOR_DISTRIBUTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_distribution)],
                CONFIRMATION: [CallbackQueryHandler(handle_confirmation)]
            },
            fallbacks=[CallbackQueryHandler(cancel, pattern="^cancel$")],
            per_user=True,
            per_chat=True,
            per_message=False
        )

        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CommandHandler("stats", stats))
        telegram_app.add_handler(conv_handler)
        logger.info("Обробники команд додано")

        # Перевірка токена
        bot_info = await telegram_app.bot.get_me()
        logger.info(f"Бот успішно ініціалізовано: {bot_info.username}")

        # Налаштування вебхука
        try:
            await telegram_app.bot.set_webhook(url=WEBHOOK_URL)
            logger.info(f"Вебхук успішно встановлено: {WEBHOOK_URL}")
        except Exception as e:
            logger.error(f"Помилка встановлення вебхука: {e}")
            raise
    except Exception as e:
        logger.error(f"Помилка ініціалізації Telegram Application: {e}", exc_info=True)
        raise

@app.before_serving
async def startup():
    await init_telegram_app()

@app.route('/')
async def ping():
    logger.debug(f"Отримано пінг на / о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    try:
        response = urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe")
        logger.info(f"Flask ping: Telegram API responded with {response.getcode()}")
        return "Bot is alive", 200
    except Exception as e:
        logger.error(f"Flask ping: Telegram API error: {e}")
        return "Bot is alive, but Telegram API failed", 200

@app.route('/webhook', methods=['POST'])
async def webhook():
    try:
        logger.info(f"Отримано вебхук-запит о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        if telegram_app is None:
            logger.error("Telegram Application не ініціалізовано")
            return Response(status=500)
        json_data = await request.get_json(force=True)
        if not json_data:
            logger.error("JSON дані не отримані")
            return Response(status=400)
        logger.debug(f"JSON дані: {json_data}")
        update = Update.de_json(json_data, telegram_app.bot)
        if update is None:
            logger.error("Не вдалося десеріалізувати оновлення")
            return Response(status=400)
        await telegram_app.process_update(update)
        logger.info("Вебхук оброблено успішно")
        return Response(status=200)
    except Exception as e:
        logger.error(f"Помилка обробки вебхука: {e}", exc_info=True)
        return Response(status=500)

@app.route('/favicon.ico')
@app.route('/favicon.png')
async def favicon():
    return Response(status=204)

if __name__ == "__main__":
    logger.info(f"🚀 Бот запущено о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
