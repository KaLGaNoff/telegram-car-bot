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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

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

# Створюємо Application для бота
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Допоміжні функції
def generate_progress_bar(percent, width=10):
    """Генерує текстову смугу прогресу"""
    filled = int(round(width * percent / 100))
    return "🟩" * filled + "⬜" * (width - filled)

def calculate_statistics():
    """Розраховує статистику на основі даних з таблиці"""
    if not sheet_cache or len(sheet_cache) <= 1:
        return None
    
    total_distance = 0
    city_km = district_km = highway_km = 0
    city_fuel = district_fuel = highway_fuel = 0
    days = set()
    
    for row in sheet_cache[1:]:
        try:
            date_str = row[0]
            days.add(date_str)
            
            if row[2]: total_distance += float(row[2])
            if row[3]: city_km += float(row[3])
            if row[6]: district_km += float(row[6])
            if row[9]: highway_km += float(row[9])
            if row[4]: city_fuel += float(row[4].replace(',', '.'))
            if row[7]: district_fuel += float(row[7].replace(',', '.'))
            if row[10]: highway_fuel += float(row[10].replace(',', '.'))
        except (ValueError, IndexError):
            continue
    
    total_km = city_km + district_km + highway_km
    city_percent = (city_km / total_km * 100) if total_km else 0
    district_percent = (district_km / total_km * 100) if total_km else 0
    highway_percent = (highway_km / total_km * 100) if total_km else 0
    
    return {
        'total_distance': total_distance,
        'city_km': city_km, 'city_percent': city_percent, 'city_fuel': city_fuel,
        'district_km': district_km, 'district_percent': district_percent, 'district_fuel': district_fuel,
        'highway_km': highway_km, 'highway_percent': highway_percent, 'highway_fuel': highway_fuel,
        'days_count': len(days)
    }

# Функції обробники
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Отримано команду /start від користувача {update.effective_user.id}")
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ *У тебе немає доступу до цього бота.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ: {update.effective_user.id}")
        return

    keyboard = [
        [InlineKeyboardButton("🟢 Додати пробіг", callback_data="add"), InlineKeyboardButton("🔴 Видалити", callback_data="delete")],
        [InlineKeyboardButton("📊 Звіт за місяць", callback_data="report"), InlineKeyboardButton("🧾 Остання поїздка", callback_data="last")],
        [InlineKeyboardButton("📈 Статистика", callback_data="stats"), InlineKeyboardButton("♻️ Скинути", callback_data="reset")],
        [InlineKeyboardButton("ℹ️ Допомога", callback_data="help")]
    ]
    await update.message.reply_text(
        "🚗 *Вітаю у твоєму авто-боті!* 👋\nОбери дію нижче:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    logger.info(f"Користувач {update.effective_user.id} запустив бота")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка команди /stats та кнопки статистики"""
    logger.info(f"Отримано запит на статистику від {update.effective_user.id}")
    
    if update.effective_user.id != OWNER_ID:
        if hasattr(update, 'message'):
            await update.message.reply_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        else:
            await update.callback_query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        return

    stats_data = calculate_statistics()
    if not stats_data:
        response = "📊 *Ще немає даних для статистики*"
        if hasattr(update, 'message'):
            await update.message.reply_text(response, parse_mode="Markdown")
        else:
            await update.callback_query.edit_message_text(response, parse_mode="Markdown")
        return

    try:
        avg_daily = stats_data['total_distance'] / stats_data['days_count'] if stats_data['days_count'] else 0
        
        stats_text = (
            f"📊 *Детальна статистика*\n\n"
            f"📏 *Загальний пробіг:* {stats_data['total_distance']:.1f} км\n"
            f"📅 *Днів з записами:* {stats_data['days_count']}\n"
            f"📈 *Середньодобовий пробіг:* {avg_daily:.1f} км\n\n"
            f"⛽ *Розподіл за типами доріг:*\n"
            f"🏙 *Місто:* {stats_data['city_km']:.1f} км ({stats_data['city_percent']:.1f}%) {generate_progress_bar(stats_data['city_percent'])}\n"
            f"🌳 *Район:* {stats_data['district_km']:.1f} км ({stats_data['district_percent']:.1f}%) {generate_progress_bar(stats_data['district_percent'])}\n"
            f"🛣 *Траса:* {stats_data['highway_km']:.1f} км ({stats_data['highway_percent']:.1f}%) {generate_progress_bar(stats_data['highway_percent'])}\n\n"
            f"🔋 *Витрати палива:*\n"
            f"• Місто: {stats_data['city_fuel']:.1f} л\n"
            f"• Район: {stats_data['district_fuel']:.1f} л\n"
            f"• Траса: {stats_data['highway_fuel']:.1f} л\n"
            f"• Загалом: {stats_data['city_fuel'] + stats_data['district_fuel'] + stats_data['highway_fuel']:.1f} л"
        )
        
        if hasattr(update, 'message'):
            await update.message.reply_text(stats_text, parse_mode="Markdown")
        else:
            await update.callback_query.edit_message_text(stats_text, parse_mode="Markdown")
            
    except Exception as e:
        error_msg = "❌ *Помилка при отриманні статистики*"
        if hasattr(update, 'message'):
            await update.message.reply_text(error_msg, parse_mode="Markdown")
        else:
            await update.callback_query.edit_message_text(error_msg, parse_mode="Markdown")
        logger.error(f"Помилка статистики: {e}")

async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки звіту за місяць"""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        return

    try:
        # Аналіз даних за останні 30 днів
        eest = pytz.timezone("Europe/Kiev")
        today = datetime.now(eest)
        month_ago = today - timedelta(days=30)
        
        monthly_distance = 0
        monthly_fuel = 0
        days_with_data = 0
        
        for row in sheet_cache[1:]:
            try:
                row_date = datetime.strptime(row[0], "%d.%m.%Y").replace(tzinfo=eest)
                if row_date >= month_ago:
                    if row[2]: monthly_distance += float(row[2])
                    if row[12]: monthly_fuel += float(row[12].replace(',', '.'))
                    days_with_data += 1
            except (ValueError, IndexError):
                continue
        
        avg_consumption = (monthly_fuel / monthly_distance * 100) if monthly_distance else 0
        
        report_text = (
            f"📋 *Звіт за останні 30 днів*\n\n"
            f"📅 Період: {month_ago.strftime('%d.%m')} - {today.strftime('%d.%m.%Y')}\n"
            f"📊 Загальний пробіг: {monthly_distance:.1f} км\n"
            f"⛽ Витрачено палива: {monthly_fuel:.1f} л\n"
            f"📈 Середня витрата: {avg_consumption:.1f} л/100км\n"
            f"📅 Днів з поїздками: {days_with_data}\n\n"
            f"🏆 *Показники ефективності:*\n"
            f"• Щоденний пробіг: {monthly_distance/30:.1f} км/день\n"
            f"• Витрати на паливо: ~{monthly_fuel * 54:.0f} грн\n"
            f"• Ефективність: {'🟢' if avg_consumption < 11 else '🟡' if avg_consumption < 13 else '🔴'}"
        )
        
        await query.edit_message_text(report_text, parse_mode="Markdown")
        
    except Exception as e:
        await query.edit_message_text("❌ Помилка формування звіту")
        logger.error(f"Помилка звіту: {e}")

async def handle_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка кнопки останньої поїздки"""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        return

    try:
        if not sheet_cache or len(sheet_cache) < 2:
            await query.edit_message_text("📭 *Немає записів про поїздки*")
            return
            
        last_row = sheet_cache[-1]
        prev_row = sheet_cache[-2] if len(sheet_cache) >= 3 else None
        
        last_trip_text = (
            f"🧾 *Остання поїздка*\n\n"
            f"📅 Дата: {last_row[0]}\n"
            f"📏 Одометр: {last_row[1]} км\n"
            f"🔄 Подолано: {last_row[2]} км\n"
            f"⛽ Витрачено: {last_row[12]} л\n\n"
            f"🛣 *Розподіл:*\n"
            f"• Місто: {last_row[3]} км\n"
            f"• Район: {last_row[6]} км\n"
            f"• Траса: {last_row[9]} км\n"
        )
        
        if prev_row:
            try:
                prev_odo = float(prev_row[1])
                last_odo = float(last_row[1])
                efficiency = "🟢 Краще" if (last_odo - prev_odo) > (float(prev_row[1]) - float(sheet_cache[-3][1])) else "🟡 Стабільно"
                last_trip_text += f"\n📊 *Порівняння:* {efficiency}"
            except (ValueError, IndexError):
                pass
                
        await query.edit_message_text(last_trip_text, parse_mode="Markdown")
        
    except Exception as e:
        await query.edit_message_text("❌ Помилка отримання даних")
        logger.error(f"Помилка останнього запису: {e}")

# Решта функцій (handle_button, handle_odometer, handle_distribution, handle_confirmation, cancel) залишаються незмінними
# [ВСТАВТЕ ТУТ РЕШТУ ФУНКЦІЙ З ПОПЕРЕДНЬОГО КОДУ]

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

# Маршрути Flask та решта коду залишаються незмінними
# [ВСТАВТЕ ТУТ РЕШТУ КОДУ З ПОПЕРЕДНЬОГО ВІДПОВІДІ]
