import os
import json
import time
import logging
import warnings
import gspread
from datetime import datetime, timedelta
import pytz
import threading
import asyncio
from queue import Queue
import requests
from flask import Flask, request

# Придушення PTBUserWarning
warnings.filterwarnings("ignore", category=UserWarning)

# Налаштування логування
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class FaviconFilter(logging.Filter):
    def filter(self, record):
        return '/favicon' not in record.getMessage()

log_handler = logging.StreamHandler()
log_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
))
log_handler.addFilter(FaviconFilter())
logger.addHandler(log_handler)

logger.info("Бот запускається...")

# Змінні оточення
OWNER_ID = 270380991
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
RENDER_PORT = os.getenv("PORT", "10000")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not all([TELEGRAM_TOKEN, GOOGLE_SHEET_ID, SERVICE_ACCOUNT_JSON, WEBHOOK_URL]):
    logger.error("Відсутні обов'язкові змінні оточення")
    raise ValueError("Відсутні обов'язкові змінні оточення")

# Ініціалізація Google Sheets
try:
    credentials = json.loads(SERVICE_ACCOUNT_JSON)
    client = gspread.service_account_from_dict(credentials)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    logger.info("Успішно підключено до Google Sheets")
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

# Черга для оновлень
update_queue = Queue()

# Створюємо Flask додаток
app = Flask(__name__)

# Імпортуємо Telegram компоненти після ініціалізації Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# Створюємо Application для бота
application = Application.builder().token(TELEGRAM_TOKEN).build()

# [ВСТАВТЕ ТУТ ВСІ ФУНКЦІЇ ОБРОБКИ КОМАНД ТА ПОВІДОМЛЕНЬ З ПОПЕРЕДНЬОГО КОДУ]
# Тут мають бути всі функції: start, stats, handle_button, handle_odometer, 
# handle_distribution, handle_confirmation, cancel тощо

# Додаємо обробники до application
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

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(conv_handler)

# Маршрут для вебхука
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        update = Update.de_json(request.get_json(), application.bot)
        update_queue.put(update)
        logger.info(f"Отримано оновлення: {update.update_id}")
        return 'ok'
    except Exception as e:
        logger.error(f"Помилка обробки вебхука: {e}")
        return 'error', 500

# Маршрут для health check
@app.route('/health')
def health():
    return 'OK'

# Обробник для favicon
@app.route('/favicon.ico')
def favicon():
    return '', 204

# Головна сторінка
@app.route('/')
def index():
    return 'Telegram Bot is running!'

def set_webhook():
    try:
        # Видаляємо будь-які кінцеві слеші з WEBHOOK_URL
        webhook_url = WEBHOOK_URL.rstrip('/') + '/webhook'
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
        response = requests.post(url, data={'url': webhook_url})
        if response.status_code == 200:
            logger.info(f"Вебхук встановлено: {webhook_url}")
        else:
            logger.error(f"Помилка встановлення вебхука: {response.text}")
    except Exception as e:
        logger.error(f"Помилка при спробі встановлення вебхука: {e}")

async def process_updates():
    while True:
        if not update_queue.empty():
            update = update_queue.get()
            try:
                await application.process_update(update)
            except Exception as e:
                logger.error(f"Помилка обробки оновлення: {e}")
        await asyncio.sleep(0.1)

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.start())
    loop.create_task(process_updates())
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(application.stop())
        loop.close()

if __name__ == '__main__':
    # Встановлюємо вебхук
    set_webhook()
    
    # Запускаємо бота в окремому потоці
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    
    # Запускаємо Flask
    app.run(host='0.0.0.0', port=int(RENDER_PORT), debug=False)
