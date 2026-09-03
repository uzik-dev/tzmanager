import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from config import (
    GOOGLE_SHEETS_SPREADSHEET_ID,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_SHEETS_TENTS_WORKSHEET,
)

# Часовой пояс МСК (UTC+3) — раньше колонка "Обновлено" писалась через datetime.now()
# (локальное время сервера), из-за чего при хостинге бота не в МСК метка времени в
# таблице расходилась с остальными датами в проекте (все они по МСК).
MSK_TZ = timezone(timedelta(hours=3))

HEADERS = [
    "Палатка №", "Статус", "Игрок", "Telegram ID",
    "Дата окончания", "Последняя оплата (🛢)", "Оплачено всего (🛢)", "Обновлено",
]

_client = None
_worksheet = None
_warned_not_configured = False
_warned_error = False
_tent_rows = None
_sheets_retry_after = 0.0


def is_configured() -> bool:
    return bool(GOOGLE_SHEETS_SPREADSHEET_ID) and bool(GOOGLE_SERVICE_ACCOUNT_FILE)


def _get_worksheet():
    """Ленивая (при первом обращении) синхронная инициализация клиента gspread и листа.
    Блокирующая функция — вызывается только через asyncio.to_thread, чтобы не подвешивать
    event loop бота на время сетевого запроса к Google API."""
    global _client, _worksheet
    if _worksheet is not None:
        return _worksheet

    import gspread  # локальный импорт: если библиотека не установлена, а Sheets не используются — бот всё равно запустится

    _client = gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_FILE)
    spreadsheet = _client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)

    try:
        ws = spreadsheet.worksheet(GOOGLE_SHEETS_TENTS_WORKSHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=GOOGLE_SHEETS_TENTS_WORKSHEET, rows=100, cols=len(HEADERS))
        ws.append_row(HEADERS)

    # Если лист уже существовал, но без заголовков (например, создан вручную) — допишем их.
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(HEADERS)

    _worksheet = ws
    return ws


def _upsert_row_sync(row_values: list, tent_id: int):
    global _tent_rows
    ws = _get_worksheet()
    if _tent_rows is None:
        _tent_rows = {}
        for row_number, value in enumerate(ws.col_values(1)[1:], start=2):
            value = str(value).strip()
            if value.isdigit():
                _tent_rows[int(value)] = row_number
    row_number = _tent_rows.get(int(tent_id))
    if row_number:
        ws.update(f"A{row_number}:H{row_number}", [row_values])
    else:
        ws.append_row(row_values)
        _tent_rows[int(tent_id)] = len(ws.col_values(1))


async def sync_tent(tent_id: int, status: str, player: str, tg_id, end_date: str,
                     last_payment=None, total_paid=None):
    """Обновляет (или создаёт) строку одной палатки в листе "Палатки".
    Никогда не бросает исключение наружу: сбой Google Sheets (нет доступа, нет сети,
    неверный ID таблицы и т.д.) не должен ронять и не должен тормозить основной бот."""
    global _warned_not_configured, _warned_error

    if not is_configured():
        if not _warned_not_configured:
            logging.warning(
                "Google Sheets не настроен (пустой GOOGLE_SHEETS_SPREADSHEET_ID в config.py) — "
                "синхронизация реестра пропускается. Бот при этом работает нормально."
            )
            _warned_not_configured = True
        return

    global _sheets_retry_after
    if time.monotonic() < _sheets_retry_after:
        return

    row = [
        tent_id,
        status,
        player or "—",
        tg_id if tg_id else "—",
        end_date or "—",
        last_payment if last_payment else "—",
        total_paid if total_paid else "—",
        datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M"),
    ]
    try:
        await asyncio.to_thread(_upsert_row_sync, row, tent_id)
    except Exception as e:
        if "429" in str(e) or "Quota exceeded" in str(e):
            _sheets_retry_after = time.monotonic() + 60
            logging.warning("Google Sheets временно ограничил запросы; синхронизация приостановлена на 60 секунд.")
            return
        logging.error(f"❌ Ошибка синхронизации палатки №{tent_id} с Google Sheets: {e}")
        _warned_error = True


async def sync_all_tents(tents_iterable):
    """Массовая синхронизация — например, при старте бота, чтобы реестр сразу
    отражал текущее реальное состояние всех 20 палаток.
    tents_iterable — список кортежей (tent_id, status, player, tg_id, end_date, last_payment, total_paid).
    """
    if not is_configured():
        return
    for row in tents_iterable:
        await sync_tent(*row)
        await asyncio.sleep(1.1)  # Google Sheets API: не больше ~60 записей/мин на сервисный аккаунт по умолчанию
