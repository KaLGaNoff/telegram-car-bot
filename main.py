import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple, List

import pytz
import gspread
from gspread_formatting import (
    cellFormat,
    textFormat,
    color,
    format_cell_range,
    borders,
    Border,
    NumberFormat,
)

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.requests import Request

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest, TimedOut, NetworkError

# ------------------------------------------------------------
# ЛОГІНГ
# ------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# ENV
# ------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")  # JSON-рядок (не шлях!)
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") or (
    f"https://{RENDER_EXTERNAL_HOSTNAME}/webhook" if RENDER_EXTERNAL_HOSTNAME else None
)

# ------------------------------------------------------------
# ТАЙМЗОНА / ДАТИ
# ------------------------------------------------------------
TZ = pytz.timezone("Europe/Kyiv")

def now_kyiv() -> datetime:
    return datetime.now(TZ)

# ------------------------------------------------------------
# GSHEET
# ------------------------------------------------------------
gc = None
ws = None

def init_gsheet() -> None:
    global gc, ws
    if not SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        raise RuntimeError("Не задані SERVICE_ACCOUNT_JSON або GOOGLE_SHEET_ID")
    try:
        creds = json.loads(SERVICE_ACCOUNT_JSON)
    except Exception as e:
        raise RuntimeError(f"SERVICE_ACCOUNT_JSON не валідний JSON: {e}")
    gc = gspread.service_account_from_dict(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    ws = sh.sheet1
    logger.info("Google Sheet підключено")

# ------------------------------------------------------------
# СТАН / КОНСТАНТИ КОНВЕРСЕЙШЕНУ
# ------------------------------------------------------------
(
    STATE_WAITING_ODOMETER,
    STATE_WAITING_DISTRIBUTION,
    STATE_WAITING_CONFIRMATION,
) = range(3)

telegram_app: Optional[Application] = None

# Тимчасові дані користувачів (на випадок рестартів — мінімум для поточної сесії)
user_state: Dict[int, Dict[str, Any]] = {}

# ------------------------------------------------------------
# ДОП ОПЦІЇ/ТЕКСТИ
# ------------------------------------------------------------
BTN_ADD = "➕ Додати пробіг"
BTN_LAST = "📄 Останній запис"
BTN_REPORT = "📊 Звіт"
BTN_HELP = "❓ Допомога"
BTN_RESET = "♻️ Скинути"
BTN_DELETE = "🗑 Видалити останній"

def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(BTN_ADD, callback_data="add"),
            InlineKeyboardButton(BTN_LAST, callback_data="last"),
        ],
        [
            InlineKeyboardButton(BTN_REPORT, callback_data="report"),
            InlineKeyboardButton(BTN_HELP, callback_data="help"),
        ],
        [
            InlineKeyboardButton(BTN_DELETE, callback_data="delete"),
            InlineKeyboardButton(BTN_RESET, callback_data="reset"),
        ],
    ]
    return InlineKeyboardMarkup(rows)

# ------------------------------------------------------------
# ДОПОМОЖНІ ФУНКЦІЇ ДЛЯ ТАБЛИЦІ
# ------------------------------------------------------------
def get_last_row_values() -> Optional[List[Any]]:
    values = ws.get_all_values()
    if len(values) <= 1:
        return None
    return values[-1]

def append_row_and_format(row: List[Any]) -> None:
    """
    Додає рядок у таблицю й застосовує форматування:
    - межі по всіх клітинках
    - центрування
    - формат чисел де треба
    """
    ws.append_row(row, value_input_option="USER_ENTERED")
    # індекс щойно доданого рядка
    last_row_index = ws.row_count
    # знайдемо реальний індекс останнього не-порожнього
    values = ws.get_all_values()
    last_row_index = len(values)

    rng = f"A{last_row_index}:N{last_row_index}"
    fmt = cellFormat(
        horizontalAlignment="CENTER",
        verticalAlignment="MIDDLE",
        textFormat=textFormat(bold=False),
        borders=borders(
            top=Border("SOLID"),
            bottom=Border("SOLID"),
            left=Border("SOLID"),
            right=Border("SOLID"),
        ),
    )
    try:
        format_cell_range(ws, rng, fmt)
    except Exception as e:
        logger.warning(f"Не вдалося застосувати форматування для {rng}: {e}")

# ------------------------------------------------------------
# БІЗНЕС-ЛОГІКА
# ------------------------------------------------------------
def parse_int(text: str) -> Optional[int]:
    try:
        return int(text.strip().replace(" ", ""))
    except Exception:
        return None

def compute_distribution_diff(prev_odometer: int, new_odometer: int) -> int:
    return max(0, new_odometer - prev_odometer)

def build_distribution_text(diff: int) -> str:
    return (
        "Розподіли кілометраж (місто/округ/траса).\n"
        f"Загальний пробіг за період: <b>{diff} км</b>.\n"
        "Надішли у форматі: <code>місто околиця траса</code>, наприклад: <code>120 30 50</code>"
    )

def split_distribution(text: str) -> Optional[Tuple[int, int, int]]:
    parts = text.replace(",", " ").split()
    if len(parts) != 3:
        return None
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except Exception:
            return None
    return tuple(nums)  # type: ignore

def get_prev_odometer() -> int:
    last = get_last_row_values()
    if not last:
        return 0
    try:
        return int(last[1])  # другий стовпець — одометр
    except Exception:
        return 0

def render_last_text() -> str:
    last = get_last_row_values()
    if not last:
        return "Поки немає записів."
    # Підлаштуємо під вашу структуру колонок (A..N)
    (
        date,
        odometer,
        diff,
        city_km,
        city_exact,
        city_rounded,
        district_km,
        district_exact,
        district_rounded,
        highway_km,
        highway_exact,
        highway_rounded,
        total_exact,
        total_rounded,
        *rest,
    ) = (last + [""] * 14)[:14]
    return (
        f"<b>Дата:</b> {date}\n"
        f"<b>Одометр:</b> {odometer}\n"
        f"<b>Пробіг:</b> {diff} км\n"
        f"<b>Місто:</b> {city_km} (≈ {city_rounded})\n"
        f"<b>Округ:</b> {district_km} (≈ {district_rounded})\n"
        f"<b>Траса:</b> {highway_km} (≈ {highway_rounded})\n"
        f"<b>Разом (точно/≈):</b> {total_exact} / {total_rounded}"
    )

# ------------------------------------------------------------
# HANDLERS
# ------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Вітаю! Оберіть дію нижче.",
        reply_markup=main_menu_keyboard(),
    )

async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Команди:\n"
        f"{BTN_ADD} — додати новий запис\n"
        f"{BTN_LAST} — показати останній запис\n"
        f"{BTN_REPORT} — короткий звіт за місяць\n"
        f"{BTN_DELETE} — видалити останній запис\n"
        f"{BTN_RESET} — скинути поточний ввід\n\n"
        "Усі дії доступні через кнопки."
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML)

async def on_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    user_state.pop(uid, None)
    text = "Скинуто. Починаймо спочатку."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard())

async def on_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = render_last_text()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML)

async def on_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Зітремо останній рядок — дуже обережно, у вас може бути інша логіка архівації
    try:
        values = ws.get_all_values()
        if len(values) <= 1:
            text = "Немає що видаляти."
        else:
            ws.delete_rows(len(values))
            text = "Останній запис видалено."
    except Exception as e:
        logger.exception(e)
        text = f"Помилка видалення: {e}"

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard())

async def on_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Дуже спрощений звіт (приклад)
    try:
        values = ws.get_all_values()
        if len(values) <= 1:
            text = "Даних для звіту ще немає."
        else:
            # рахуємо по останніх N рядках або за поточний місяць
            # тут просто по всім
            total_km = 0
            for row in values[1:]:
                try:
                    total_km += int(row[2])  # diff
                except Exception:
                    pass
            text = f"Сумарний пробіг у таблиці: <b>{total_km} км</b>."
    except Exception as e:
        logger.exception(e)
        text = f"Помилка при формуванні звіту: {e}"

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode=ParseMode.HTML)

# -------- Додавання запису (конверсейшн) --------
async def start_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Введи поточний показник одометра:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="reset")]]))
    else:
        await update.effective_message.reply_text("Введи поточний показник одометра:", reply_markup=ReplyKeyboardRemove())
    return STATE_WAITING_ODOMETER

async def got_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text or ""
    val = parse_int(text)
    if val is None or val <= 0:
        await update.effective_message.reply_text("Надішли ціле число (км). Спробуй ще раз.")
        return STATE_WAITING_ODOMETER

    uid = update.effective_user.id
    prev = get_prev_odometer()
    diff = compute_distribution_diff(prev, val)
    user_state[uid] = {
        "odometer": val,
        "diff": diff,
    }
    await update.effective_message.reply_text(
        build_distribution_text(diff),
        parse_mode=ParseMode.HTML,
    )
    return STATE_WAITING_DISTRIBUTION

async def got_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    st = user_state.get(uid) or {}
    dist = split_distribution(update.effective_message.text or "")
    if not dist:
        await update.effective_message.reply_text(
            "Формат: <code>місто околиця траса</code>, наприклад: <code>120 30 50</code>",
            parse_mode=ParseMode.HTML,
        )
        return STATE_WAITING_DISTRIBUTION

    c, d, h = dist
    if c < 0 or d < 0 or h < 0:
        await update.effective_message.reply_text("Значення не можуть бути від’ємними.")
        return STATE_WAITING_DISTRIBUTION

    diff = st.get("diff", 0)
    if c + d + h != diff:
        await update.effective_message.reply_text(
            f"Сума ({c+d+h}) не дорівнює пробігу ({diff}). Введи ще раз."
        )
        return STATE_WAITING_DISTRIBUTION

    st["city"] = c
    st["district"] = d
    st["highway"] = h
    user_state[uid] = st

    text = (
        "<b>Підтверди запис:</b>\n"
        f"Одометр: <b>{st['odometer']}</b>\n"
        f"Пробіг: <b>{diff}</b>\n"
        f"Місто/Округ/Траса: <b>{c}/{d}/{h}</b>"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Підтвердити", callback_data="confirm")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="reset")],
        ]
    )
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    return STATE_WAITING_CONFIRMATION

async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    uid = update.effective_user.id
    st = user_state.get(uid)
    if not st:
        await update.callback_query.edit_message_text("Немає даних для збереження.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    # Формування рядка під вашу структуру колонок (A..N).
    # Якщо у вас складніші розрахунки (exact/rounded), підставте свої формули.
    date_s = now_kyiv().strftime("%d.%m.%Y %H:%M")
    od = st["odometer"]
    diff = st["diff"]
    c = st["city"]
    d = st["district"]
    h = st["highway"]

    # Заглушки exact/rounded (замініть своїми обчисленнями, якщо треба)
    city_exact = c
    city_rounded = c
    district_exact = d
    district_rounded = d
    highway_exact = h
    highway_rounded = h
    total_exact = diff
    total_rounded = diff

    row = [
        date_s,              # A: дата/час
        od,                  # B: одометр
        diff,                # C: різниця
        c, city_exact, city_rounded,
        d, district_exact, district_rounded,
        h, highway_exact, highway_rounded,
        total_exact, total_rounded
    ]

    try:
        append_row_and_format(row)
        user_state.pop(uid, None)
        await update.callback_query.edit_message_text("✅ Збережено!", reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.exception(e)
        await update.callback_query.edit_message_text(f"Помилка збереження: {e}", reply_markup=main_menu_keyboard())

    return ConversationHandler.END

# ------------------------------------------------------------
# CALLBACKS (меню)
# ------------------------------------------------------------
async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = update.callback_query.data
    if data == "add":
        await start_add(update, context)
        return
    if data == "last":
        await on_last(update, context)
        return
    if data == "report":
        await on_report(update, context)
        return
    if data == "delete":
        await on_delete(update, context)
        return
    if data == "reset":
        await on_reset(update, context)
        return
    if data == "help":
        await on_help(update, context)
        return
    await update.callback_query.answer("Невідома дія")

# ------------------------------------------------------------
# ІНІЦІАЛІЗАЦІЯ TELEGRAM APPLICATION
# ------------------------------------------------------------
async def init_telegram_app() -> None:
    """
    Створює Application, додає хендлери, ініціалізує & стартує,
    ставить вебхук.
    """
    global telegram_app, ws

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Не задано TELEGRAM_TOKEN")

    # gsheet
    if ws is None:
        init_gsheet()

    # Будуємо Application
    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Handlers
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add, pattern="^add$")],
        states={
            STATE_WAITING_ODOMETER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_odometer)
            ],
            STATE_WAITING_DISTRIBUTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_distribution)
            ],
            STATE_WAITING_CONFIRMATION: [
                CallbackQueryHandler(on_confirm, pattern="^confirm$")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(on_reset, pattern="^reset$"),
            CommandHandler("reset", on_reset),
        ],
        per_user=True,
        per_chat=True,
        per_message=True,  # щоб не було попередження
    )

    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("help", on_help))
    telegram_app.add_handler(CommandHandler("last", on_last))
    telegram_app.add_handler(CommandHandler("report", on_report))
    telegram_app.add_handler(CommandHandler("delete", on_delete))
    telegram_app.add_handler(CallbackQueryHandler(on_menu_click))
    telegram_app.add_handler(conv_handler)

    # Перевірка з'єднання з Telegram API
    bot_info = await telegram_app.bot.get_me()
    logger.info(f"Бот успішно ініціалізовано: {bot_info.username}")

    # Запуск Telegram Application
    await telegram_app.initialize()
    await telegram_app.start()

    # Налаштування вебхука
    if not WEBHOOK_URL:
        raise RuntimeError(
            "Не задано WEBHOOK_URL або RENDER_EXTERNAL_HOSTNAME — не можу поставити вебхук"
        )
    logger.info(f"Ставимо вебхук: {WEBHOOK_URL}")
    await telegram_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    logger.info("Вебхук встановлено")

# ------------------------------------------------------------
# STARLETTE APP + ROUTES
# ------------------------------------------------------------
async def on_startup():
    # Ініціалізуємо Telegram Application та стартуємо його
    await init_telegram_app()

async def on_shutdown():
    # Акуратно зупиняємо Telegram Application і прибираємо вебхук
    global telegram_app
    if telegram_app is not None:
        try:
            await telegram_app.bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:
            logger.warning(f"Не вдалося видалити вебхук при зупинці: {e}")
        try:
            await telegram_app.stop()
        except Exception as e:
            logger.warning(f"Помилка при зупинці Application: {e}")
        try:
            await telegram_app.shutdown()
        except Exception as e:
            logger.warning(f"Помилка при shutdown Application: {e}")

app = Starlette(on_startup=[on_startup], on_shutdown=[on_shutdown])

@app.get("/")
async def root(_: Request):
    return PlainTextResponse("OK")

@app.post("/webhook")
async def webhook(request: Request):
    global telegram_app
    if telegram_app is None:
        logger.error("Application ще не ініціалізовано!")
        return JSONResponse({"status": "error", "detail": "app not initialized"}, status_code=500)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "detail": "invalid json"}, status_code=400)

    try:
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
    except BadRequest as e:
        logger.warning(f"BadRequest: {e}")
    except (TimedOut, NetworkError) as e:
        logger.warning(f"Network issue: {e}")
    except Exception as e:
        logger.exception(e)
        return JSONResponse({"status": "error"}, status_code=500)

    return JSONResponse({"status": "ok"})

# ------------------------------------------------------------
# ЛОКАЛЬНИЙ ЗАПУСК
# ------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
