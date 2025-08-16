import os
import re
import json
import pytz
import logging
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

# =========================
# НАЛАШТУВАННЯ / КОНСТАНТИ
# =========================
tz = pytz.timezone("Europe/Kiev")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("bot")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

CITY_L100 = float(os.getenv("CITY_L_PER_100", "12"))
DISTRICT_L100 = float(os.getenv("DISTRICT_L_PER_100", "9"))
HIGHWAY_L100 = float(os.getenv("HIGHWAY_L_PER_100", "7"))

WAITING_FOR_ODOMETER, WAITING_FOR_DISTRIBUTION, CONFIRM = range(3)

telegram_app: Application | None = None
gc = None
worksheet = None
user_data_store: dict[int, dict] = {}

# =========================
# УТИЛІТИ
# =========================
def _build_webhook_url() -> str:
    env_url = os.getenv("WEBHOOK_URL")
    if env_url:
        url = env_url.strip()
    else:
        host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if not host:
            raise RuntimeError("Не знайдено WEBHOOK_URL або RENDER_EXTERNAL_HOSTNAME")
        url = f"https://{host}" if not host.startswith("http") else host

    url = url.rstrip("/")
    if url.endswith("/webhook/webhook"):
        url = url[:-8]
    if not url.endswith("/webhook"):
        url = f"{url}/webhook"
    return url


def _authorize_gspread():
    global gc, worksheet
    if not (GOOGLE_SHEET_ID and SERVICE_ACCOUNT_JSON):
        raise RuntimeError("Немає GOOGLE_SHEET_ID або SERVICE_ACCOUNT_JSON")
    creds_info = json.loads(SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    worksheet = sh.sheet1


def _last_row_index() -> int:
    return len(worksheet.get_all_values())


def _get_last_odometer() -> int | None:
    vals = worksheet.get_all_values()
    if len(vals) <= 1:
        return None
    try:
        return int(vals[-1][1])
    except Exception:
        return None


def _parse_distribution(text: str, total_km: int):
    t = text.lower().strip()
    nums = re.findall(r"\d+", t)
    if len(nums) == 3:
        a, b, c = map(int, nums[:3])
        if a + b + c == total_km:
            return a, b, c

    def pick(patterns):
        for p in patterns:
            m = re.search(fr"{p}\s*(\d+)", t)
            if m:
                return int(m.group(1))
        return None

    city = pick(["місто", "город", r"\bм\b"])
    district = pick(["район", r"\bр\b"])
    highway = pick(["траса", "шосе", r"\bт\b"])

    if None not in (city, district, highway) and city + district + highway == total_km:
        return city, district, highway
    return None


def _format_just_added_row(row_index: int):
    cell_fmt = CellFormat(
        textFormat=TextFormat(bold=False),
        horizontalAlignment="CENTER",
        borders=Borders(
            top={"style": "SOLID"},
            bottom={"style": "SOLID"},
            left={"style": "SOLID"},
            right={"style": "SOLID"},
        ),
    )
    format_cell_range(worksheet, f"A{row_index}:N{row_index}", cell_fmt)


# =========================
# KEYBOARD
# =========================
def _main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати запис", callback_data="add"),
         InlineKeyboardButton("ℹ️ Останній запис", callback_data="last")],
        [InlineKeyboardButton("🗑 Видалити останній", callback_data="delete"),
         InlineKeyboardButton("📊 Звіт місяця", callback_data="report")],
        [InlineKeyboardButton("🔁 Скинути", callback_data="reset"),
         InlineKeyboardButton("❓ Допомога", callback_data="help")],
    ])


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if OWNER_ID and uid != OWNER_ID:
        await update.message.reply_text("❌ У тебе немає доступу.")
        return
    user_data_store.pop(uid, None)
    await update.message.reply_text("Привіт! Обери дію 👇", reply_markup=_main_keyboard())


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if OWNER_ID and uid != OWNER_ID:
        await q.edit_message_text("❌ У тебе немає доступу.")
        return ConversationHandler.END

    data = q.data
    if data == "add":
        last_odo = _get_last_odometer()
        hint = f" (попередній: {last_odo})" if last_odo is not None else ""
        await q.edit_message_text(f"Введи *поточний одометр*{hint}:", parse_mode="Markdown")
        return WAITING_FOR_ODOMETER

    if data == "last":
        vals = worksheet.get_all_values()
        if len(vals) <= 1:
            await q.edit_message_text("Записів ще немає.")
            return ConversationHandler.END
        last = vals[-1]
        msg = (
            f"🕒 {last[0]}\n"
            f"📍 Одометр: {last[1]}\n"
            f"🔄 Пробіг: {last[2]} км\n"
            f"🏙 Місто: {last[3]} км ({last[4]} → {last[5]} л)\n"
            f"🏞 Район: {last[6]} км ({last[7]} → {last[8]} л)\n"
            f"🛣 Траса: {last[9]} км ({last[10]} → {last[11]} л)\n"
            f"Σ Паливо: {last[12]} → {last[13]} л"
        )
        await q.edit_message_text(msg)
        return ConversationHandler.END

    if data == "delete":
        r = _last_row_index()
        if r <= 1:
            await q.edit_message_text("Нічого видаляти.")
        else:
            worksheet.delete_rows(r)
            await q.edit_message_text("✅ Останній запис видалено.")
        return ConversationHandler.END

    if data == "report":
        now = datetime.now(tz)
        month = now.strftime("%Y-%m")
        vals = worksheet.get_all_values()
        total = 0.0
        cnt = 0
        for row in vals[1:]:
            if row and row[0].startswith(month):
                try:
                    total += float(row[13])
                    cnt += 1
                except Exception:
                    pass
        await q.edit_message_text(f"📊 Записів: {cnt}\nΣ за {month}: {round(total,2)} л")
        return ConversationHandler.END

    if data == "reset":
        user_data_store.pop(uid, None)
        await q.edit_message_text("Скинуто. Обери дію:", reply_markup=_main_keyboard())
        return ConversationHandler.END

    if data == "help":
        await q.edit_message_text(
            "Додати запис → введи одометр → введи розподіл (напр. `місто 50 район 30 траса 20` "
            "або `50/30/20`). Сума = пробіг.",
            parse_mode="Markdown", reply_markup=_main_keyboard()
        )
        return ConversationHandler.END

    return ConversationHandler.END


async def handle_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Введи ціле число одометра.")
        return WAITING_FOR_ODOMETER

    odo = int(txt)
    prev = _get_last_odometer()
    diff = 0 if prev is None else odo - prev
    if prev is not None and diff <= 0:
        await update.message.reply_text(
            f"Новий одометр ({odo}) має бути > попереднього ({prev}). Спробуй ще."
        )
        return WAITING_FOR_ODOMETER

    user_data_store[uid] = {"odometer": odo, "diff": diff}
    eq = (diff // 3) if diff else 0
    await update.message.reply_text(
        "Введи розподіл *місто/район/траса*.\n"
        f"Напр.: `місто {eq} район {eq} траса {diff-2*eq}` або `50/30/20`.\n"
        f"Сума має дорівнювати *{diff}* км.",
        parse_mode="Markdown"
    )
    return WAITING_FOR_DISTRIBUTION


async def handle_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    data = user_data_store.get(uid)
    if not data:
        await update.message.reply_text("Натисни /start і почнемо заново.")
        return ConversationHandler.END

    parsed = _parse_distribution(update.message.text or "", data["diff"])
    if not parsed:
        await update.message.reply_text("Не вдалось розібрати розподіл. Перевір приклад і суму.")
        return WAITING_FOR_DISTRIBUTION

    city_km, district_km, highway_km = parsed

    def r2(x: float) -> float:
        return round(x + 1e-9, 2)

    city_exact = city_km * CITY_L100 / 100.0
    district_exact = district_km * DISTRICT_L100 / 100.0
    highway_exact = highway_km * HIGHWAY_L100 / 100.0
    total_exact = city_exact + district_exact + highway_exact

    data.update({
        "city_km": city_km, "district_km": district_km, "highway_km": highway_km,
        "city_exact": city_exact, "city_rounded": r2(city_exact),
        "district_exact": district_exact, "district_rounded": r2(district_exact),
        "highway_exact": highway_exact, "highway_rounded": r2(highway_exact),
        "total_exact": total_exact, "total_rounded": r2(total_exact),
    })
    user_data_store[uid] = data

    text = (
        f"📍 Одометр: {data['odometer']}\n"
        f"🔄 Пробіг: {data['diff']} км\n\n"
        f"🏙 Місто: {city_km} км → {r2(city_exact)} л\n"
        f"🏞 Район: {district_km} км → {r2(district_exact)} л\n"
        f"🛣 Траса: {highway_km} км → {r2(highway_exact)} л\n"
        f"Σ Всього: {r2(total_exact)} л\n\n"
        f"Зберегти запис?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Зберегти", callback_data="save"),
         InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
    ])
    await update.message.reply_text(text, reply_markup=keyboard)
    return CONFIRM


async def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = user_data_store.get(uid)
    if not data:
        await q.edit_message_text("Дані загублено. Спробуй /start.")
        return ConversationHandler.END

    now = datetime.now(tz)
    row = [
        now.strftime("%Y-%m-%d %H:%M:%S"),
        str(data["odometer"]),
        str(data["diff"]),
        str(data["city_km"]),
        f"{data['city_exact']:.4f}",
        f"{data['city_rounded']:.2f}",
        str(data["district_km"]),
        f"{data['district_exact']:.4f}",
        f"{data['district_rounded']:.2f}",
        str(data["highway_km"]),
        f"{data['highway_exact']:.4f}",
        f"{data['highway_rounded']:.2f}",
        f"{data['total_exact']:.4f}",
        f"{data['total_rounded']:.2f}",
    ]
    worksheet.append_row(row, value_input_option="RAW")
    r = _last_row_index()
    _format_just_added_row(r)

    user_data_store.pop(uid, None)
    await q.edit_message_text("✅ Запис збережено.", reply_markup=_main_keyboard())
    return ConversationHandler.END


async def cancel_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Скасовано.", reply_markup=_main_keyboard())
    else:
        await update.message.reply_text("Скасовано.", reply_markup=_main_keyboard())
    return ConversationHandler.END


# =========================
# ІНІЦІАЛІЗАЦІЯ / ЖИТТЄВИЙ ЦИКЛ
# =========================
async def init_telegram_app():
    global telegram_app
    if telegram_app is not None:
        return

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Не знайдено TELEGRAM_TOKEN")

    _authorize_gspread()

    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(handle_buttons)],
        states={
            WAITING_FOR_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_odometer)],
            WAITING_FOR_DISTRIBUTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_distribution)],
            CONFIRM: [
                CallbackQueryHandler(confirm_save, pattern="^save$"),
                CallbackQueryHandler(cancel_save, pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_save)],
        per_chat=True,
        per_user=True,
    )

    telegram_app.add_handler(conv_handler)
    telegram_app.add_handler(CommandHandler("help", start))

    await telegram_app.initialize()

    webhook_url = _build_webhook_url()
    await telegram_app.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    logger.info(f"Вебхук встановлено: {webhook_url}")


async def shutdown_telegram_app():
    global telegram_app
    if telegram_app is None:
        return
    try:
        await telegram_app.bot.delete_webhook()
    except Exception as e:
        logger.warning(f"Помилка deleteWebhook: {e}")
    try:
        await telegram_app.shutdown()
    except Exception as e:
        logger.warning(f"Помилка Application.shutdown: {e}")
    telegram_app = None
    logger.info("PTB зупинено")


# =========================
# HTTP ROUTES
# =========================
async def home(request: Request):
    return PlainTextResponse("Bot is running")

async def webhook(request: Request):
    global telegram_app
    if telegram_app is None:
        return Response("App not initialized", status_code=503)

    try:
        data = await request.json()
    except Exception:
        return Response("Invalid JSON", status_code=400)

    try:
        update = Update.de_json(data, bot=telegram_app.bot)
    except Exception:
        return Response("Failed to deserialize update", status_code=400)

    try:
        await telegram_app.process_update(update)
    except Exception as e:
        logger.exception("Помилка обробки апдейту")
        return Response(f"Webhook error: {e}", status_code=500)

    return Response(status_code=200)


routes = [
    Route("/", home, methods=["GET", "HEAD"]),
    Route("/webhook", webhook, methods=["POST"]),
]

app = Starlette(
    routes=routes,
    on_startup=[init_telegram_app],
    on_shutdown=[shutdown_telegram_app],
)

# локальний запуск
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
