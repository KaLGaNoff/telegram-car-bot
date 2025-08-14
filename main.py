import os
import re
import json
import logging
from datetime import datetime

import gspread
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ConversationHandler, ContextTypes
)
from gspread_formatting import (
    CellFormat, TextFormat, Borders, Border, Color, format_cell_range
)

# ---------------------- Налаштування логування ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

# ---------------------- Константи/Secrets ----------------------
OWNER_ID = 270380991

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")

if not TELEGRAM_TOKEN:
    raise RuntimeError("❌ TELEGRAM_TOKEN не знайдено у змінних середовища")
if not GOOGLE_SHEET_ID:
    raise RuntimeError("❌ GOOGLE_SHEET_ID не знайдено у змінних середовища")
if not SERVICE_ACCOUNT_JSON:
    raise RuntimeError("❌ SERVICE_ACCOUNT_JSON не знайдено у змінних середовища")

# ---------------------- Google Sheets ----------------------
credentials = json.loads(SERVICE_ACCOUNT_JSON)
client = gspread.service_account_from_dict(credentials)
sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

# ---------------------- Стани діалогу ----------------------
WAITING_FOR_ODOMETER, WAITING_FOR_DISTRIBUTION, CONFIRMATION = range(3)

# Тимчасові дані користувача
user_data_store: dict[int, dict] = {}

# ---------------------- Допоміжні ----------------------
def _int_str(x) -> str:
    """Повертає ціле число як рядок без .0"""
    return str(int(float(x)))

def _is_number(s: str) -> bool:
    s = s.strip().replace(",", ".")
    try:
        float(s)
        return True
    except ValueError:
        return False

def _format_new_row_style(row_index: int):
    """Центрування та рамка для нового рядка"""
    try:
        fmt = CellFormat(
            horizontalAlignment='CENTER',
            textFormat=TextFormat(bold=False),
            borders=Borders(
                top=Border(style='SOLID', color=Color(0, 0, 0)),
                bottom=Border(style='SOLID', color=Color(0, 0, 0)),
                left=Border(style='SOLID', color=Color(0, 0, 0)),
                right=Border(style='SOLID', color=Color(0, 0, 0)),
            ),
        )
        # Стовпці A..N (14)
        format_cell_range(sheet, f"A{row_index}:N{row_index}", fmt)
    except Exception as e:
        log.warning("Не вдалося застосувати формат до рядка %s: %s", row_index, e)

def _nice_last_rows_text(rows: list[list[str]], limit: int = 5) -> str:
    """Акуратний вивід останніх записів (без шапки, якщо вона є)"""
    data = rows[:]
    if data and data[0] and data[0][0].strip().lower() in ("дата", "date"):
        data = data[1:]
    if not data:
        return "📊 Таблиця порожня."

    tail = data[-limit:]
    lines = ["📊 *Останні записи:*\n"]
    # Візьмемо перші 5 колонок для компактності: Дата | Одометр | Пробіг | Місто | Розхід місто
    for r in tail:
        d = (r[0] if len(r) > 0 else "")
        odo = (r[1] if len(r) > 1 else "")
        diff = (r[2] if len(r) > 2 else "")
        city_km = (r[3] if len(r) > 3 else "")
        city_l = (r[4] if len(r) > 4 else "")
        lines.append(f" • {d} | {odo} | {diff} | {city_km} | {city_l}")
    return "\n".join(lines)

def _build_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Додати пробіг", callback_data="add")],
        [InlineKeyboardButton("🗑 Видалити останній запис", callback_data="delete")],
        [InlineKeyboardButton("🧾 Останній запис", callback_data="last")],
        [InlineKeyboardButton("📊 Звіт (5 записів)", callback_data="report")],
        [InlineKeyboardButton("♻️ Скинути", callback_data="reset")],
        [InlineKeyboardButton("ℹ️ Допомога", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ---------------------- Обробники бота ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ У тебе немає доступу до цього бота.")
        return
    await update.message.reply_text("👋 Обери дію:", reply_markup=_build_menu_keyboard())

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        await query.edit_message_text("❌ У тебе немає доступу.")
        return

    data = query.data
    if data == "add":
        await query.edit_message_text("Введи поточний одометр (число):")
        return WAITING_FOR_ODOMETER

    elif data == "delete":
        rows = sheet.get_all_values()
        if rows and len(rows) >= 1:
            sheet.delete_rows(len(rows))  # видаляємо останній рядок (шапку не чіпаємо, якщо є)
            await query.edit_message_text("🗑 Останній запис видалено.")
        else:
            await query.edit_message_text("⚠️ Таблиця порожня.")

    elif data == "report":
        rows = sheet.get_all_values()
        await query.edit_message_text(_nice_last_rows_text(rows), parse_mode="Markdown")

    elif data == "last":
        rows = sheet.get_all_values()
        if not rows or len(rows) <= 1:
            await query.edit_message_text("🧾 Останнього запису немає.")
            return
        # Якщо є шапка, беремо передостанній індекс як останній даний рядок
        body = rows[1:] if (rows and rows[0] and rows[0][0].strip().lower() in ("дата", "date")) else rows
        last = body[-1] if body else []
        # Розкладаємо красиво
        text = (
            "🧾 *Останній запис:*\n"
            f"• Дата: {last[0] if len(last)>0 else ''}\n"
            f"• Одометр: {last[1] if len(last)>1 else ''}\n"
            f"• Пробіг: {last[2] if len(last)>2 else ''} км\n"
            f"• Місто: {last[3] if len(last)>3 else ''} км → {last[4] if len(last)>4 else ''} л (≈ {last[5] if len(last)>5 else ''})\n"
            f"• Район: {last[6] if len(last)>6 else ''} км → {last[7] if len(last)>7 else ''} л (≈ {last[8] if len(last)>8 else ''})\n"
            f"• Траса: {last[9] if len(last)>9 else ''} км → {last[10] if len(last)>10 else ''} л (≈ {last[11] if len(last)>11 else ''})\n"
            f"• Разом: {last[12] if len(last)>12 else ''} л (≈ {last[13] if len(last)>13 else ''})"
        )
        await query.edit_message_text(text, parse_mode="Markdown")

    elif data == "reset":
        user_data_store.pop(query.from_user.id, None)
        await query.edit_message_text("♻️ Стан скинуто.", reply_markup=_build_menu_keyboard())

    elif data == "help":
        await query.edit_message_text(
            "ℹ️ Натисни *«Додати пробіг»* і дотримуйся інструкцій.\n"
            "• Одометр — лише число\n"
            "• Розподіл приклад: `місто 50 район 30 траса 20`\n"
            "• Розподіл має дорівнювати пробігу за період\n",
            parse_mode="Markdown"
        )

# Крок 1 — Введення одометра
async def handle_odometer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not _is_number(text):
        await update.message.reply_text("❗️ Введи число, напр. `53200`", parse_mode="Markdown")
        return WAITING_FOR_ODOMETER

    odometer = int(float(text.replace(",", ".")))
    rows = sheet.get_all_values()

    if len(rows) >= 2:
        prev_odo = int(float(rows[-1][1]))
    else:
        prev_odo = 0

    diff = odometer - prev_odo
    if diff <= 0:
        await update.message.reply_text("❗️ Одометр має бути більший за попередній.")
        return WAITING_FOR_ODOMETER

    user_data_store[update.effective_user.id] = {"odometer": odometer, "diff": diff}

    await update.message.reply_text(
        f"📏 Попередній одометр: {prev_odo}\n"
        f"📍 Поточний одометр: {odometer}\n"
        f"🔄 Пробіг за період: {diff} км\n\n"
        "🛣 Введи розподіл пробігу (наприклад: `місто 50 район 30 траса 6`):",
        parse_mode="Markdown"
    )
    return WAITING_FOR_DISTRIBUTION

# Крок 2 — Введення розподілу
async def handle_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    user_id = update.effective_user.id
    data = user_data_store.get(user_id, {})

    if not data:
        await update.message.reply_text("⚠️ Дані загублено. Почни знову.")
        return ConversationHandler.END

    # Шукаємо цілі числа після ключових слів (місто|район|траса)
    city_km = district_km = highway_km = 0
    for name, value in re.findall(r"(місто|район|трас[аиі])\s+(\d+)", text, flags=re.IGNORECASE):
        if name.startswith("міст"):
            city_km = int(value)
        elif name.startswith("район"):
            district_km = int(value)
        else:
            highway_km = int(value)

    total_entered = city_km + district_km + highway_km
    if total_entered != data["diff"]:
        await update.message.reply_text(
            f"⚠️ Сума ({total_entered}) не дорівнює пробігу за період ({data['diff']}). Виправ."
        )
        return WAITING_FOR_DISTRIBUTION

    # Формули розрахунку (л/100км)
    def calc(l_per_100, km):
        exact = round(km * l_per_100 / 100, 4)
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

    summary = (
        "📋 *Новий запис:*\n"
        f"• Одометр: {data['odometer']}\n"
        f"• Пробіг: {data['diff']} км\n"
        f"• Місто: {city_km} км → {c_exact} л (≈ {c_rounded})\n"
        f"• Район: {district_km} км → {d_exact} л (≈ {d_rounded})\n"
        f"• Траса: {highway_km} км → {h_exact} л (≈ {h_rounded})\n"
        f"• Загалом: {total_exact} л (≈ {total_rounded})\n\n"
        "✅ Зберегти?"
    )

    keyboard = [
        [InlineKeyboardButton("✅ Так", callback_data="confirm_yes")],
        [InlineKeyboardButton("❌ Ні", callback_data="confirm_no")]
    ]
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return CONFIRMATION

# Крок 3 — Підтвердження
async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "confirm_no":
        user_data_store.pop(user_id, None)
        await query.edit_message_text("❌ Скасовано.", reply_markup=_build_menu_keyboard())
        return ConversationHandler.END

    data = user_data_store.pop(user_id, {})
    if not data:
        await query.edit_message_text("⚠️ Дані не знайдено.", reply_markup=_build_menu_keyboard())
        return ConversationHandler.END

    today = datetime.now().strftime("%d.%m.%Y")
    row = [
        today,
        str(data["odometer"]),
        str(data["diff"]),
        str(int(data["city_km"])),
        str(data["city_exact"]).replace('.', ','),
        str(data["city_rounded"]),
        str(int(data["district_km"])),
        str(data["district_exact"]).replace('.', ','),
        str(data["district_rounded"]),
        str(int(data["highway_km"])),
        str(data["highway_exact"]).replace('.', ','),
        str(data["highway_rounded"]),
        str(data["total_exact"]).replace('.', ','),
        str(data["total_rounded"])
    ]
    sheet.append_row(row)
    # Вирівнювання та рамка для доданого рядка
    row_index = len(sheet.get_all_values())
    _format_new_row_style(row_index)

    await query.edit_message_text("✅ Запис збережено.", reply_markup=_build_menu_keyboard())
    return ConversationHandler.END

# ---------------------- Ініціалізація PTB Application ----------------------
telegram_app: Application | None = None

def _build_telegram_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_button)],
        states={
            WAITING_FOR_ODOMETER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_odometer)],
            WAITING_FOR_DISTRIBUTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_distribution)],
            CONFIRMATION: [CallbackQueryHandler(handle_confirmation, pattern="^confirm_(yes|no)$")]
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv_handler)
    return app

# ---------------------- FastAPI (Render) ----------------------
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    global telegram_app
    telegram_app = _build_telegram_app()
    # Ініціалізуємо та стартуємо PTB-додаток для мануальної обробки апдейтів
    await telegram_app.initialize()
    await telegram_app.start()

    # Вебхук
    base = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base:
        webhook_url = base.rstrip("/") + "/webhook"
        await telegram_app.bot.set_webhook(url=webhook_url)
        log.info("Вебхук встановлено: %s", webhook_url)
    else:
        log.warning("WEBHOOK_URL/RENDER_EXTERNAL_URL не задано – вебхук не встановлено.")

@app.on_event("shutdown")
async def on_shutdown():
    global telegram_app
    if telegram_app:
        try:
            await telegram_app.bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:
            log.warning("Не вдалося видалити вебхук: %s", e)
        await telegram_app.stop()
        await telegram_app.shutdown()
        telegram_app = None

# Healthcheck
@app.get("/", response_class=PlainTextResponse)
async def root_get():
    return "Bot is running"

@app.head("/", response_class=PlainTextResponse)
async def root_head():
    return ""

# Прийом апдейтів від Telegram
@app.post("/webhook")
async def webhook(request: Request):
    if telegram_app is None:
        log.error("Telegram Application не ініціалізовано")
        return JSONResponse({"ok": False, "error": "app_not_initialized"}, status_code=500)

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
