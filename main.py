import os
import re
import json
import pytz
import asyncio
import logging
import aiohttp
from datetime import datetime

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse
from starlette.routing import Route

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler,
    ContextTypes, filters,
)

import gspread
from google.oauth2.service_account import Credentials
from gspread_formatting import CellFormat, Borders, format_cell_range, TextFormat

# НАЛАШТУВАННЯ
tz = pytz.timezone("Europe/Kyiv")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot")
logger.setLevel(logging.DEBUG)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

OWNER_ID = 270380991
CITY_L100 = 11.66
DISTRICT_L100 = 11.17
HIGHWAY_L100 = 10.19

WAITING_FOR_ODOMETER, WAITING_FOR_DISTRIBUTION, CONFIRM = range(3)

telegram_app: Application | None = None
gc = None
worksheet = None
user_data_store: dict[int, dict] = {}


# УТИЛІТИ
def _build_webhook_url() -> str:
    logger.debug("Формуємо WEBHOOK_URL")
    env_url = os.getenv("WEBHOOK_URL")
    if env_url:
        url = env_url.strip()
        logger.debug(f"Використовуємо WEBHOOK_URL з змінної оточення: {url}")
    else:
        host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if not host:
            logger.error("Не знайдено WEBHOOK_URL або RENDER_EXTERNAL_HOSTNAME")
            raise RuntimeError("Не знайдено WEBHOOK_URL або RENDER_EXTERNAL_HOSTNAME")
        url = f"https://{host}" if not host.startswith("http") else host
        logger.debug(f"Використовуємо RENDER_EXTERNAL_HOSTNAME: {url}")

    url = url.rstrip("/")
    if url.endswith("/webhook/webhook"):
        url = url[:-8]
    if not url.endswith("/webhook"):
        url = f"{url}/webhook"
    logger.debug(f"Сформований WEBHOOK_URL: {url}")
    return url


def _authorize_gspread():
    global gc, worksheet
    logger.debug("Авторизація gspread")
    try:
        creds_info = json.loads(SERVICE_ACCOUNT_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        gc = gspread.authorize(creds)
        worksheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
        logger.info("gspread авторизовано успішно")
    except Exception as e:
        logger.error(f"Помилка авторизації gspread: {e}", exc_info=True)
        raise


def _last_row_index() -> int:
    logger.debug("Отримуємо індекс останнього рядка")
    try:
        row_count = len(worksheet.get_all_values())
        logger.debug(f"Індекс останнього рядка: {row_count}")
        return row_count
    except Exception as e:
        logger.error(f"Помилка отримання індексу рядка: {e}", exc_info=True)
        return 1


def _get_last_odometer() -> int | None:
    logger.debug("Отримуємо останній показник одометра")
    try:
        vals = worksheet.get_all_values()
        if len(vals) <= 1:
            logger.debug("Немає записів для одометра")
            return None
        last_odo = int(vals[-1][1])
        logger.debug(f"Останній одометр: {last_odo}")
        return last_odo
    except Exception as e:
        logger.error(f"Помилка отримання одометра: {e}", exc_info=True)
        return None


def _parse_distribution(text: str, total_km: int) -> tuple[int, int, int] | None:
    logger.debug(f"Парсимо розподіл: {text}, сума = {total_km}")
    text = text.lower().strip()
    # Спрощуємо формат: приймаємо "місто X", "X Y Z" або "X"
    nums = re.findall(r"\d+", text)
    if len(nums) == 1 and "місто" in text:
        city = int(nums[0])
        if city == total_km:
            logger.debug(f"Розподіл: місто={city}, район=0, траса=0")
            return city, 0, 0
    elif len(nums) == 3:
        city, dist, hw = map(int, nums[:3])
        if city + dist + hw == total_km:
            logger.debug(f"Розподіл коректний: місто={city}, район={dist}, траса={hw}")
            return city, dist, hw
    logger.debug("Невірний розподіл")
    return None


def _format_just_added_row(row_index: int):
    logger.debug(f"Форматуємо рядок {row_index}")
    try:
        fmt = CellFormat(
            textFormat=TextFormat(bold=False),
            horizontalAlignment="CENTER",
            borders=Borders(
                top={"style": "SOLID"}, bottom={"style": "SOLID"},
                left={"style": "SOLID"}, right={"style": "SOLID"}
            ),
        )
        format_cell_range(worksheet, f"A{row_index}:N{row_index}", fmt)
        logger.debug(f"Рядок {row_index} відформатовано")
    except Exception as e:
        logger.error(f"Помилка форматування рядка {row_index}: {e}", exc_info=True)


# KEYBOARD
def _main_keyboard():
    logger.debug("Формуємо основну клавіатуру")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати запис", callback_data="add"),
         InlineKeyboardButton("ℹ️ Останній запис", callback_data="last")],
        [InlineKeyboardButton("🗑 Видалити останній", callback_data="delete"),
         InlineKeyboardButton("📊 Звіт місяця", callback_data="report")],
        [InlineKeyboardButton("🔁 Скинути", callback_data="reset"),
         InlineKeyboardButton("❓ Допомога", callback_data="help")],
    ])
    logger.debug("Основна клавіатура сформована")
    return keyboard


# HANDLERS
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug(f"Обробка команди /start від користувача {update.effective_user.id}")
    if update.effective_user.id != OWNER_ID:
        logger.warning(f"Несанкціонований доступ: {update.effective_user.id}")
        await update.message.reply_text("❌ У тебе немає доступу.")
        return
    await update.message.reply_text("Привіт! Обери дію 👇", reply_markup=_main_keyboard())
    logger.info(f"Команда /start успішно оброблена для {update.effective_user.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug(f"Скасування розмови для користувача {update.effective_user.id}")
    context.user_data.clear()
    user_data_store.pop(update.effective_user.id, None)
    await update.message.reply_text("🚫 Дію скасовано.", reply_markup=_main_keyboard())
    return ConversationHandler.END


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    logger.debug(f"Обробка кнопки: {q.data} від користувача {q.from_user.id}")
    try:
        await q.answer()
        if q.from_user.id != OWNER_ID:
            logger.warning(f"Несанкціонований доступ до кнопки: {q.from_user.id}")
            await q.edit_message_text("❌ У тебе немає доступу.")
            return ConversationHandler.END

        if q.data == "add":
            last_odo = _get_last_odometer()
            hint = f" (попередній: {last_odo})" if last_odo else ""
            await q.edit_message_text(f"Введи одометр{hint}:")
            logger.info(f"Користувач {q.from_user.id} обрав додавання запису")
            context.user_data["state"] = WAITING_FOR_ODOMETER
            return WAITING_FOR_ODOMETER

        if q.data == "delete":
            r = _last_row_index()
            if r > 1:
                worksheet.delete_rows(r)
                await q.edit_message_text("✅ Видалено останній запис.")
                logger.info(f"Останній запис видалено, рядок: {r}")
            else:
                await q.edit_message_text("Немає що видаляти.")
                logger.info("Спроба видалити запис, але таблиця порожня")
            return ConversationHandler.END

        if q.data == "last":
            vals = worksheet.get_all_values()
            if len(vals) <= 1:
                await q.edit_message_text("Немає записів.")
                logger.info("Спроба перегляду останнього запису, але таблиця порожня")
                return ConversationHandler.END
            await q.edit_message_text(str(vals[-1]))
            logger.info(f"Останній запис відображено: {vals[-1]}")
            return ConversationHandler.END

        if q.data == "report":
            now = datetime.now(tz)
            month = now.strftime("%Y-%m")
            vals = worksheet.get_all_values()
            total = sum(float(r[13]) for r in vals[1:] if r and r[0].startswith(month))
            await q.edit_message_text(f"📊 Звіт {month}: {round(total,2)} л")
            logger.info(f"Звіт за {month}: {round(total,2)} л")
            return ConversationHandler.END

        if q.data == "reset":
            user_data_store.pop(q.from_user.id, None)
            context.user_data.clear()
            await q.edit_message_text("🔁 Дані скинуто.")
            logger.info(f"Дані скинуто для користувача {q.from_user.id}")
            return ConversationHandler.END

        if q.data == "help":
            await q.edit_message_text(
                "❓ Допомога: \n"
                "- /start: меню\n"
                "- Додати запис: показує останній одометр, введи новий, потім розподіл (місто, район, траса), сума = різниця.\n"
                "- Останній запис: показує останній рядок.\n"
                "- Видалити останній: видаляє останній запис.\n"
                "- Звіт місяця: показує витрати за місяць.\n"
                "- Скинути: очищає тимчасові дані.\n"
                "- /cancel: скасувати діалог."
            )
            logger.info(f"Допомога показана для користувача {q.from_user.id}")
            return ConversationHandler.END

        await q.edit_message_text("Невідома дія.")
        logger.warning(f"Невідома дія кнопки: {q.data} від користувача {q.from_user.id}")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Помилка обробки кнопки {q.data}: {e}", exc_info=True)
        await q.edit_message_text("🚫 Помилка. Спробуй /start.")
        return ConversationHandler.END


async def handle_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.debug(f"Обробка одометра: {update.message.text} від користувача {update.effective_user.id}, стан: {context.user_data.get('state')}")
    try:
        odo = int(update.message.text.strip())
        prev = _get_last_odometer()
        diff = odo - prev if prev else 0
        user_data_store[update.effective_user.id] = {"odometer": odo, "diff": diff}
        await update.message.reply_text(f"Введи розподіл: місто, район, траса (сума = {diff})")
        logger.info(f"Одометр введено: {odo}, різниця: {diff}")
        context.user_data["state"] = WAITING_FOR_DISTRIBUTION
        return WAITING_FOR_DISTRIBUTION
    except ValueError as e:
        logger.error(f"Помилка парсингу одометра: {e}", exc_info=True)
        await update.message.reply_text("❌ Введи ціле число.")
        return WAITING_FOR_ODOMETER
    except Exception as e:
        logger.error(f"Помилка обробки одометра: {e}", exc_info=True)
        await update.message.reply_text("🚫 Помилка. Спробуй /start.")
        return ConversationHandler.END


async def handle_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Обробка розподілу: {update.message.text} від користувача {user_id}, стан: {context.user_data.get('state')}")
    try:
        data = user_data_store.get(user_id)
        if not data:
            logger.error("Немає даних користувача для розподілу")
            await update.message.reply_text("🚫 Помилка. Почни з /start.")
            return ConversationHandler.END
        parsed = _parse_distribution(update.message.text, data["diff"])
        if not parsed:
            await update.message.reply_text("❌ Невірний розподіл. Введи: місто, район, траса (сума = {})".format(data["diff"]))
            logger.warning(f"Невірний розподіл: {update.message.text}")
            return WAITING_FOR_DISTRIBUTION
        city, dist, hw = parsed
        c = city * CITY_L100 / 100
        d = dist * DISTRICT_L100 / 100
        h = hw * HIGHWAY_L100 / 100
        t = c + d + h
        data.update({"city": city, "dist": dist, "hw": hw, "c": c, "d": d, "h": h, "t": t})
        await update.message.reply_text(f"🏙 {c:.2f} л, 🏞 {d:.2f} л, 🛣 {h:.2f} л\nΣ {t:.2f} л. Зберегти?")
        logger.info(f"Розподіл оброблено: місто={c:.2f}, район={d:.2f}, траса={h:.2f}, сума={t:.2f}")
        context.user_data["state"] = CONFIRM
        return CONFIRM
    except Exception as e:
        logger.error(f"Помилка обробки розподілу: {e}", exc_info=True)
        await update.message.reply_text("🚫 Помилка. Спробуй /start.")
        return ConversationHandler.END


async def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    logger.debug(f"Підтвердження збереження від користувача {user_id}, стан: {context.user_data.get('state')}")
    try:
        d = user_data_store.pop(user_id, None)
        if not d:
            logger.error("Немає даних для збереження")
            await update.message.reply_text("🚫 Помилка. Почни з /start.")
            return ConversationHandler.END
        now = datetime.now(tz)
        row = [now.strftime("%Y-%m-%d %H:%M:%S"), d["odometer"], d["diff"],
               d["city"], f"{d['c']:.4f}", round(d["c"]),
               d["dist"], f"{d['d']:.4f}", round(d["d"]),
               d["hw"], f"{d['h']:.4f}", round(d["h"]),
               f"{d['t']:.4f}", round(d["t"])]
        worksheet.append_row(row)
        _format_just_added_row(_last_row_index())
        await update.message.reply_text("✅ Збережено.", reply_markup=_main_keyboard())
        logger.info(f"Запис збережено: {row}")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Помилка збереження: {e}", exc_info=True)
        await update.message.reply_text("🚫 Помилка. Спробуй /start.")
        return ConversationHandler.END


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Помилка: {context.error}", exc_info=True)
    if update and update.message:
        await update.message.reply_text("🚫 Виникла помилка. Спробуй /start.")
    context.user_data.clear()
    user_data_store.pop(update.effective_user.id, None) if update else None
    return ConversationHandler.END


# KEEP ALIVE
async def keep_alive():
    logger.debug("Запускаємо keep_alive для пінгу сервера")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get("https://telegram-car-bot-px9n.onrender.com") as resp:
                    logger.debug(f"keep_alive пінг: статус {resp.status}")
                    if resp.status != 200:
                        logger.error(f"keep_alive неуспішний: статус {resp.status}")
            except Exception as e:
                logger.error(f"Помилка keep_alive: {e}", exc_info=True)
            await asyncio.sleep(10)  # Зменшено інтервал до 10 секунд


async def telegram_ping():
    logger.debug("Запускаємо telegram_ping для підтримки активності")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe") as resp:
                    logger.debug(f"telegram_ping: статус {resp.status}")
                    if resp.status != 200:
                        logger.error(f"telegram_ping неуспішний: статус {resp.status}")
            except Exception as e:
                logger.error(f"Помилка telegram_ping: {e}", exc_info=True)
            await asyncio.sleep(5)  # Зменшено інтервал до 5 секунд


# APP
async def init_telegram_app():
    global telegram_app
    logger.info("Починаємо ініціалізацію Telegram Application")
    try:
        _authorize_gspread()
        telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(handle_buttons, pattern="^add$")],
            states={
                WAITING_FOR_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_odometer)],
                WAITING_FOR_DISTRIBUTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_distribution)],
                CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_save)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_message=False,
        )
        telegram_app.add_handler(conv)
        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CallbackQueryHandler(handle_buttons, pattern="^(delete|last|report|reset|help)$"))
        telegram_app.add_error_handler(error_handler)
        await telegram_app.initialize()
        await telegram_app.start()
        webhook_url = _build_webhook_url()
        await telegram_app.bot.set_webhook(webhook_url, drop_pending_updates=True)
        asyncio.create_task(keep_alive())
        asyncio.create_task(telegram_ping())
        logger.info("Telegram app ініціалізовано та запущено")
    except Exception as e:
        logger.error(f"Помилка ініціалізації Telegram Application: {e}", exc_info=True)
        telegram_app = None
        raise


async def shutdown_telegram_app():
    logger.debug("Завершення роботи telegram_app")
    if telegram_app:
        try:
            await telegram_app.bot.delete_webhook()
            logger.info("Вебхук видалено")
            await telegram_app.stop()
            logger.info("telegram_app зупинено")
            await telegram_app.shutdown()
            logger.info("telegram_app завершено")
        except Exception as e:
            logger.error(f"Помилка завершення telegram_app: {e}", exc_info=True)


async def home(request: Request):
    logger.debug(f"Отримано пінг на / від {request.client.host}")
    return PlainTextResponse("Bot is alive ✅")


async def webhook(request: Request):
    logger.debug(f"Отримано вебхук-запит від {request.client.host}")
    if not telegram_app:
        logger.error("Telegram Application не ініціалізовано")
        return Response(status_code=500)
    try:
        data = await request.json()
        logger.debug(f"Отримано дані вебхука: {data}")
        update = Update.de_json(data, bot=telegram_app.bot)
        if update is None:
            logger.error("Не вдалося десеріалізувати оновлення")
            return Response(status_code=400)
        await telegram_app.process_update(update)
        logger.info("Вебхук оброблено успішно")
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Помилка обробки вебхука: {e}", exc_info=True)
        return Response(status_code=500)


routes = [Route("/", home), Route("/webhook", webhook, methods=["POST"])]
app = Starlette(routes=routes, on_startup=[init_telegram_app], on_shutdown=[shutdown_telegram_app])
