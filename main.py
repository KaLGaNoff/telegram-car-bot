import os
import json
import time
import logging
import warnings
import gspread
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ConversationHandler, ContextTypes
)
from gspread_formatting import CellFormat, TextFormat, Borders, format_cell_range
import telegram
from logging.handlers import TimedRotatingFileHandler
import threading
import urllib.request
from flask import Flask, Response

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

if not all([TELEGRAM_TOKEN, GOOGLE_SHEET_ID, SERVICE_ACCOUNT_JSON]):
    logger.error("Відсутні обов’язкові змінні оточення")
    raise ValueError("Відсутні обов’язкові змінні оточення")

# Перевірка Telegram API
try:
    response = urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe")
    logger.info(f"Telegram API check: {response.getcode()} OK")
except Exception as e:
    logger.error(f"Помилка перевірки Telegram API: {e}")

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

# Flask сервер
flask_app = Flask(__name__)

@flask_app.route('/')
def ping():
    logger.debug(f"Отримано пінг на / о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    try:
        response = urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe")
        logger.info(f"Flask ping: Telegram API responded with {response.getcode()}")
        return "Bot is alive", 200
    except Exception as e:
        logger.error(f"Flask ping: Telegram API error: {e}")
        return "Bot is alive, but Telegram API failed", 200

@flask_app.route('/favicon.ico')
@flask_app.route('/favicon.png')
def favicon():
    return Response(status=204)

def run_flask():
    logger.info("Запускаємо Flask сервер")
    try:
        flask_app.run(host='0.0.0.0', port=8080)
    except Exception as e:
        logger.error(f"Помилка Flask сервера: {e}")

# Запускаємо Flask у окремому потоці
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Отримано команду /start від користувача {update.effective_user.id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ *У тебе немає доступу до цього бота.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ: {update.effective_user.id}")
        return

    keyboard = [
        [InlineKeyboardButton("🟢 Додати пробіг", callback_data="add"), InlineKeyboardButton("🔴 Видалити", callback_data="delete")],
        [InlineKeyboardButton("📊 Звіт", callback_data="report"), InlineKeyboardButton("🧾 Останній", callback_data="last")],
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
    logger.info(f"Отримано команду /stats від користувача {update.effective_user.id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до /stats: {update.effective_user.id}")
        return

    if not sheet_cache:
        await update.message.reply_text("📈 *Таблиця порожня.* 😕", parse_mode="Markdown")
        logger.info(f"Користувач {update.effective_user.id} спробував переглянути статистику: таблиця порожня")
        return

    try:
        total_distance = 0
        city_km = district_km = highway_km = 0
        city_fuel = district_fuel = highway_fuel = 0
        days = set()
        last_7_days_distance = 0
        last_7_days_fuel = 0
        eest = pytz.timezone("Europe/Kiev")
        today = datetime.now(eest)
        seven_days_ago = today - timedelta(days=7)

        for row in sheet_cache[1:]:  # Пропускаємо заголовок
            date_str = row[0]
            try:
                row_date = datetime.strptime(date_str, "%d.%m.%Y").replace(tzinfo=eest)
                days.add(date_str)
                distance = float(row[2]) if row[2] else 0
                total_distance += distance
                city_km += float(row[3]) if row[3] else 0
                district_km += float(row[6]) if row[6] else 0
                highway_km += float(row[9]) if row[9] else 0
                city_fuel += float(row[4].replace(',', '.')) if row[4] else 0
                district_fuel += float(row[7].replace(',', '.')) if row[7] else 0
                highway_fuel += float(row[10].replace(',', '.')) if row[10] else 0

                if row_date >= seven_days_ago:
                    last_7_days_distance += distance
                    last_7_days_fuel += float(row[12].replace(',', '.')) if row[12] else 0
            except ValueError as e:
                logger.warning(f"Неправильний формат дати в рядку {row}: {e}")
                continue

            avg_daily_distance = total_distance / len(days) if days else 0
            total_km = city_km + district_km + highway_km
            city_percent = (city_km / total_km * 100) if total_km else 0
            district_percent = (district_km / total_km * 100) if total_km else 0
            highway_percent = (highway_km / total_km * 100) if total_km else 0

            def progress_bar(percent, emoji):
                filled = int(percent / 10)
                return emoji * filled + "⬜" * (10 - filled)

            text = (
                f"📈 *Статистика пробігу* 🚗\n\n"
                f"📏 *Загальний пробіг*: {total_distance:.1f} км\n"
                f"📅 *Середній за день*: {avg_daily_distance:.1f} км\n"
                f"🛣 *Пробіг за типом дороги*:\n"
                f"  🏙 *Місто*: {city_km:.1f} км ({city_fuel:.2f} л) `{progress_bar(city_percent, '🟦')} {city_percent:.1f}%`\n"
                f"  🌳 *Район*: {district_km:.1f} км ({district_fuel:.2f} л) `{progress_bar(district_percent, '🟩')} {district_percent:.1f}%`\n"
                f"  🛣 *Траса*: {highway_km:.1f} км ({highway_fuel:.2f} л) `{progress_bar(highway_percent, '🟧')} {highway_percent:.1f}%`\n"
                f"📆 *Останні 7 днів*:\n"
                f"  🔄 Пробіг: {last_7_days_distance:.1f} км\n"
                f"  ⛽ Витрати пального: {last_7_days_fuel:.2f} л\n"
            )
            await update.message.reply_text(text, parse_mode="Markdown")
            logger.info(f"Користувач {update.effective_user.id} переглянув статистику")
    except Exception as e:
        await update.message.reply_text(f"⚠️ *Помилка при отриманні статистики*: {e}", parse_mode="Markdown")
        logger.error(f"Помилка статистики: {e}")

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    logger.info(f"Отримано callback: {query.data} від користувача {query.from_user.id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")

    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до кнопки: {query.from_user.id}")
        return

    data = user_data_store.get(query.from_user.id, {})

    if query.data == "add":
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        last_odo = int(float(sheet_cache[-1][1])) if len(sheet_cache) >= 2 else None
        last_odo_text = f"📍 *Твій останній одометр*: {last_odo}" if last_odo else "📍 *Це твій перший запис!*"
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

    elif query.data == "report" or query.data == "last":
        if not sheet_cache:
            await query.edit_message_text("📈 *Таблиця порожня.* 😕", parse_mode="Markdown")
            return
        text = "📊 *Останні записи*:\n\n```\nДата       | Одометр | Пробіг | Місто | Витрати\n"
        for row in sheet_cache[-5:]:
            text += f"{row[0]:<11}| {row[1]:<8}| {row[2]:<7}| {row[3]:<6}| {row[4]}\n"
        text += "```"
        await query.edit_message_text(text, parse_mode="Markdown")
        logger.info(f"Користувач {query.from_user.id} переглянув звіт")

    elif query.data == "stats":
        await stats(update, context)  # Викликаємо команду stats

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
            "4. Загальний кілометраж має відповідати різниці одометра.\n"
            "📈 *Статистика* покаже твої поїздки!",
            parse_mode="Markdown"
        )

    elif query.data == "retry_odometer":
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        last_odo = int(float(sheet_cache[-1][1])) if len(sheet_cache) >= 2 else None
        last_odo_text = f"📍 *Твій останній одометр*: {last_odo}" if last_odo else "📍 *Це твій перший запис!*"
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
        prev_odo = int(float(sheet_cache[-1][1])) if len(sheet_cache) >= 2 else 0
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        await query.edit_message_text(
            f"📏 *Попередній одометр*: {prev_odo}\n"
            f"📍 *Поточний одометр*: {data['odometer']}\n"
            f"🔄 *Пробіг за період*: {data['diff']} км\n\n"
            f"🛣 *Введи розподіл пробігу* (наприклад, *місто* {int(data['diff']/3)} *район* {int(data['diff']/3)} *траса* {int(data['diff']/3)}):\n"
            f"ℹ️ Загальний кілометраж має дорівнювати {data['diff']} км.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_DISTRIBUTION

async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.info(f"Отримано підтвердження: {query.data} від користувача {user_id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")

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

    try:
        start_time = time.time()
        sheet.append_row(row)
        row_index = len(sheet_cache) + 1
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
        update_sheet_cache()
        await query.edit_message_text(
            f"✅ *Запис збережено!* 🎉\n"
            f"📅 {today} | 📏 {data['odometer']} км | 🔄 {data['diff']} км | ⛽ {data['total_exact']:.2f} л",
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
    logger.info(f"Отримано скасування від користувача {user_id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    user_data_store.pop(user_id, None)
    await query.edit_message_text("❌ *Операцію скасовано.*", parse_mode="Markdown")
    logger.info(f"Користувач {user_id} скасував операцію")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Помилка: {context.error}")
    if update and update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("⚠️ *Щось пішло не так. Спробуй ще раз.*", parse_mode="Markdown")

if __name__ == "__main__":
    while True:
        try:
            logger.info(f"🚀 Бот запущено о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
            telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
            logger.info("Application успішно ініціалізовано")

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
            telegram_app.add_handler(conv_handler)
            telegram_app.add_error_handler(error_handler)
            telegram_app.run_polling()
        except KeyboardInterrupt:
            logger.info("Бот зупинено користувачем (Ctrl+C)")
            break
        except Exception as e:
            logger.error(f"Бот впав: {e} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
            time.sleep(10)
