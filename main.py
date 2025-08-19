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
from gspread_formatting import CellFormat, TextFormat, Borders, format_cell_range

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

def safe_float_conversion(value, default=0):
    """Безпечне перетворення в float з обробкою помилок"""
    if not value or value in ['#VALUE!', '#ERROR!', '']:
        return default
    try:
        return float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        return default

def format_new_row(row_index):
    """Форматує новий рядок у таблиці"""
    try:
        cell_format = CellFormat(
            horizontalAlignment='CENTER',
            textFormat=TextFormat(bold=False, fontSize=10),
            borders=Borders(
                top={'style': 'SOLID', 'width': 1},
                bottom={'style': 'SOLID', 'width': 1},
                left={'style': 'SOLID', 'width': 1},
                right={'style': 'SOLID', 'width': 1}
            )
        )
        format_cell_range(sheet, f"A{row_index}:N{row_index}", cell_format)
        logger.info(f"Відформатовано рядок {row_index}")
    except Exception as e:
        logger.error(f"Помилка форматування рядка: {e}")

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
            if not date_str or date_str in ['#VALUE!', '#ERROR!']:
                continue
                
            days.add(date_str)
            
            # Безпечне отримання значень
            total_distance += safe_float_conversion(row[2])
            city_km += safe_float_conversion(row[3])
            district_km += safe_float_conversion(row[6])
            highway_km += safe_float_conversion(row[9])
            city_fuel += safe_float_conversion(row[4])
            district_fuel += safe_float_conversion(row[7])
            highway_fuel += safe_float_conversion(row[10])
        except (ValueError, IndexError) as e:
            logger.warning(f"Помилка обробки рядка {row}: {e}")
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
    if update.message:
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
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        return

    stats_data = calculate_statistics()
    if not stats_data:
        response = "📊 *Ще немає даних для статистики*"
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(response, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.edit_message_text(response, parse_mode="Markdown")
        return

    try:
        avg_daily = stats_data['total_distance'] / stats_data['days_count'] if stats_data['days_count'] else 0
        
        stats_text = (
            f"📊 *Детальна статистика*\n\n"
            f"📏 *Загальний пробіг:* {stats_data['total_distance']:.0f} км\n"
            f"📅 *Днів з записами:* {stats_data['days_count']}\n"
            f"📈 *Середньодобовий пробіг:* {avg_daily:.1f} км\n\n"
            f"⛽ *Розподіл за типами доріг:*\n"
            f"🏙 *Місто:* {stats_data['city_km']:.0f} км ({stats_data['city_percent']:.1f}%) {generate_progress_bar(stats_data['city_percent'])}\n"
            f"🌳 *Район:* {stats_data['district_km']:.0f} км ({stats_data['district_percent']:.1f}%) {generate_progress_bar(stats_data['district_percent'])}\n"
            f"🛣 *Траса:* {stats_data['highway_km']:.0f} км ({stats_data['highway_percent']:.1f}%) {generate_progress_bar(stats_data['highway_percent'])}\n\n"
            f"🔋 *Витрати палива:*\n"
            f"• Місто: {stats_data['city_fuel']:.1f} л\n"
            f"• Район: {stats_data['district_fuel']:.1f} л\n"
            f"• Траса: {stats_data['highway_fuel']:.1f} л\n"
            f"• Загалом: {stats_data['city_fuel'] + stats_data['district_fuel'] + stats_data['highway_fuel']:.1f} л"
        )
        
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(stats_text, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.edit_message_text(stats_text, parse_mode="Markdown")
            
    except Exception as e:
        error_msg = "❌ *Помилка при отриманні статистики*"
        logger.error(f"Помилка статистики: {e}")
        if hasattr(update, 'message') and update.message:
            await update.message.reply_text(error_msg, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.edit_message_text(error_msg, parse_mode="Markdown")

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
                if not row[0] or row[0] in ['#VALUE!', '#ERROR!']:
                    continue
                    
                row_date = datetime.strptime(row[0], "%d.%m.%Y").replace(tzinfo=eest)
                if row_date >= month_ago:
                    monthly_distance += safe_float_conversion(row[2])
                    monthly_fuel += safe_float_conversion(row[12])
                    days_with_data += 1
            except (ValueError, IndexError):
                continue
        
        avg_consumption = (monthly_fuel / monthly_distance * 100) if monthly_distance else 0
        
        report_text = (
            f"📋 *Звіт за останні 30 днів*\n\n"
            f"📅 Період: {month_ago.strftime('%d.%m')} - {today.strftime('%d.%m.%Y')}\n"
            f"📊 Загальний пробіг: {monthly_distance:.0f} км\n"
            f"⛽ Витрачено палива: {monthly_fuel:.1f} л\n"
            f"📈 Середня витрата: {avg_consumption:.1f} л/100км\n"
            f"📅 Днів з поїздками: {days_with_data}\n\n"
            f"🏆 *Показники ефективності:*\n"
            f"• Щоденний пробіг: {monthly_distance/30:.1f} км/день\n"
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
        
        # Безпечне отримання значень
        last_odo = safe_float_conversion(last_row[1])
        last_distance = safe_float_conversion(last_row[2])
        last_fuel = safe_float_conversion(last_row[12])
        city_km = safe_float_conversion(last_row[3])
        district_km = safe_float_conversion(last_row[6])
        highway_km = safe_float_conversion(last_row[9])
        
        last_trip_text = (
            f"🧾 *Остання поїздка*\n\n"
            f"📅 Дата: {last_row[0] if last_row[0] not in ['#VALUE!', '#ERROR!'] else 'Невідомо'}\n"
            f"📏 Одометр: {last_odo:.0f} км\n"
            f"🔄 Подолано: {last_distance:.0f} км\n"
            f"⛽ Витрачено: {last_fuel:.1f} л\n\n"
            f"🛣 *Розподіл:*\n"
            f"• Місто: {city_km:.0f} км\n"
            f"• Район: {district_km:.0f} км\n"
            f"• Траса: {highway_km:.0f} км\n"
        )
        
        if prev_row:
            try:
                prev_odo = safe_float_conversion(prev_row[1])
                efficiency = "🟢 Краще" if last_distance > (safe_float_conversion(prev_row[2]) if len(prev_row) > 2 else 0) else "🟡 Стабільно"
                last_trip_text += f"\n📊 *Порівняння:* {efficiency}"
            except (ValueError, IndexError):
                pass
                
        await query.edit_message_text(last_trip_text, parse_mode="Markdown")
        
    except Exception as e:
        await query.edit_message_text("❌ Помилка отримання даних")
        logger.error(f"Помилка останнього запису: {e}")

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    logger.info(f"Отримано callback: {query.data} від користувача {query.from_user.id}")

    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до кнопки: {query.from_user.id}")
        return

    if query.data == "add":
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        last_odo = safe_float_conversion(sheet_cache[-1][1]) if len(sheet_cache) >= 2 else None
        last_odo_text = f"📍 *Твій останній одометр*: {last_odo:.0f}" if last_odo else "📍 *Це твій перший запис!*"
        await query.edit_message_text(
            f"{last_odo_text}\n\n📏 *Введи поточний одометр* (наприклад, `53200`):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_ODOMETER

    elif query.data == "delete":
        if sheet_cache:
            try:
                start_time = time.time()
                sheet.delete_rows(len(sheet_cache))
                update_sheet_cache()
                await query.edit_message_text("🗑 *Останній запис видалено!* ✅")
                logger.info(f"Користувач {query.from_user.id} видалив останній запис за {time.time() - start_time:.3f} сек")
            except Exception as e:
                await query.edit_message_text(f"⚠️ *Помилка видалення*: {e}", parse_mode="Markdown")
                logger.error(f"Помилка видалення запису: {e}")
        else:
            await query.edit_message_text("📈 *Таблиця порожня.* 😕", parse_mode="Markdown")

    elif query.data == "report":
        await handle_report(update, context)

    elif query.data == "last":
        await handle_last(update, context)

    elif query.data == "stats":
        await stats(update, context)

    elif query.data == "reset":
        user_data_store.pop(query.from_user.id, None)
        await query.edit_message_text("♻️ *Дані скинуто!* ✅", parse_mode="Markdown")
        logger.info(f"Користувач {query.from_user.id} скинув дані")

    elif query.data == "help":
        await query.edit_message_text(
            "ℹ️ *Як користуватися ботом*:\n"
            "1. Натисни 🟢 *Додати пробіг*.\n"
            "2. Введи одометр (наприклад, `53200`).\n"
            "3. Вкажи розподіл: *місто* 50 *район* 30 *траса* 6.\n"
            "4. Сума має відповідати різниці одометра.\n"
            "📈 *Статистика* покаже твої поїздки!",
            parse_mode="Markdown"
        )

    elif query.data == "retry_odometer":
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        last_odo = safe_float_conversion(sheet_cache[-1][1]) if len(sheet_cache) >= 2 else None
        last_odo_text = f"📍 *Твій останній одометр*: {last_odo:.0f}" if last_odo else "📍 *Це твій перший запис!*"
        await query.edit_message_text(
            f"{last_odo_text}\n\n📏 *Введи поточний одометр* (наприклад, `53200`):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_ODOMETER

    elif query.data == "retry_distribution":
        user_id = query.from_user.id
        data = user_data_store.get(user_id, {})
        if not data:
            await query.edit_message_text("⚠️ *Дані загублено. Почни знову.*", parse_mode="Markdown")
            logger.warning(f"Дані загублено для користувача {user_id}")
            return ConversationHandler.END
        prev_odo = safe_float_conversion(sheet_cache[-1][1]) if len(sheet_cache) >= 2 else 0
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        await query.edit_message_text(
            f"📏 *Попередній одометр*: {prev_odo:.0f}\n"
            f"📍 *Поточний одометр*: {data['odometer']:.0f}\n"
            f"🔄 *Пробіг за період*: {data['diff']:.0f} км\n\n"
            f"🛣 *Введи розподіл пробігу* \n(наприклад, *місто* {int(data['diff']/3)} *район* {int(data['diff']/3)} *траса* {int(data['diff']/3)}):\n\n"
            f"ℹ️ Сума має дорівнювати {data['diff']:.0f} км.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_DISTRIBUTION

async def handle_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Отримано введення одометра від користувача {update.effective_user.id}: {update.message.text}")
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до одометра: {update.effective_user.id}")
        return ConversationHandler.END

    text = update.message.text.strip().replace(",", ".")
    if not text.replace(".", "", 1).isdigit():
        keyboard = [
            [InlineKeyboardButton("🔄 Спробувати ще", callback_data="retry_odometer"),
             InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]
        await update.message.reply_text(
            "😅 *Введи число* (наприклад, `53200`):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_ODOMETER

    odometer = int(float(text))
    rows = sheet_cache

    if len(rows) >= 2:
        prev_odo = safe_float_conversion(rows[-1][1])
    else:
        prev_odo = 0

    diff = odometer - prev_odo
    if diff <= 0:
        keyboard = [
            [InlineKeyboardButton("🔄 Спробувати ще", callback_data="retry_odometer"),
             InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]
        await update.message.reply_text(
            f"❗️ *Одометр має бути більший за попередній* ({prev_odo:.0f}).",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_ODOMETER

    user_data_store[update.effective_user.id] = {
        "odometer": odometer,
        "diff": diff
    }

    keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
    await update.message.reply_text(
        f"📏 *Попередній одометр*: {prev_odo:.0f}\n"
        f"📍 *Поточний одометр*: {odometer}\n"
        f"🔄 *Пробіг за період*: {diff:.0f} км\n\n"
        f"🛣 *Введи розподіл пробігу* \n(наприклад, *місто* {int(diff/3)} *район* {int(diff/3)} *траса* {int(diff/3)}):\n\n"
        f"ℹ️ Сума має дорівнювати {diff:.0f} км.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    logger.info(f"Користувач {update.effective_user.id} ввів одометр: {odometer}")
    return WAITING_FOR_DISTRIBUTION

async def handle_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Отримано розподіл пробігу від користувача {update.effective_user.id}: {update.message.text}")
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до розподілу: {update.effective_user.id}")
        return ConversationHandler.END

    text = update.message.text.lower()
    user_id = update.effective_user.id
    data = user_data_store.get(user_id, {})

    if not data:
        await update.message.reply_text("⚠️ *Дані загублено. Почни знову.*", parse_mode="Markdown")
        logger.warning(f"Дані загублено для користувача {user_id}")
        return ConversationHandler.END

    city_km = district_km = highway_km = 0
    try:
        for word in text.split():
            if "міст" in word:
                next_value = text.split(word)[1].strip().split()[0]
                city_km = safe_float_conversion(next_value)
            elif "район" in word:
                next_value = text.split(word)[1].strip().split()[0]
                district_km = safe_float_conversion(next_value)
            elif "трас" in word:
                next_value = text.split(word)[1].strip().split()[0]
                highway_km = safe_float_conversion(next_value)
    except (IndexError, ValueError):
        keyboard = [
            [InlineKeyboardButton("🔄 Спробувати ще", callback_data="retry_distribution"),
             InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]
        await update.message.reply_text(
            f"😅 *Неправильний формат.* Введи, наприклад: *місто* {int(data['diff']/3)} *район* {int(data['diff']/3)} *траса* {int(data['diff']/3)}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_DISTRIBUTION

    total_entered = city_km + district_km + highway_km
    if abs(total_entered - data["diff"]) > 1:
        keyboard = [
            [InlineKeyboardButton("🔄 Спробувати ще", callback_data="retry_distribution"),
             InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]
        await update.message.reply_text(
            f"⚠️ *Сума ({total_entered}) не збігається з пробігом ({data['diff']}).* Виправ.\n"
            f"Введи, наприклад: *місто* {int(data['diff']/3)} *район* {int(data['diff']/3)} *траса* {int(data['diff']/3)}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_DISTRIBUTION

    def calc(litres_per_100km, km):
        exact = round(km * litres_per_100km / 100, 4)
        rounded = round(exact)
        return exact, rounded

    c_exact, c_rounded = calc(11.66, city_km)
    d_exact, d_rounded = calc(11.17, district_km)
    h_exact, h_rounded = calc(10.19, highway_km)
    total_exact = round(c_exact + d_exact + h_exact, 4)
    total_rounded = round(total_exact)

    data.update({
        "city_km": city_km, "city_exact": c_exact, "city_rounded": c_rounded,
        "district_km": district_km, "district_exact": d_exact, "district_rounded": d_rounded,
        "highway_km": highway_km, "highway_exact": h_exact, "highway_rounded": h_rounded,
        "total_exact": total_exact, "total_rounded": total_rounded
    })
    user_data_store[user_id] = data

    text = (
        f"📋 *Новий запис*:\n"
        f"📏 *Одометр*: {data['odometer']} км\n"
        f"🔄 *Пробіг*: {data['diff']:.0f} км\n"
        f"🏙 *Місто*: {int(city_km)} км → {c_exact} л (≈ {c_rounded})\n"
        f"🌳 *Район*: {int(district_km)} км → {d_exact} л (≈ {d_rounded})\n"
        f"🛣 *Траса*: {int(highway_km)} км → {h_exact} л (≈ {h_rounded})\n"
        f"⛽ *Загалом*: {total_exact} л (≈ {total_rounded})\n\n"
        f"✅ *Зберегти запис?*"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Так", callback_data="confirm_yes"), InlineKeyboardButton("❌ Ні", callback_data="confirm_no")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ]
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    logger.info(f"Користувач {user_id} ввів розподіл: місто={city_km}, район={district_km}, траса={highway_km}")
    return CONFIRMATION

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.info(f"Отримано підтвердження: {query.data} від користувача {user_id}")

    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до підтвердження: {user_id}")
        return ConversationHandler.END

    if query.data == "confirm_no" or query.data == "cancel":
        user_data_store.pop(user_id, None)
        await query.edit_message_text("❌ *Скасовано.*", parse_mode="Markdown")
        logger.info(f"Користувач {user_id} скасував запис")
        return ConversationHandler.END

    data = user_data_store.pop(user_id, {})
    if not data:
        await query.edit_message_text("⚠️ *Дані не знайдено.*", parse_mode="Markdown")
        logger.warning(f"Дані не знайдено для користувача {user_id}")
        return ConversationHandler.END

    eest = pytz.timezone("Europe/Kiev")
    today = datetime.now(eest).strftime("%d.%m.%Y")
    logger.info(f"Поточна дата EEST: {today}")

    row = [
        today,
        str(int(data.get("odometer", 0))),
        str(int(data.get("diff", 0))),
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

    try:
        start_time = time.time()
        sheet.append_row(row)
        row_index = len(sheet_cache) + 1  # Новий рядок
        update_sheet_cache()
        
        # Форматуємо новий рядок
        format_new_row(row_index)
        
        await query.edit_message_text(
            f"✅ *Запис збережено!* 🎉\n\n"
            f"📅 {today} | 📏 {int(data['odometer'])} км | 🔄 {int(data['diff'])} км | ⛽ {data['total_exact']} л",
            parse_mode="Markdown"
        )
        logger.info(f"Користувач {user_id} зберіг запис: {row} за {time.time() - start_time:.3f} сек")
    except Exception as e:
        await query.edit_message_text(f"⚠️ *Помилка збереження*: {e}", parse_mode="Markdown")
        logger.error(f"Помилка збереження запису: {e}")
        return ConversationHandler.END

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.info(f"Отримано скасування від користувача {user_id}")
    user_data_store.pop(user_id, None)
    await query.edit_message_text("❌ *Операцію скасовано.*", parse_mode="Markdown")
    logger.info(f"Користувач {user_id} скасував операцію")
    return ConversationHandler.END

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
