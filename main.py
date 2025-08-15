# main.py
import os
import re
import json
import math
import pytz
import asyncio
import logging
from datetime import datetime

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse, JSONResponse
from starlette.routing import Route

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import gspread
from google.oauth2.service_account import Credentials
from gspread_formatting import CellFormat, Borders, format_cell_range, TextFormat

# -------------------------
# Налаштування та константи
# -------------------------
tz = pytz.timezone("Europe/Kiev")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bot")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # якщо 0 — доступ без перевірки

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # не змінював назву
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

# опційно (літри на 100 км). Якщо не вказані — розумні дефолти
CITY_L100 = float(os.getenv("CITY_L_PER_100", "12"))
DISTRICT_L100 = float(os.getenv("DISTRICT_L_PER_100", "9"))
HIGHWAY_L100 = float(os.getenv("HIGHWAY_L_PER_100", "7"))

# Стани розмови
WAITING_FOR_ODOMETER, WAITING_FOR_DISTRIBUTION, CONFIRM = range(3)

# Глобальні посилання
telegram_app: Application | None = None
gc = None
worksheet = None

# Тут тимчасово тримаємо дані користувача між кроками
user_data_store: dict[int, dict] = {}

# -------------------------
# Допоміжні функції
# -------------------------
def _build_webhook_url() -> str:
    """
    Нормалізує URL вебхука так, щоб закінчувався рівно на '/webhook'.
    Пріоритет: WEBHOOK_URL -> RENDER_EXTERNAL_HOSTNAME.
    """
    env_url = os.getenv("WEBHOOK_URL")
    if env_url:
        url = env_url.strip()
    else:
        host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if not host:
            raise RuntimeError("Не знайдено WEBHOOK_URL або RENDER_EXTERNAL_HOSTNAME")
        if not host.startswith("http"):
            url = f"https://{host}"
        else:
            url = host

    url = url.rstrip("/")  # прибираємо хвости
    # якщо хтось задав .../webhook/webhook — приводимо до одного
    if url.endswith("/webhook/webhook"):
        url = url[:-8]  # зрізаємо один '/webhook'
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
    worksheet = sh.sheet1  # лишаю як було (перший аркуш)


def _last_row_index() -> int:
    vals = worksheet.get_all_values()
    return len(vals)  # 1-based


def _get_last_odometer() -> int | None:
    vals = worksheet.get_all_values()
    if len(vals) <= 1:
        return None
    *_, last = vals
    try:
        return int(last[1])  # Колонка B — одометр (як у твоїй структурі)
    except Exception:
        return None


def _parse_distribution(text: str, total_km: int) -> tuple[int, int, int] | None:
    """
    Підтримує:
      - "місто 50 район 30 траса 20"
      - "м 50 р 30 т 20"
      - "50/30/20" або "50 30 20"
    Перевіряє, що сума дорівнює total_km.
    """
    t = text.lower().strip()

    # варіант 50/30/20 або "50 30 20"
    m = re.findall(r"\d+", t)
    if len(m) == 3 and all(s.isdigit() for s in m):
        a, b, c = map(int, m[:3])
        if a + b + c == total_km:
            return a, b, c

    # варіант з мітками
    city = district = highway = None

    # місто
    m_city = re.search(r"(місто|город|м)\s*(\d+)", t)
    if m_city:
        city = int(m_city.group(2))
    # район
    m_dist = re.search(r"(район|р)\s*(\d+)", t)
    if m_dist:
        district = int(m_dist.group(2))
    # траса/шосе
    m_high = re.search(r"(траса|шосе|т)\s*(\d+)", t)
    if m_high:
        highway = int(m_high.group(2))

    parts = [city, district, highway]
    if all(v is not None for v in parts) and sum(parts) == total_km:
        return city, district, highway

    return None


def _fmt(dt_: datetime) -> str:
    return dt_.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_just_added_row(row_index: int):
    """Центрування + рамки по всій новій стрічці A..N."""
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
    rng = f"A{row_index}:N{row_index}"
    format_cell_range(worksheet, rng, cell_fmt)


# -------------------------
# Handlers бота
# -------------------------
def _main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Додати запис", callback_data="add"),
            InlineKeyboardButton("ℹ️ Останній запис", callback_data="last"),
        ],
        [
            InlineKeyboardButton("🗑 Видалити останній", callback_data="delete"),
            InlineKeyboardButton("📊 Звіт місяця", callback_data="report"),
        ],
        [
            InlineKeyboardButton("🔁 Скинути", callback_data="reset"),
            InlineKeyboardButton("❓ Допомога", callback_data="help"),
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if OWNER_ID and uid != OWNER_ID:
        await update.message.reply_text("❌ У тебе немає доступу.")
        return
    user_data_store.pop(uid, None)
    await update.message.reply_text(
        "Привіт! Обери дію нижче 👇",
        reply_markup=_main_keyboard()
    )


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if OWNER_ID and uid != OWNER_ID:
        await q.edit_message_text("❌ У тебе немає доступу.")
        return ConversationHandler.END

    data = q.data

    if data == "add":
        # Питаємо одометр
        last_odo = _get_last_odometer()
        hint = f" (попередній: {last_odo})" if last_odo is not None else ""
        await q.edit_message_text(
            f"Введи *поточний одометр*{hint}:", parse_mode="Markdown"
        )
        return WAITING_FOR_ODOMETER

    elif data == "last":
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

    elif data == "delete":
        r = _last_row_index()
        if r <= 1:
            await q.edit_message_text("Нічого видаляти.")
        else:
            worksheet.delete_rows(r)
            await q.edit_message_text("✅ Останній запис видалено.")
        return ConversationHandler.END

    elif data == "report":
        # Простий звіт за поточний місяць: сума колонки N (total_rounded)
        now = datetime.now(tz)
        month = now.strftime("%Y-%m")
        vals = worksheet.get_all_values()
        total = 0.0
        cnt = 0
        for i, row in enumerate(vals[1:], start=2):
            # row[0] — дата-час у str
            if row and row[0].startswith(month):
                try:
                    total += float(row[13])
                    cnt += 1
                except Exception:
                    pass
        await q.edit_message_text(f"📊 Записів: {cnt}\nΣ за {month}: {round(total,2)} л")
        return ConversationHandler.END

    elif data == "reset":
        user_data_store.pop(uid, None)
        await q.edit_message_text("Скинуто. Обери дію:", reply_markup=_main_keyboard())
        return ConversationHandler.END

    elif data == "help":
        await q.edit_message_text(
            "Додати запис → введи одометр → введи розподіл пробігу (наприклад: "
            "`місто 50 район 30 траса 20` або `50/30/20`). Сума має дорівнювати пробігу.",
            parse_mode="Markdown",
            reply_markup=_main_keyboard()
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
    if prev is None:
        diff = 0
    else:
        diff = odo - prev
        if diff <= 0:
            await update.message.reply_text(
                f"Новий одометр ({odo}) має бути більший за попередній ({prev}). Спробуй ще."
            )
            return WAITING_FOR_ODOMETER

    user_data_store[uid] = {"odometer": odo, "diff": diff}
    eq = (diff // 3) if diff else 0
    await update.message.reply_text(
        "Введи розподіл пробігу по *місто/район/траса*.\n"
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

    dist_raw = update.message.text or ""
    city_km, district_km, highway_km = (0, 0, 0)
    parsed = _parse_distribution(dist_raw, data["diff"])
    if not parsed:
        await update.message.reply_text(
            "Не вдалось розібрати розподіл. Перевір приклад та суму кілометрів."
        )
        return WAITING_FOR_DISTRIBUTION

    city_km, district_km, highway_km = parsed
    data.update({
        "city_km": city_km,
        "district_km": district_km,
        "highway_km": highway_km,
    })

    # Розрахунки пального (exact та округлення до 2 знаків)
    city_exact = city_km * CITY_L100 / 100.0
    district_exact = district_km * DISTRICT_L100 / 100.0
    highway_exact = highway_km * HIGHWAY_L100 / 100.0

    def r2(x): return round(x + 1e-9, 2)

    data.update({
        "city_exact": city_exact,
        "city_rounded": r2(city_exact),
        "district_exact": district_exact,
        "district_rounded": r2(district_exact),
        "highway_exact": highway_exact,
        "highway_rounded": r2(highway_exact),
    })
    total_exact = city_exact + district_exact + highway_exact
    data.update({
        "total_exact": total_exact,
        "total_rounded": r2(total_exact),
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
        await q.edit_message_text("Дані загублено. Спробуй ще раз /start.")
        return ConversationHandler.END

    # Формуємо рядок (A..N = 14 колонок)
    now = datetime.now(tz)
    row = [
        now.strftime("%Y-%m-%d %H:%M:%S"),             # A: дата
        str(data["odometer"]),                          # B: одометр
        str(data["diff"]),                              # C: пробіг
        str(data["city_km"]),                           # D
        f"{data['city_exact']:.4f}",                    # E
        f"{data['city_rounded']:.2f}",                  # F
        str(data["district_km"]),                       # G
        f"{data['district_exact']:.4f}",                # H
        f"{data['district_rounded']:.2f}",              # I
        str(data["highway_km"]),                        # J
        f"{data['highway_exact']:.4f}",                 # K
        f"{data['highway_rounded']:.2f}",               # L
        f"{data['total_exact']:.4f}",                   # M
        f"{data['total_rounded']:.2f}",                 # N
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


# -------------------------
# Ініціалізація PTB + Starlette
# -------------------------
async def init_telegram_app():
    global telegram_app
    if telegram_app is not None:
        return  # вже ініціалізовано

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Не знайдено TELEGRAM_TOKEN")

    _authorize_gspread()

    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Розмова
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start),
                      CallbackQueryHandler(handle_buttons)],
        states={
            WAITING_FOR_ODOMETER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_odometer)
            ],
            WAITING_FOR_DISTRIBUTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_distribution)
            ],
            CONFIRM: [
                CallbackQueryHandler(confirm_save, pattern="^save$"),
                CallbackQueryHandler(cancel_save, pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_save)],
        per_chat=True,
        per_user=True,
        per_message=True,  # щоб не було попередження
    )

    telegram_app.add_handler(conv_handler)
    telegram_app.add_handler(CommandHandler("help", start))  # простий /help -> меню

    # ВАЖЛИВО: ініціалізуємо застосунок, але не запускаємо polling
    await telegram_app.initialize()

    # Ставимо вебхук
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


# -------------------------
# Starlette routes
# -------------------------
async def home(request: Request):
    # підтримуємо GET і HEAD
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

# ------------- локальний запуск -------------
if __name__ == "__main__":
    import uvicorn
    # локально вебхук не потрібен, але ініціалізація все одно має пройти для handlers
    async def _local():
        await init_telegram_app()
    asyncio.run(_local())

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
