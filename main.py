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
