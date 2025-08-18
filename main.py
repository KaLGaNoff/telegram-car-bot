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
from flask import Flask, request, Response
import requests
import threading

# Придушення PTBUserWarning
warnings.filterwarnings("ignore", category=telegram.warnings.PTBUserWarning)

# Налаштування логування
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
log_handler = TimedRotatingFileHandler(
    filename="bot.log",
    when="midnight",
    interval=1,
    backupCount=7
)
log_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
))
logger.addHandler(log_handler)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
))
logger.addHandler(console_handler)

logger.info(f"Версія python-telegram-bot: {telegram.__version__}")

# Змінні оточення
OWNER_ID = 270380991
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://your-render-app.onrender.com/webhook")

if not all([TELEGRAM_TOKEN, GOOGLE_SHEET_ID, SERVICE_ACCOUNT_JSON]):
    logger.error("Відсутні обов’язкові змінні оточення")
    raise ValueError("Відсутні обов’язкові змінні оточення")

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
WAITING_FOR_ODOMETER, WAITING_FOR_DISTRIBUTION, CONFIRM = range(3)
user_data_store = {}

# Flask сервер
app = Flask(__name__)
telegram_app = None

def check_webhook():
    logger.debug("Перевірка стану вебхука")
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo")
        data = resp.json()
        logger.info(f"Статус вебхука: {data}")
        if not data.get("result", {}).get("url"):
            logger.info(f"Вебхук не встановлено, встановлюємо {WEBHOOK_URL}")
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                json={"url": WEBHOOK_URL, "drop_pending_updates": True}
            )
            logger.info(f"Вебхук встановлено: {resp.json()}")
        elif data.get("result", {}).get("pending_update_count", 0) > 0:
            logger.warning(f"Знайдено {data['result']['pending_update_count']} необроблених оновлень, очищаємо")
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                json={"url": data["result"]["url"], "drop_pending_updates": True}
            )
            logger.info(f"Очищено оновлення: {resp.json()}")
    except Exception as e:
        logger.error(f"Помилка перевірки вебхука: {e}", exc_info=True)

def periodic_webhook_check():
    logger.debug("Запускаємо періодичну перевірку вебхука")
    while True:
        check_webhook()
        time.sleep(60)

def keep_alive():
    logger.debug("Запускаємо keep_alive")
    while True:
        try:
            resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe")
            logger.debug(f"Keep alive: Telegram API відповів {resp.status_code}")
        except Exception as e:
            logger.error(f"Keep alive помилка: {e}")
        time.sleep(300)

def telegram_ping():
    logger.debug("Запускаємо telegram_ping")
    while True:
        try:
            resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe")
            logger.debug(f"Telegram ping: {resp.status_code}")
        except Exception as e:
            logger.error(f"Telegram ping помилка: {e}")
        time.sleep(5)

def init_telegram_app():
    global telegram_app
    logger.info("Починаємо ініціалізацію Telegram Application")
    try:
        telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(handle_buttons)],
            states={
                WAITING_FOR_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_odometer)],
                WAITING_FOR_DISTRIBUTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_distribution)],
                CONFIRM: [CallbackQueryHandler(confirm_save)]
            },
            fallbacks=[CallbackQueryHandler(cancel, pattern="^cancel$")],
            per_user=True,
            per_chat=True,
            per_message=False
        )
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CommandHandler("stats", stats))
        telegram_app.add_handler(conv_handler)
        telegram_app.add_error_handler(error_handler)
        telegram_app.initialize()
        telegram_app.start()
        check_webhook()
        # Запускаємо періодичні перевірки у фонових потоках
        threading.Thread(target=keep_alive, daemon=True).start()
        threading.Thread(target=telegram_ping, daemon=True).start()
        threading.Thread(target=periodic_webhook_check, daemon=True).start()
        logger.info("Telegram app ініціалізовано та запущено")
    except Exception as e:
        logger.error(f"Помилка ініціалізації Telegram Application: {e}", exc_info=True)
        telegram_app = None
        raise

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        update = Update.de_json(update, telegram_app.bot)
        telegram_app.process_update(update)
        return Response(status=200)
    except Exception as e:
        logger.error(f"Помилка обробки вебхука: {e}", exc_info=True)
        return Response(status=500)

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}

def _main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Додати пробіг", callback_data="add"), InlineKeyboardButton("🔴 Видалити", callback_data="delete")],
        [InlineKeyboardButton("📊 Звіт", callback_data="report"), InlineKeyboardButton("🧾 Останній", callback_data="last")],
        [InlineKeyboardButton("📈 Статистика", callback_data="stats"), InlineKeyboardButton("♻️ Скинути", callback_data="reset")],
        [InlineKeyboardButton("ℹ️ Допомога", callback_data="help")]
    ])

def _get_last_odometer():
    return int(float(sheet_cache[-1][1])) if len(sheet_cache) >= 2 else 0

def _parse_distribution(text, expected_sum):
    city_km = district_km = highway_km = 0
    text = text.lower()
    try:
        for word in text.split():
            if "міст" in word:
                next_value = text.split(word)[1].strip().split()[0]
                city_km = float(next_value)
            elif "район" in word:
                next_value = text.split(word)[1].strip().split()[0]
                district_km = float(next_value)
            elif "трас" in word:
                next_value = text.split(word)[1].strip().split()[0]
                highway_km = float(next_value)
    except (IndexError, ValueError):
        raise ValueError("Неправильний формат розподілу")
    total_entered = city_km + district_km + highway_km
    if abs(total_entered - expected_sum) > 1:
        raise ValueError(f"Загальний кілометраж ({total_entered}) не збігається з пробігом ({expected_sum})")
    return city_km, district_km, highway_km

def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Отримано команду /start від користувача {update.effective_user.id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    if update.effective_user.id != OWNER_ID:
        logger.warning(f"Несанкціонований доступ: {update.effective_user.id}")
        update.message.reply_text("❌ *У тебе немає доступу до цього бота.*", parse_mode="Markdown")
        return
    context.user_data.clear()  # Очищаємо стан при /start
    user_data_store.pop(update.effective_user.id, None)
    update.message.reply_text(
        "🚗 *Вітаю у твоєму авто-боті!* 👋\nОбери дію нижче:",
        reply_markup=_main_keyboard(),
        parse_mode="Markdown"
    )
    logger.info(f"Команда /start успішно оброблена для {update.effective_user.id}")

def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Отримано команду /stats від користувача {update.effective_user.id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    if update.effective_user.id != OWNER_ID:
        update.message.reply_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до /stats: {update.effective_user.id}")
        return

    if not sheet_cache:
        update.message.reply_text("📈 *Таблиця порожня.* 😕", parse_mode="Markdown")
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
        update.message.reply_text(text, parse_mode="Markdown")
        logger.info(f"Користувач {update.effective_user.id} переглянув статистику")
    except Exception as e:
        update.message.reply_text(f"⚠️ *Помилка при отриманні статистики*: {e}", parse_mode="Markdown")
        logger.error(f"Помилка статистики: {e}")

def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    query.answer()
    logger.info(f"Отримано callback: {query.data} від користувача {query.from_user.id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")

    if query.from_user.id != OWNER_ID:
        query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до кнопки: {query.from_user.id}")
        return

    data = user_data_store.get(query.from_user.id, {})
    context.user_data["state"] = None

    if query.data == "add":
        context.user_data["state"] = WAITING_FOR_ODOMETER
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        last_odo = _get_last_odometer()
        last_odo_text = f"📍 *Твій останній одометр*: {last_odo}" if last_odo else "📍 *Це твій перший запис!*"
        query.edit_message_text(
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
                query.edit_message_text("🗑 *Останній запис видалено!* ✅")
                logger.info(f"Користувач {query.from_user.id} видалив останній запис за {time.time() - start_time:.3f} сек")
            except Exception as e:
                query.edit_message_text(f"⚠️ *Помилка видалення*: {e}", parse_mode="Markdown")
                logger.error(f"Помилка видалення запису: {e}")
        else:
            query.edit_message_text("📈 *Таблиця порожня.* 😕", parse_mode="Markdown")

    elif query.data == "report" or query.data == "last":
        if not sheet_cache:
            query.edit_message_text("📈 *Таблиця порожня.* 😕", parse_mode="Markdown")
            return
        text = "📊 *Останні записи*:\n\n```\nДата       | Одометр | Пробіг | Місто | Витрати\n"
        for row in sheet_cache[-5:]:
            text += f"{row[0]:<11}| {row[1]:<8}| {row[2]:<7}| {row[3]:<6}| {row[4]}\n"
        text += "```"
        query.edit_message_text(text, parse_mode="Markdown")
        logger.info(f"Користувач {query.from_user.id} переглянув звіт")

    elif query.data == "stats":
        stats(update, context)

    elif query.data == "reset":
        user_data_store.pop(query.from_user.id, None)
        context.user_data.clear()
        query.edit_message_text("♻️ *Дані скинуто!* ✅", parse_mode="Markdown")
        logger.info(f"Користувач {query.from_user.id} скинув дані")

    elif query.data == "help":
        query.edit_message_text(
            "ℹ️ *Як користуватися ботом*:\n"
            "1. Натисни 🟢 *Додати пробіг*.\n"
            "2. Введи одометр (наприклад, `53200`).\n"
            "3. Вкажи розподіл: *місто* 50 *район* 30 *траса* 6.\n"
            "4. Загальний кілометраж має відповідати різниці одометра.\n"
            "📈 *Статистика* покаже твої поїздки!",
            parse_mode="Markdown"
        )

    elif query.data == "retry_odometer":
        context.user_data["state"] = WAITING_FOR_ODOMETER
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        last_odo = _get_last_odometer()
        last_odo_text = f"📍 *Твій останній одометр*: {last_odo}" if last_odo else "📍 *Це твій перший запис!*"
        query.edit_message_text(
            f"{last_odo_text}\n\n📏 *Введи поточний одометр* (наприклад, `53200`):",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_ODOMETER

    elif query.data == "retry_distribution":
        user_id = query.from_user.id
        data = user_data_store.get(user_id, {})
        if not data:
            query.edit_message_text("⚠️ *Дані загублено. Почни знову.*", parse_mode="Markdown")
            logger.warning(f"Дані загублено для користувача {user_id}")
            return ConversationHandler.END
        context.user_data["state"] = WAITING_FOR_DISTRIBUTION
        prev_odo = _get_last_odometer()
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        query.edit_message_text(
            f"📏 *Попередній одометр*: {prev_odo}\n"
            f"📍 *Поточний одометр*: {data['odometer']}\n"
            f"🔄 *Пробіг за період*: {data['diff']} км\n\n"
            f"🛣 *Введи розподіл пробігу* (наприклад, *місто* {int(data['diff']/3)} *район* {int(data['diff']/3)} *траса* {int(data['diff']/3)}):\n"
            f"ℹ️ Загальний кілометраж має дорівнювати {data['diff']} км.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WAITING_FOR_DISTRIBUTION

def handle_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Обробка одометра: {update.message.text} від користувача {user_id}, стан: {context.user_data.get('state')}")
    if update.effective_user.id != OWNER_ID:
        update.message.reply_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до одометра: {user_id}")
        return ConversationHandler.END

    if context.user_data.get("state") != WAITING_FOR_ODOMETER:
        logger.warning(f"Невірний стан для одометра: {context.user_data.get('state')}")
        context.user_data.clear()
        user_data_store.pop(user_id, None)
        update.message.reply_text("🚫 Почни з /start або натисни 'Додати запис'.", parse_mode="Markdown")
        return ConversationHandler.END

    try:
        text = update.message.text.strip().replace(",", ".")
        if not text.replace(".", "", 1).isdigit():
            keyboard = [
                [InlineKeyboardButton("🔄 Спробувати ще", callback_data="retry_odometer"),
                 InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
            ]
            update.message.reply_text(
                "😅 *Введи число* (наприклад, `53200`):",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return WAITING_FOR_ODOMETER

        odo = int(float(text))
        prev = _get_last_odometer()
        diff = odo - prev if prev else odo
        if diff <= 0:
            keyboard = [
                [InlineKeyboardButton("🔄 Спробувати ще", callback_data="retry_odometer"),
                 InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
            ]
            update.message.reply_text(
                f"❗️ *Одометр має бути більший за попередній* ({prev}).",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return WAITING_FOR_ODOMETER

        user_data_store[user_id] = {"odometer": odo, "diff": diff}
        context.user_data["state"] = WAITING_FOR_DISTRIBUTION
        keyboard = [[InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]]
        update.message.reply_text(
            f"📏 *Попередній одометр*: {prev}\n"
            f"📍 *Поточний одометр*: {odo}\n"
            f"🔄 *Пробіг за період*: {diff} км\n\n"
            f"🛣 *Введи розподіл пробігу* (наприклад, *місто* {int(diff/3)} *район* {int(diff/3)} *траса* {int(diff/3)}):\n"
            f"ℹ️ Загальний кілометраж має дорівнювати {diff} км.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"Одометр введено: {odo}, різниця: {diff}")
        return WAITING_FOR_DISTRIBUTION
    except Exception as e:
        logger.error(f"Помилка обробки одометра: {e}", exc_info=True)
        context.user_data.clear()
        user_data_store.pop(user_id, None)
        update.message.reply_text("🚫 Помилка. Спробуй /start.", parse_mode="Markdown")
        return ConversationHandler.END

def handle_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Обробка розподілу: {update.message.text} від користувача {user_id}, стан: {context.user_data.get('state')}")
    if update.effective_user.id != OWNER_ID:
        update.message.reply_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до розподілу: {user_id}")
        return ConversationHandler.END

    if context.user_data.get("state") != WAITING_FOR_DISTRIBUTION:
        logger.warning(f"Невірний стан для розподілу: {context.user_data.get('state')}")
        context.user_data.clear()
        user_data_store.pop(user_id, None)
        update.message.reply_text("🚫 Почни з /start або натисни 'Додати запис'.", parse_mode="Markdown")
        return ConversationHandler.END

    try:
        data = user_data_store.get(user_id)
        if not data:
            logger.error("Немає даних користувача для розподілу")
            context.user_data.clear()
            update.message.reply_text("🚫 Помилка. Почни з /start.", parse_mode="Markdown")
            return ConversationHandler.END

        city_km, district_km, highway_km = _parse_distribution(update.message.text, data["diff"])
        
        def calc(litres_per_100km, km):
            exact = round(km * litres_per_100km / 100, 2)
            rounded = round(exact)
            return exact, rounded

        c_exact, c_rounded = calc(11.66, city_km)
        d_exact, d_rounded = calc(11.17, district_km)
        h_exact, h_rounded = calc(10.19, highway_km)
        total_exact = round(c_exact + d_exact + h_exact, 2)
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
            f"🔄 *Пробіг*: {data['diff']} км\n"
            f"🏙 *Місто*: {int(city_km)} км → {c_exact:.2f} л (≈ {c_rounded})\n"
            f"🌳 *Район*: {int(district_km)} км → {d_exact:.2f} л (≈ {d_rounded})\n"
            f"🛣 *Траса*: {int(highway_km)} км → {h_exact:.2f} л (≈ {h_rounded})\n"
            f"⛽ *Загальний кілометраж*: {total_exact:.2f} л (≈ {total_rounded})\n\n"
            f"✅ *Зберегти запис?*"
        )

        keyboard = [
            [InlineKeyboardButton("✅ Так", callback_data="confirm_yes"), InlineKeyboardButton("❌ Ні", callback_data="confirm_no")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]
        update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["state"] = CONFIRM
        logger.info(f"Користувач {user_id} ввів розподіл: місто={city_km}, район={district_km}, траса={highway_km}")
        return CONFIRM
    except Exception as e:
        logger.error(f"Помилка обробки розподілу: {e}", exc_info=True)
        context.user_data.clear()
        user_data_store.pop(user_id, None)
        update.message.reply_text("🚫 Помилка. Спробуй /start.", parse_mode="Markdown")
        return ConversationHandler.END

def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    logger.debug(f"Підтвердження збереження від користувача {user_id}, стан: {context.user_data.get('state')}")
    
    if query.from_user.id != OWNER_ID:
        query.edit_message_text("❌ *У тебе немає доступу.*", parse_mode="Markdown")
        logger.warning(f"Несанкціонований доступ до підтвердження: {user_id}")
        return ConversationHandler.END

    if context.user_data.get("state") != CONFIRM:
        logger.warning(f"Невірний стан для підтвердження: {context.user_data.get('state')}")
        context.user_data.clear()
        user_data_store.pop(user_id, None)
        query.edit_message_text("🚫 Почни з /start або натисни 'Додати запис'.", parse_mode="Markdown")
        return ConversationHandler.END

    try:
        if query.data == "confirm_no" or query.data == "cancel":
            user_data_store.pop(user_id, None)
            context.user_data.clear()
            query.edit_message_text("❌ *Скасовано.*", parse_mode="Markdown")
            logger.info(f"Користувач {user_id} скасував запис")
            return ConversationHandler.END

        data = user_data_store.pop(user_id, {})
        if not data:
            logger.error("Немає даних для збереження")
            context.user_data.clear()
            query.edit_message_text("🚫 Помилка. Почни з /start.", parse_mode="Markdown")
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
        query.edit_message_text(
            f"✅ *Запис збережено!* 🎉\n"
            f"📅 {today} | 📏 {data['odometer']} км | 🔄 {data['diff']} км | ⛽ {data['total_exact']:.2f} л",
            parse_mode="Markdown"
        )
        logger.info(f"Користувач {user_id} зберіг запис: {row} за {time.time() - start_time:.3f} сек")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Помилка збереження: {e}", exc_info=True)
        context.user_data.clear()
        user_data_store.pop(user_id, None)
        query.edit_message_text("🚫 Помилка. Спробуй /start.", parse_mode="Markdown")
        return ConversationHandler.END

def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    query.answer()
    user_id = query.from_user.id
    logger.info(f"Отримано скасування від користувача {user_id} о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
    user_data_store.pop(user_id, None)
    context.user_data.clear()
    query.edit_message_text("❌ *Операцію скасовано.*", parse_mode="Markdown")
    logger.info(f"Користувач {user_id} скасував операцію")
    return ConversationHandler.END

def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Помилка: {context.error}", exc_info=True)
    if update and update.callback_query:
        update.callback_query.answer()
        update.callback_query.edit_message_text("⚠️ *Щось пішло не так. Спробуй ще раз.*", parse_mode="Markdown")

def main():
    try:
        logger.info(f"🚀 Бот запущено о {datetime.now(pytz.timezone('Europe/Kiev')).strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        init_telegram_app()
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
    except Exception as e:
        logger.error(f"Помилка запуску: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    main()
