import asyncio
import logging
import sqlite3
import re
import os
import pandas as pd
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import FSInputFile
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    ADMIN_ID, BOT_TOKEN, LOG_CHANNEL_ID, TARIFFS, VIEWER_ID,
    MODERATOR_IDS, SUPER_ADMIN_ID, TAX_OFFICER_IDS, TAX_RATE,
    GOOGLE_SHEETS_SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SHEETS_TENTS_WORKSHEET
)
import sheets_sync
import firestore_sync

logging.basicConfig(level=logging.WARNING)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Часовой пояс МСК (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))


# === СИСТЕМА ЛОГИРОВАНИЯ ОШИБОК В TELEGRAM-КАНАЛ ===
# Стандартный logging.Handler: любой logging.error(...)/logging.exception(...) в любом
# месте кода теперь автоматически долетает в LOG_CHANNEL_ID, без ручных await send_log(...).
# Работает асинхронно через очередь, чтобы не блокировать event loop синхронным вызовом.
class TelegramErrorHandler(logging.Handler):
    def __init__(self, level=logging.ERROR):
        super().__init__(level)
        self._queue: "asyncio.Queue[str] | None" = None
        self._worker_started = False

    def _ensure_worker(self):
        if self._worker_started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._queue = asyncio.Queue()
        loop.create_task(self._worker())
        self._worker_started = True

    async def _worker(self):
        while True:
            text = await self._queue.get()
            try:
                await bot.send_message(chat_id=LOG_CHANNEL_ID, text=text, parse_mode="HTML")
            except Exception:
                pass  # Не даём ошибке логирования породить бесконечный цикл логирования ошибок

    def emit(self, record: logging.LogRecord):
        if not LOG_CHANNEL_ID:
            return
        try:
            self._ensure_worker()
            if self._queue is None:
                return
            msg = self.format(record)
            text = f"🐞 <b>[ERROR | {record.levelname}]</b>\n<code>{msg[:3500]}</code>"
            self._queue.put_nowait(text)
        except Exception:
            pass


_tg_handler = TelegramErrorHandler(level=logging.ERROR)
_tg_handler.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(message)s", datefmt="%d.%m.%Y %H:%M:%S"))
logging.getLogger().addHandler(_tg_handler)

# === РОЛЕВАЯ СИСТЕМА И ПРОВЕРКА ПРАВ ===
def is_super_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID or user_id == ADMIN_ID

def can_manage_tents(user_id: int) -> bool:
    return is_super_admin(user_id) or user_id in MODERATOR_IDS

def can_export_reports(user_id: int) -> bool:
    return is_super_admin(user_id) or user_id in TAX_OFFICER_IDS


# === ПОСТОЯННАЯ КЛАВИАТУРА ВНИЗУ ЭКРАНА (вместо системной клавиатуры ввода) ===
def main_reply_kb(user_id: int) -> types.ReplyKeyboardMarkup:
    """Кнопки внизу экрана, всегда доступны независимо от текущего меню.
    ВАЖНО: кнопки «Отмена» тут нет намеренно — по актуальному ТЗ отмена доступна
    только на этапе подтверждения загруженного скрина оплаты (inline-кнопка там же)."""
    rows = [[types.KeyboardButton(text="🏠 Главное меню")]]
    if can_manage_tents(user_id):
        rows.append([types.KeyboardButton(text="🛠 Админ-панель")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# === 🧾 ФУНКЦИЯ ГЕНЕРАЦИИ ЭЛЕКТРОННОГО ЧЕКА ===
def generate_receipt_text(receipt_id: str, nick: str, tent_id: int, days: int, price: int, end_date_str: str) -> str:
    return (
        f"🧾 <b>ОФИЦИАЛЬНЫЙ ЧЕК ОБ ОПЛАТЕ АРЕНДЫ #{receipt_id}</b>\n"
        f"──────────────────────────\n"
        f"👤 <b>Арендатор:</b> {nick}\n"
        f"⛺ <b>Объект:</b> Палатка #{tent_id}\n"
        f"⏳ <b>Оплаченный срок:</b> {days} дн.\n"
        f"🛢️ <b>Сумма оплаты:</b> {price} нефти\n"
        f"📅 <b>Действует ДО:</b> {end_date_str} 23:59:59 (МСК)\n"
        f"──────────────────────────\n"
        f"✅ <b>СТАТУС:</b> ПРОВЕДЕНО И ПОДТВЕРЖДЕНО\n"
        f"🏛️ <i>Налоговая служба / Администрация</i>"
    )


# === ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ЛОГИРОВАНИЯ В КАНАЛ ===
async def send_log(text: str):
    """Отправка системного события в закрытый LOG-канал"""
    if LOG_CHANNEL_ID:
        try:
            now = datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M")
            log_message = f"🪵 <b>[LOG | {now}]</b>\n{text}"
            await bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=log_message,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"❌ Ошибка отправки в LOG-канал ({LOG_CHANNEL_ID}): {e}")


# === УВЕДОМЛЕНИЯ АДМИНИСТРАЦИИ ===
# ВАЖНО: раньше уведомления слались только на ADMIN_ID, который в config.py был
# незаполненным плейсхолдером ("1" — несуществующий Telegram ID), поэтому реальный
# админ (SUPER_ADMIN_ID) никогда их не получал. Теперь уведомления уходят сразу
# всем реальным получателям: SUPER_ADMIN_ID, ADMIN_ID (если задан отдельно) и всем MODERATOR_IDS.
def get_admin_recipients() -> list:
    ids = {SUPER_ADMIN_ID, ADMIN_ID, *MODERATOR_IDS}
    return [i for i in ids if i]


async def notify_admins(text: str, parse_mode: str = "HTML"):
    for admin_id in get_admin_recipients():
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode=parse_mode)
        except Exception as e:
            logging.error(f"❌ Не удалось уведомить админа {admin_id}: {e}")


async def send_request_card_to_admins(photo_id: str, caption: str, reply_markup, is_document: bool = False):
    """Отправляет карточку заявки (с чеком и кнопками Подтвердить/Отклонить) всем админам/модераторам.
    Подтверждение на одной карточке помечает заявку обработанной — остальные копии
    при попытке нажать покажут 'Эта заявка уже обработана' (это безопасно)."""
    for admin_id in get_admin_recipients():
        try:
            if is_document:
                await bot.send_document(chat_id=admin_id, document=photo_id, caption=caption, reply_markup=reply_markup)
            else:
                await bot.send_photo(chat_id=admin_id, photo=photo_id, caption=caption, reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"❌ Не удалось отправить карточку заявки админу {admin_id}: {e}")


def extract_proof_file(message: types.Message):
    """Достаёт file_id доказательства оплаты из сообщения, независимо от того, прислано
    оно как сжатое 'фото' или как 'файл/документ'-изображение. Возвращает (file_id, is_document)
    либо (None, None), если в сообщении нет подходящего изображения."""
    if message.photo:
        return message.photo[-1].file_id, False
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return message.document.file_id, True
    return None, None


async def send_proof(chat_id: int, photo_id: str, is_document: bool = False, caption: str = None):
    """Пересылает сохранённое доказательство оплаты корректным методом в зависимости от того,
    было оно фото или документом (иначе Telegram API вернёт ошибку неверного file_id)."""
    if is_document:
        await bot.send_document(chat_id=chat_id, document=photo_id, caption=caption)
    else:
        await bot.send_photo(chat_id=chat_id, photo=photo_id, caption=caption)


async def safe_send(user_id: int, text: str, parse_mode: str = "HTML", reply_markup=None) -> bool:
    """Единая точка отправки личных сообщений игрокам напрямую по user_id.
    Возвращает True/False вместо того, чтобы падать — раньше в разных местах кода
    отправка была разбросана по отдельным try/except с разным поведением, из-за чего
    часть сбоев (заблокировал бота, удалил аккаунт, неверный ID) либо не логировалась,
    либо вообще не обрабатывалась. Здесь же можно централизованно расширить проверку —
    например, сверяться со списком известных tg_id в таблице users."""
    if not user_id:
        logging.error("safe_send: вызван без user_id")
        return False
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        return True
    except Exception as e:
        logging.error(f"❌ Не удалось отправить сообщение игроку {user_id}: {e}")
        return False


# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()

    # ВАЖНО (надёжность БД): включаем WAL-режим — это резко снижает риск повреждения
    # базы при параллельной записи/чтении и при аварийном завершении процесса.
    # Полноценный переезд на PostgreSQL — отдельная инфраструктурная задача, для которой
    # нужны хостинг/строка подключения; см. пояснение в конце. Здесь усиливаем то, что есть.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            nickname TEXT UNIQUE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tents (
            id INTEGER PRIMARY KEY,
            tg_id INTEGER,
            nickname TEXT,
            end_date TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tent_id INTEGER,
            nickname TEXT,
            price INTEGER,
            days INTEGER,
            photo_id TEXT,
            pay_date TEXT,
            pay_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tent_id INTEGER,
            user_id INTEGER,
            tariff_code TEXT,
            photo_id TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            tg_id INTEGER PRIMARY KEY,
            reason TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stats_corrections (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            oil_offset INTEGER DEFAULT 0,
            deals_offset INTEGER DEFAULT 0
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO stats_corrections (id, oil_offset, deals_offset) VALUES (1, 0, 0)")

    # Дедупликация авто-напоминаний за 24ч/6ч до конца аренды — чтобы одно и то же
    # напоминание не улетало игроку повторно на каждом часовом прогоне планировщика.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_reminders (
            tent_id INTEGER,
            milestone TEXT,
            end_date TEXT,
            PRIMARY KEY (tent_id, milestone, end_date)
        )
    """)

    cursor.execute("PRAGMA table_info(payments_history)")
    columns = [col[1] for col in cursor.fetchall()]
    if "photo_id" not in columns:
        cursor.execute("ALTER TABLE payments_history ADD COLUMN photo_id TEXT")
    if "is_document" not in columns:
        cursor.execute("ALTER TABLE payments_history ADD COLUMN is_document INTEGER DEFAULT 0")

    # ВАЖНО: раньше бот принимал доказательство оплаты ТОЛЬКО как сжатое "фото" (F.photo).
    # Если игрок отправлял скриншот как файл/документ (частый случай на ПК — "Отправить как файл",
    # чтобы не терять качество), бот вообще никак на это не реагировал: ни игроку, ни админу
    # ничего не приходило. Теперь принимаются оба варианта, и тип файла запоминается,
    # чтобы потом переслать его админу корректным методом (send_photo или send_document).
    cursor.execute("PRAGMA table_info(pending_requests)")
    columns = [col[1] for col in cursor.fetchall()]
    if "is_document" not in columns:
        cursor.execute("ALTER TABLE pending_requests ADD COLUMN is_document INTEGER DEFAULT 0")

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tents_tg_id ON tents(tg_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payments_tent_id ON payments_history(tent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_tent_id ON pending_requests(tent_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_user_id ON pending_requests(user_id)")

    cursor.execute("SELECT COUNT(*) FROM tents")
    if cursor.fetchone()[0] == 0:
        for i in range(1, 21):
            cursor.execute("INSERT INTO tents (id, tg_id, nickname, end_date) VALUES (?, NULL, NULL, NULL)", (i,))

    conn.commit()
    conn.close()


def is_nickname_taken(nickname: str, exclude_tg_id: int = None) -> bool:
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    if exclude_tg_id:
        cursor.execute("SELECT 1 FROM users WHERE LOWER(nickname) = LOWER(?) AND tg_id != ?", (nickname, exclude_tg_id))
    else:
        cursor.execute("SELECT 1 FROM users WHERE LOWER(nickname) = LOWER(?)", (nickname,))
    res = cursor.fetchone()
    conn.close()
    return bool(res)


def is_blacklisted(tg_id: int) -> bool:
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM blacklist WHERE tg_id = ?", (tg_id,))
    res = cursor.fetchone()
    conn.close()
    return bool(res)


def ban_user(tg_id: int, reason: str = "Нарушение правил"):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO blacklist (tg_id, reason) VALUES (?, ?)", (tg_id, reason))
    conn.commit()
    conn.close()


def unban_user(tg_id: int):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM blacklist WHERE tg_id = ?", (tg_id,))
    conn.commit()
    conn.close()


def save_user(tg_id, username, nickname):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO users (tg_id, username, nickname) VALUES (?, ?, ?)", (tg_id, username, nickname))
    conn.commit()
    conn.close()


async def delete_user_db(tg_id):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
    conn.commit()
    conn.close()

    owned = await firestore_sync.get_user_tent(tg_id)
    if owned:
        await firestore_sync.clear_tent(owned[0])
        await sync_tent_row(owned[0])


def get_all_users():
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id, username, nickname FROM users")
    rows = cursor.fetchall()
    conn.close()
    return rows


async def get_user_tent(tg_id):
    """Теперь читает из Firestore (единый источник данных с веб-приложением и
    Google-таблицей) вместо локальной SQLite."""
    return await firestore_sync.get_user_tent(tg_id)


async def get_tent(tent_id):
    return await firestore_sync.get_tent(tent_id)


async def get_all_tents_list():
    """Список всех 20 палаток (id, tg_id, nickname, end_date) — замена массовых
    'SELECT ... FROM tents' запросов, которые раньше шли напрямую в SQLite."""
    return await firestore_sync.get_all_tents()


def _mirror_payment_to_sqlite(tent_id, nickname, price, days, photo_id, is_document):
    """Локальное зеркало платежа в SQLite payments_history — нужно только для того,
    чтобы существующая генерация Excel-отчётов продолжала работать без переписывания.
    Канонической историей платежей теперь считается payments[] в документе Firestore."""
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    today = datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M")
    cursor.execute(
        "INSERT INTO payments_history (tent_id, nickname, price, days, photo_id, pay_date, is_document) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tent_id, nickname, price, days, photo_id, today, int(is_document))
    )
    conn.commit()
    conn.close()


async def assign_tent_db(tent_id, tg_id, nickname, days=7, price=0, photo_id=None, is_document=False):
    end_date = await firestore_sync.assign_tent(tent_id, tg_id, nickname, days=days, price=price)
    if price:
        await asyncio.to_thread(_mirror_payment_to_sqlite, tent_id, nickname, price, days, photo_id, is_document)
    return end_date


async def clear_tent_db(tent_id):
    await firestore_sync.clear_tent(tent_id)


async def update_tent_date_db(tent_id, new_date_str):
    await firestore_sync.update_tent_date(tent_id, new_date_str)


def tent_status_label(end_date_str) -> str:
    """Единая формулировка статуса палатки по дате окончания — используется и в
    сообщении игроку 'Моя палатка', и при синхронизации в Google Sheets."""
    if not end_date_str:
        return "⚪ Свободна"
    now_msk = datetime.now(MSK_TZ).replace(tzinfo=None)
    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        days_left = (end_date - now_msk).days + 1
    except Exception:
        return "⚪ Свободна"
    if days_left < 0:
        return f"🔴 ПРОСРОЧЕНА на {abs(days_left)} дн."
    if days_left <= 2:
        return f"🟡 Заканчивается (осталось {days_left} дн.)"
    return f"🟢 Активна (осталось {days_left} дн.)"


async def sync_tent_row(tent_id: int):
    """Подтягивает текущее состояние палатки (из Firestore) и её платежей и
    отправляет строку в Google-таблицу реестра. Вызывается после КАЖДОГО изменения
    палатки — не блокирует и не ломает основной функционал бота при сбое."""
    if not sheets_sync.is_configured():
        return
    tent = await get_tent(tent_id)
    if not tent:
        return
    _, tg_id, nickname, end_date = tent

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT price FROM payments_history WHERE tent_id = ? ORDER BY id DESC LIMIT 1",
        (tent_id,)
    )
    last_row = cursor.fetchone()
    cursor.execute("SELECT SUM(price) FROM payments_history WHERE tent_id = ?", (tent_id,))
    total_row = cursor.fetchone()
    conn.close()

    last_payment = last_row[0] if last_row else None
    total_paid = total_row[0] if total_row and total_row[0] else None

    await sheets_sync.sync_tent(
        tent_id=tent_id,
        status=tent_status_label(end_date),
        player=nickname,
        tg_id=tg_id,
        end_date=end_date,
        last_payment=last_payment,
        total_paid=total_paid,
    )


async def extend_tent_db(tent_id, days, price, photo_id, nickname, is_document=False):
    """Продление — бизнес-логика 'от предыдущей даты, либо от сегодня если просрочено'
    теперь живёт в firestore_sync.extend_tent (ровно та же формула, что была здесь)."""
    new_end = await firestore_sync.extend_tent(tent_id, days, price, nickname)
    if price:
        await asyncio.to_thread(_mirror_payment_to_sqlite, tent_id, nickname, price, days, photo_id, is_document)
    return new_end


def get_stats_offsets():
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT oil_offset, deals_offset FROM stats_corrections WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return row if row else (0, 0)


def update_stats_offsets(oil_offset: int, deals_offset: int):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE stats_corrections SET oil_offset = ?, deals_offset = ? WHERE id = 1", (oil_offset, deals_offset))
    conn.commit()
    conn.close()


# === СОСТОЯНИЯ (FSM) ===
class RegisterState(StatesGroup):
    waiting_for_nickname = State()
    waiting_for_edit_nickname = State()

class RenewState(StatesGroup):
    waiting_for_tariff = State()
    waiting_for_photo = State()
    confirming_photo = State()

# ➕ Состояния для самостоятельного выбора и аренды свободной палатки
class RentTentState(StatesGroup):
    waiting_for_tent_choice = State()
    waiting_for_tariff = State()
    waiting_for_photo = State()
    confirming_photo = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()

class EditDateState(StatesGroup):
    waiting_for_new_date = State()

class EditStatsState(StatesGroup):
    waiting_for_oil = State()
    waiting_for_deals = State()

class DeleteUserState(StatesGroup):
    waiting_for_reason = State()

class DMState(StatesGroup):
    waiting_for_message = State()


# === МИДДЛВАРЬ / ПРОВЕРКА НА БАН ===
@dp.message.outer_middleware()
@dp.callback_query.outer_middleware()
async def check_ban_middleware(handler, event, data):
    user = getattr(event, "from_user", None)
    if user and is_blacklisted(user.id):
        if isinstance(event, types.CallbackQuery):
            await event.answer("🚫 Вы заблокированы в системе!", show_alert=True)
        else:
            await event.answer("🚫 Вы заблокированы в Торговой Зоне и не можете использовать бота.")
        return
    return await handler(event, data)


# === ОБРАБОТКА ОТМЕНЫ СОСТОЯНИЙ (ОБЩАЯ) ===
@dp.callback_query(F.data == "cancel_state")
async def cancel_state_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("❌ Действие отменено.")
    if can_manage_tents(callback.from_user.id):
        await show_admin_panel(callback.message.chat.id, callback.from_user.id)
    else:
        conn = sqlite3.connect("tents.db")
        cursor = conn.cursor()
        cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (callback.from_user.id,))
        user = cursor.fetchone()
        conn.close()
        nick = user[0] if user else "Игрок"
        await show_main_menu(callback.message.chat.id, callback.from_user.id, nick)


# === ЛИЧНЫЕ СООБЩЕНИЯ ОТ АДМИНИСТРАЦИИ ИГРОКУ ===
@dp.callback_query(F.data.startswith("dm_user_"))
async def dm_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not can_manage_tents(callback.from_user.id):
        return

    target_id = int(callback.data.split("_")[2])

    # Проверяем, что у нас вообще есть telegram_id этого игрока в базе — если запись
    # ссылается на игрока, который никогда не запускал бота (или был удалён), писать некуда.
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (target_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await callback.answer("❌ У этого игрока нет сохранённого Telegram ID в базе — написать ему нельзя.", show_alert=True)
        return

    nick = row[0]
    await state.update_data(dm_target_id=target_id, dm_target_nick=nick)
    await state.set_state(DMState.waiting_for_message)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить", callback_data="cancel_state")

    await callback.message.answer(
        f"✉️ Напишите сообщение для игрока <b>{nick}</b> следующим сообщением — оно уйдёт ему в личные "
        f"от имени бота с пометкой «от администрации».",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(DMState.waiting_for_message)
async def dm_user_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_id = data.get("dm_target_id")
    nick = data.get("dm_target_nick", "Игрок")
    await state.clear()

    if not target_id:
        await message.answer("⚠️ Сессия отправки сообщения устарела, начните заново.")
        return

    if not message.text:
        await message.answer("❌ Поддерживается только текст. Сообщение не отправлено, попробуйте снова через «✉️ Написать игроку».")
        return

    delivered = await safe_send(
        target_id,
        f"📩 <b>Сообщение от администрации:</b>\n\n{message.text}"
    )

    if delivered:
        await message.answer(f"✅ Сообщение доставлено игроку {nick}.")
        await send_log(f"✉️ <b>Личное сообщение:</b> {message.from_user.full_name} → {nick} (ID: <code>{target_id}</code>)\nТекст: {message.text}")
    else:
        await message.answer(f"❌ Не удалось доставить сообщение игроку {nick} — возможно, он заблокировал бота.")


# === МЕНЮ ПОЛЬЗОВАТЕЛЯ ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("⌨️ Быстрые кнопки — внизу экрана.", reply_markup=main_reply_kb(message.from_user.id))

    if message.from_user.id == VIEWER_ID:
        await show_stats_menu(message)
        return

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()

    if not user:
        await message.answer("👋 Приветствуем в Торговой Зоне!\n\nПожалуйста, введите ваш игровой никнейм в Minecraft:")
        await state.set_state(RegisterState.waiting_for_nickname)
    else:
        await show_main_menu(message.chat.id, message.from_user.id, user[0])


async def show_main_menu(chat_id: int, user_id: int, nickname: str):
    tent = await get_user_tent(user_id)

    kb = InlineKeyboardBuilder()
    if not tent or not tent[1]:
        kb.button(text="⛺ Арендовать палатку", callback_data="choose_free_tent")
    else:
        kb.button(text="⛺ Моя палатка", callback_data="my_tent")

    kb.button(text="✏️ Изменить ник", callback_data="edit_nick")
    kb.adjust(1)
    await bot.send_message(
        chat_id=chat_id,
        text=f"👋 С возвращением, {nickname}!\nВыберите действие:",
        reply_markup=kb.as_markup()
    )


# === КНОПКИ ПОСТОЯННОЙ КЛАВИАТУРЫ (должны быть зарегистрированы РАНЬШЕ обработчиков
# состояний вроде ввода ника — иначе, например, слово "Отменить" попадёт в тот
# хендлер как обычный текст вместо того, чтобы сработать как отмена) ===
@dp.message(F.text == "🏠 Главное меню")
async def reply_btn_main_menu(message: types.Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == VIEWER_ID:
        await show_stats_menu(message)
        return
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    conn.close()
    if not user:
        await message.answer("Сначала зарегистрируйтесь — отправьте /start")
        return
    await show_main_menu(message.chat.id, message.from_user.id, user[0])


@dp.message(F.text == "🛠 Админ-панель")
async def reply_btn_admin(message: types.Message, state: FSMContext):
    if not can_manage_tents(message.from_user.id):
        return
    await state.clear()
    text, markup = await render_admin_panel(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    """Служебная команда для сброса состояния (например, если админ передумал вводить
    новую дату или текст рассылки). Кнопки-«Отменить» в общем меню больше нет — отмена
    для игрока доступна только на этапе подтверждения загруженного скрина оплаты."""
    cur_state = await state.get_state()
    await state.clear()
    if cur_state is None:
        await message.answer("Нечего отменять — вы не в процессе оформления.")
        return
    await message.answer("❌ Действие отменено.")
    if can_manage_tents(message.from_user.id):
        text, markup = await render_admin_panel(message.from_user.id)
        await message.answer(text, reply_markup=markup)
    else:
        conn = sqlite3.connect("tents.db")
        cursor = conn.cursor()
        cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (message.from_user.id,))
        user = cursor.fetchone()
        conn.close()
        nick = user[0] if user else "Игрок"
        await show_main_menu(message.chat.id, message.from_user.id, nick)


@dp.message(RegisterState.waiting_for_nickname)
async def process_nickname(message: types.Message, state: FSMContext):
    nickname = message.text.strip()
    username = message.from_user.username or "без_юзернейма"

    if is_nickname_taken(nickname):
        await message.answer(
            f"❌ <b>Игровой никнейм «{nickname}» уже зарегистрирован!</b>\n\n"
            f"Пожалуйста, введите ваш СОБСТВЕННЫЙ настоящий никнейм в Minecraft:",
            parse_mode="HTML"
        )
        return

    save_user(message.from_user.id, username, nickname)
    await send_log(f"🆕 <b>Новая регистрация:</b>\nНик: <code>{nickname}</code>\nTG: @{username} (ID: <code>{message.from_user.id}</code>)")

    await notify_admins(f"🆕 Новый игрок зарегистрировался!\n\n👤 Ник: {nickname}\n📱 Telegram: @{username} (ID: {message.from_user.id})")

    await message.answer(f"✅ Ваш никнейм {nickname} успешно зарегистрирован!")
    await state.clear()
    await show_main_menu(message.chat.id, message.from_user.id, nickname)


@dp.callback_query(F.data == "edit_nick")
async def start_edit_nick(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("✏️ Введите ваш новый игровой никнейм:")
    await state.set_state(RegisterState.waiting_for_edit_nickname)


@dp.message(RegisterState.waiting_for_edit_nickname)
async def process_edit_nickname(message: types.Message, state: FSMContext):
    new_nick = message.text.strip()
    username = message.from_user.username or "без_юзернейма"

    if is_nickname_taken(new_nick, exclude_tg_id=message.from_user.id):
        await message.answer(
            f"❌ <b>Игровой никнейм «{new_nick}» уже занят другим игроком!</b>\n\n"
            f"Введите другой никнейм:",
            parse_mode="HTML"
        )
        return

    save_user(message.from_user.id, username, new_nick)
    await firestore_sync.rename_player_in_tent(message.from_user.id, new_nick)
    owned = await get_user_tent(message.from_user.id)
    if owned:
        await sync_tent_row(owned[0])

    await send_log(f"✏️ <b>Смена ника:</b>\nИгрок ID <code>{message.from_user.id}</code> сменил ник на <code>{new_nick}</code>")

    await message.answer(f"✅ Ваш никнейм обновлён на: {new_nick}")
    await state.clear()
    await show_main_menu(message.chat.id, message.from_user.id, new_nick)


# === ⛺ АРЕНДА СВОБОДНОЙ ПАЛАТКИ ИГРОКОМ ===
@dp.callback_query(F.data == "choose_free_tent")
async def show_free_tents_for_user(callback: types.CallbackQuery, state: FSMContext):
    existing_tent = await get_user_tent(callback.from_user.id)
    if existing_tent and existing_tent[1]:
        await callback.answer("⚠️ У вас уже есть активная палатка!", show_alert=True)
        return

    all_tents = await get_all_tents_list()

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tent_id FROM pending_requests")
    pending_ids = {row[0] for row in cursor.fetchall()}
    conn.close()

    # ВАЖНО: исключаем палатки, на которые уже подана заявка и ждёт подтверждения админом —
    # иначе палатка показывается "свободной" даже когда её уже кто-то занимает (гонка заявок).
    free_tents = [t[0] for t in all_tents if not t[1] and t[0] not in pending_ids]

    if not free_tents:
        await callback.answer("❌ На данный момент свободных палаток нет!", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for t_id in free_tents:
        kb.button(text=f"⛺ Палатка #{t_id} (Свободна)", callback_data=f"user_rent_tent_{t_id}")
    
    kb.button(text="🔙 Назад", callback_data="back_to_main_menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "📋 <b>ДОСТУПНЫЕ СВОБОДНЫЕ ПАЛАТКИ</b>\n\nВыберите номер палатки, которую хотите арендовать:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(RentTentState.waiting_for_tent_choice)


@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (callback.from_user.id,))
    user = cursor.fetchone()
    conn.close()
    nick = user[0] if user else "Игрок"
    try:
        await callback.message.delete()
    except Exception:
        pass
    await show_main_menu(callback.message.chat.id, callback.from_user.id, nick)


@dp.callback_query(F.data.startswith("user_rent_tent_"), RentTentState.waiting_for_tent_choice)
async def process_user_selected_tent(callback: types.CallbackQuery, state: FSMContext):
    tent_id = int(callback.data.split("_")[3])
    
    tent = await get_tent(tent_id)
    if not tent or tent[1] is not None:
        await callback.answer("❌ Извините, эту палатку только что заняли. Выберите другую.", show_alert=True)
        return

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM pending_requests WHERE tent_id = ?", (tent_id,))
    already_pending = cursor.fetchone()
    conn.close()
    if already_pending:
        await callback.answer("❌ На эту палатку уже подана заявка и она ожидает подтверждения. Выберите другую.", show_alert=True)
        return

    await state.update_data(tent_id=tent_id)

    kb = InlineKeyboardBuilder()
    for code, t in TARIFFS.items():
        kb.button(text=t["label"], callback_data=f"user_tariff_{code}")
    kb.button(text="🔙 Назад к списку", callback_data="choose_free_tent")
    kb.adjust(1)

    await callback.message.edit_text(
        f"✅ Вы выбрали <b>Палатку #{tent_id}</b>.\n\nТеперь выберите тариф аренды:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(RentTentState.waiting_for_tariff)


@dp.callback_query(F.data.startswith("user_tariff_"), RentTentState.waiting_for_tariff)
async def process_user_selected_tariff(callback: types.CallbackQuery, state: FSMContext):
    tariff_code = callback.data.split("_")[2]
    await state.update_data(tariff_code=tariff_code)
    tariff = TARIFFS[tariff_code]

    await callback.message.edit_text(
        f"Вы выбрали тариф: {tariff['label']}\n\n"
        f"📸 Переведите {tariff['price']} 🛢️ нефти в игре и отправьте скриншот/фото чека прямо сюда в чат."
    )
    await state.set_state(RentTentState.waiting_for_photo)


@dp.message(RentTentState.waiting_for_photo, F.photo | F.document)
async def process_user_rent_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tent_id = data.get("tent_id")
    tariff_code = data.get("tariff_code")

    if not tent_id or not tariff_code:
        await message.answer("❌ Произошла ошибка состояния. Попробуйте начать заново через /start.")
        await state.clear()
        return

    photo_id, is_document = extract_proof_file(message)
    if not photo_id:
        await message.answer(
            "❌ Это не похоже на изображение. Пришлите скриншот/фото чека об оплате "
            "(как фото, либо как файл-изображение)."
        )
        return

    # Скрин не отправляется администрации сразу — сначала игрок должен подтвердить,
    # что прикрепил именно то, что нужно (единственное место, где осталась кнопка «Отмена»).
    await state.update_data(pending_photo_id=photo_id, pending_is_document=is_document)
    await state.set_state(RentTentState.confirming_photo)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="confirm_photo_yes")
    kb.button(text="❌ Отмена", callback_data="confirm_photo_no")
    kb.adjust(2)
    caption = "📸 Подтвердите, что это верный скрин чека об оплате."
    if is_document:
        await message.answer_document(photo_id, caption=caption, reply_markup=kb.as_markup())
    else:
        await message.answer_photo(photo_id, caption=caption, reply_markup=kb.as_markup())


@dp.message(RentTentState.waiting_for_photo)
async def process_user_rent_photo_fallback(message: types.Message, state: FSMContext):
    await message.answer(
        "📸 Ожидаю скриншот/фото чека об оплате (как фото или файл-изображение).\n"
        "Просто пришлите его ещё раз."
    )


@dp.callback_query(F.data == "confirm_photo_yes", RentTentState.confirming_photo)
async def confirm_rent_photo_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tent_id = data.get("tent_id")
    tariff_code = data.get("tariff_code")
    photo_id = data.get("pending_photo_id")
    is_document = bool(data.get("pending_is_document"))
    await state.clear()

    if not tent_id or not tariff_code or not photo_id:
        await callback.answer("❌ Ошибка состояния, попробуйте заново через /start.", show_alert=True)
        return

    tariff = TARIFFS[tariff_code]

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (callback.from_user.id,))
    user_row = cursor.fetchone()
    user_nick = user_row[0] if user_row else "Неизвестно"

    cursor.execute(
        "INSERT INTO pending_requests (tent_id, user_id, tariff_code, photo_id, is_document) VALUES (?, ?, ?, ?, ?)",
        (tent_id, callback.from_user.id, tariff_code, photo_id, int(is_document))
    )
    req_id = cursor.lastrowid
    conn.commit()
    conn.close()

    try:
        await callback.message.edit_caption(caption="⏳ Заявка отправлена Администрации! Ожидайте подтверждения.")
    except Exception:
        await callback.message.answer("⏳ Заявка отправлена Администрации! Ожидайте подтверждения.")
    await callback.answer()

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"appr_{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"rej_{req_id}")
    kb.adjust(2)

    username = callback.from_user.username or "нет"
    caption_text = (
        f"📥 НОВАЯ ЗАЯВКА НА АРЕНДУ ПАЛАТКИ!\n\n"
        f"⛺ Запрошена Палатка №{tent_id}\n"
        f"👤 Игрок: @{username} (Ник: {user_nick})\n"
        f"Тариф: {tariff['label']}"
    )

    try:
        await send_request_card_to_admins(photo_id, caption_text, kb.as_markup(), is_document=is_document)
    except Exception as e:
        logging.error(f"Ошибка отправки карточки заявки админу: {e}")


@dp.callback_query(F.data == "confirm_photo_no", RentTentState.confirming_photo)
async def confirm_rent_photo_no(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_caption(caption="❌ Отменено. Загрузка скрина сброшена — начните заново через /start.")
    except Exception:
        await callback.message.answer("❌ Отменено. Загрузка скрина сброшена — начните заново через /start.")
    await callback.answer("Операция отменена")


@dp.callback_query(F.data == "my_tent")
async def show_my_tent(callback: types.CallbackQuery):
    tent = await get_user_tent(callback.from_user.id)
    if not tent or not tent[1]:
        await callback.message.answer("⏳ За вашим аккаунтом пока не закреплена палатка.\nВыберите в меню 'Арендовать палатку'.")
        return

    tent_id, tg_id, nickname, end_date_str = tent

    try:
        end_date_formatted = datetime.strptime(end_date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        end_date_formatted = "Не определено"

    status = tent_status_label(end_date_str)

    msg = (
        f"⛺ Палатка №{tent_id} ({nickname})\n"
        f"📅 Оплачена до: {end_date_formatted} 23:59:59\n"
        f"Статус: {status}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Продлить аренду", callback_data=f"renew_{tent_id}")
    kb.button(text="❌ Закончить аренду", callback_data=f"user_quit_{tent_id}")
    kb.adjust(1)

    await callback.message.answer(msg, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("user_quit_"))
async def user_quit_tent(callback: types.CallbackQuery):
    tent_id = int(callback.data.split("_")[2])
    tent = await get_tent(tent_id)

    await clear_tent_db(tent_id)
    await sync_tent_row(tent_id)
    await send_log(f"📦 <b>Отказ от палатки:</b>\nИгрок <code>{tent[2]}</code> (ID: <code>{callback.from_user.id}</code>) самостоятельно освободил палатку №{tent_id}.")

    await callback.message.answer(
        f"📦 Вы завершили аренду палатки №{tent_id}.\n\n"
        f"⚠️ Пожалуйста, уберите весь ваш товар с палатки в течение 2 дней!"
    )

    await notify_admins(f"ℹ️ Игрок {callback.from_user.full_name} ({tent[2]}) самостоятельно отказался от палатки №{tent_id}.")


# === ПРОЦЕСС ПРОДЛЕНИЯ И ЧЕК ===
@dp.callback_query(F.data.startswith("renew_"))
async def select_tariff(callback: types.CallbackQuery, state: FSMContext):
    tent_id = int(callback.data.split("_")[1])

    # ВАЖНО: раньше тут не проверялось, что палатка вообще принадлежит нажавшему кнопку —
    # любой пользователь мог отправить callback "renew_<номер>" (например, переслав кнопку
    # от другого игрока или просто угадав номер палатки 1-20) и попасть в процесс продления
    # чужой или вообще любой палатки. Теперь продлевать можно только СВОЮ палатку.
    owned_tent = await get_user_tent(callback.from_user.id)
    if not owned_tent or owned_tent[1] is None or owned_tent[0] != tent_id:
        await callback.answer("❌ Это не ваша палатка! Продлевать можно только свою палатку.", show_alert=True)
        return

    await state.update_data(tent_id=tent_id)

    kb = InlineKeyboardBuilder()
    for code, t in TARIFFS.items():
        kb.button(text=t["label"], callback_data=f"tariff_{code}")
    kb.adjust(1)

    await callback.message.answer("Выберите тариф для продления:", reply_markup=kb.as_markup())
    await state.set_state(RenewState.waiting_for_tariff)


@dp.callback_query(F.data.startswith("tariff_"))
async def process_tariff(callback: types.CallbackQuery, state: FSMContext):
    tariff_code = callback.data.split("_")[1]
    await state.update_data(tariff_code=tariff_code)
    tariff = TARIFFS[tariff_code]

    await callback.message.answer(
        f"Вы выбрали: {tariff['label']}\n\n"
        f"📸 Переведите {tariff['price']} 🛢️ нефти в игре и отправьте скриншот/фото чека прямо сюда в чат."
    )
    await state.set_state(RenewState.waiting_for_photo)


@dp.message(RenewState.waiting_for_photo, F.photo | F.document)
async def process_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tent_id = data.get("tent_id")
    tariff_code = data.get("tariff_code")

    if not tent_id or not tariff_code:
        await message.answer("❌ Ошибка состояния. Попробуйте снова через 'Моя палатка' -> 'Продлить аренду'.")
        await state.clear()
        return

    owned_tent = await get_user_tent(message.from_user.id)
    if not owned_tent or owned_tent[1] is None or owned_tent[0] != tent_id:
        await message.answer("❌ Это не ваша палатка! Продление отменено.")
        await state.clear()
        return

    photo_id, is_document = extract_proof_file(message)
    if not photo_id:
        await message.answer(
            "❌ Это не похоже на изображение. Пришлите скриншот/фото чека об оплате "
            "(как фото, либо как файл-изображение)."
        )
        return

    # Скрин не отправляется администрации сразу — сначала игрок должен подтвердить,
    # что прикрепил именно то, что нужно (единственное место, где осталась кнопка «Отмена»).
    await state.update_data(pending_photo_id=photo_id, pending_is_document=is_document)
    await state.set_state(RenewState.confirming_photo)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="confirm_photo_yes")
    kb.button(text="❌ Отмена", callback_data="confirm_photo_no")
    kb.adjust(2)
    caption = "📸 Подтвердите, что это верный скрин чека об оплате."
    if is_document:
        await message.answer_document(photo_id, caption=caption, reply_markup=kb.as_markup())
    else:
        await message.answer_photo(photo_id, caption=caption, reply_markup=kb.as_markup())


@dp.message(RenewState.waiting_for_photo)
async def process_photo_fallback(message: types.Message, state: FSMContext):
    await message.answer(
        "📸 Ожидаю скриншот/фото чека об оплате (как фото или файл-изображение).\n"
        "Просто пришлите его ещё раз."
    )


@dp.callback_query(F.data == "confirm_photo_yes", RenewState.confirming_photo)
async def confirm_renew_photo_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tent_id = data.get("tent_id")
    tariff_code = data.get("tariff_code")
    photo_id = data.get("pending_photo_id")
    is_document = bool(data.get("pending_is_document"))
    await state.clear()

    if not tent_id or not tariff_code or not photo_id:
        await callback.answer("❌ Ошибка состояния, попробуйте заново через 'Моя палатка'.", show_alert=True)
        return

    owned_tent = await get_user_tent(callback.from_user.id)
    if not owned_tent or owned_tent[1] is None or owned_tent[0] != tent_id:
        await callback.answer("❌ Это не ваша палатка! Продление отменено.", show_alert=True)
        return

    tariff = TARIFFS[tariff_code]

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO pending_requests (tent_id, user_id, tariff_code, photo_id, is_document) VALUES (?, ?, ?, ?, ?)",
        (tent_id, callback.from_user.id, tariff_code, photo_id, int(is_document))
    )
    req_id = cursor.lastrowid
    conn.commit()
    conn.close()

    try:
        await callback.message.edit_caption(caption="⏳ Заявка отправлена Администрации! Ожидайте подтверждения.")
    except Exception:
        await callback.message.answer("⏳ Заявка отправлена Администрации! Ожидайте подтверждения.")
    await callback.answer()

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"appr_{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"rej_{req_id}")
    kb.adjust(2)

    tent = await get_tent(tent_id)
    user_nick = tent[2] if tent and tent[2] else "Неизвестно"
    username = callback.from_user.username or "нет"

    caption_text = (
        f"📥 НОВАЯ ЗАЯВКА НА ПРОДЛЕНИЕ!\n\n"
        f"⛺ Палатка №{tent_id}\n"
        f"👤 Игрок: @{username} (Ник: {user_nick})\n"
        f"Тариф: {tariff['label']}"
    )

    try:
        await send_request_card_to_admins(photo_id, caption_text, kb.as_markup(), is_document=is_document)
    except Exception as e:
        logging.error(f"Ошибка отправки карточки: {e}")


@dp.callback_query(F.data == "confirm_photo_no", RenewState.confirming_photo)
async def confirm_renew_photo_no(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_caption(caption="❌ Отменено. Загрузка скрина сброшена.")
    except Exception:
        await callback.message.answer("❌ Отменено. Загрузка скрина сброшена.")
    await callback.answer("Операция отменена")


@dp.callback_query(F.data.startswith("appr_"))
async def approve_payment(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для подтверждения платежей!", show_alert=True)
        return

    req_id = int(callback.data.split("_")[1])

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tent_id, user_id, tariff_code, photo_id, is_document FROM pending_requests WHERE id = ?", (req_id,))
    req = cursor.fetchone()

    if not req:
        await callback.answer("⚠️ Эта заявка уже обработана!")
        conn.close()
        return

    tent_id, user_id, tariff_code, photo_id, is_document = req
    is_document = bool(is_document)
    tariff = TARIFFS[tariff_code]
    
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (user_id,))
    u_row = cursor.fetchone()
    user_nick = u_row[0] if u_row else "Игрок"

    tent_data = await get_tent(tent_id)
    is_currently_empty = (tent_data[1] is None)

    # Защита от гонки заявок: пока эта заявка ждала подтверждения, палатку мог уже занять
    # кто-то другой (одобрили другую заявку раньше). Раньше в этом случае код по ошибке
    # "продлевал" палатку под именем нового заявителя, хотя фактическим арендатором
    # оставался первый игрок — деньги/дни путались между разными людьми.
    if not is_currently_empty and tent_data[1] != user_id:
        cursor.execute("DELETE FROM pending_requests WHERE id = ?", (req_id,))
        conn.commit()
        conn.close()
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"❌ Палатку №{tent_id} уже успел занять другой игрок, пока ваша заявка ожидала подтверждения. Пожалуйста, выберите другую палатку через /start."
            )
        except Exception:
            pass
        await callback.answer("⚠️ Эта палатка уже занята другим игроком — заявка отклонена автоматически.", show_alert=True)
        try:
            await callback.message.edit_caption(
                caption=callback.message.caption + "\n\n⚠️ АВТООТКЛОНЕНО (палатку уже занял другой игрок)"
            )
        except Exception:
            pass
        return

    if is_currently_empty:
        await assign_tent_db(tent_id, user_id, user_nick, days=tariff["days"], price=tariff["price"],
                              photo_id=photo_id, is_document=is_document)

        updated_tent = await get_tent(tent_id)
        formatted_end_date = datetime.strptime(updated_tent[3], "%Y-%m-%d").strftime('%d.%m.%Y')

        # Палатка только что занята — все ОСТАЛЬНЫЕ ожидающие заявки на неё (от других игроков)
        # больше не актуальны, отменяем их и уведомляем тех игроков, чтобы они не ждали зря.
        cursor.execute("SELECT id, user_id FROM pending_requests WHERE tent_id = ? AND id != ?", (tent_id, req_id))
        stale_requests = cursor.fetchall()
        cursor.execute("DELETE FROM pending_requests WHERE tent_id = ? AND id != ?", (tent_id, req_id))
        conn.commit()
        for stale_id, stale_user_id in stale_requests:
            if stale_user_id != user_id:
                try:
                    await bot.send_message(
                        chat_id=stale_user_id,
                        text=f"❌ Палатку №{tent_id} уже занял другой игрок. Ваша заявка отменена автоматически — выберите другую палатку через /start."
                    )
                except Exception:
                    pass
    else:
        new_end = await extend_tent_db(tent_id, tariff["days"], tariff["price"], photo_id, user_nick, is_document=is_document)
        formatted_end_date = new_end.strftime('%d.%m.%Y')

    cursor.execute("DELETE FROM pending_requests WHERE id = ?", (req_id,))
    conn.commit()
    conn.close()
    await sync_tent_row(tent_id)

    receipt_id = f"R-{datetime.now(MSK_TZ).strftime('%d%m%Y-%H%M%S')}"
    receipt_text = generate_receipt_text(receipt_id, user_nick, tent_id, tariff["days"], tariff["price"], formatted_end_date)

    action_type = "Первичная выдача" if is_currently_empty else "Продление"
    await send_log(
        f"💰 <b>Подтверждена оплата ({action_type}):</b>\n"
        f"Палатка №{tent_id} ({user_nick})\n"
        f"Сумма: {tariff['price']} 🛢️ нефти ({tariff['days']} дн.)\n"
        f"Дата окончания: {formatted_end_date} 23:59:59"
    )

    try:
        await bot.send_message(chat_id=user_id, text=receipt_text, parse_mode="HTML")
    except Exception:
        pass

    try:
        await bot.send_message(chat_id=LOG_CHANNEL_ID, text=receipt_text, parse_mode="HTML")
    except Exception:
        pass

    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n✅ ОДОБРЕНО!\nПалатка №{tent_id} закреплена. Действует до: {formatted_end_date} 23:59:59"
    )


@dp.callback_query(F.data.startswith("rej_"))
async def reject_payment(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для отклонения платежей!", show_alert=True)
        return

    req_id = int(callback.data.split("_")[1])

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tent_id, user_id FROM pending_requests WHERE id = ?", (req_id,))
    req = cursor.fetchone()

    if req:
        tent_id, user_id = req
        cursor.execute("DELETE FROM pending_requests WHERE id = ?", (req_id,))
        conn.commit()

        await send_log(f"❌ <b>Отклонена оплата:</b>\nПалатка №{tent_id}, ID пользователя: <code>{user_id}</code>")

        try:
            await bot.send_message(
                chat_id=int(user_id),
                text=f"❌ Ваша заявка по палатке №{tent_id} отклонена.\nСвяжитесь с Администрацией."
            )
        except Exception:
            pass

    conn.close()
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n❌ ОТКЛОНЕНО!"
    )


# === 📢 МАССОВАЯ РАССЫЛКА ОБЪЯВЛЕНИЙ ===
@dp.callback_query(F.data == "adm_broadcast")
async def start_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ Рассылка доступна только Главному Админу!", show_alert=True)
        return

    await callback.message.answer(
        "📢 <b>РАССЫЛКА ОБЪЯВЛЕНИЯ</b>\n\n"
        "Отправьте текст, фото или сообщение, которое получат ВСЕ зарегистрированные игроки:",
        parse_mode="HTML"
    )
    await state.set_state(BroadcastState.waiting_for_message)


@dp.message(BroadcastState.waiting_for_message)
async def process_broadcast_msg(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return

    await state.update_data(msg_id=message.message_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить рассылку", callback_data="confirm_broadcast")
    kb.button(text="❌ Отмена", callback_data="back_admin")
    kb.adjust(1)

    await message.answer("⚠️ <b>Подтвердите отправку объявления:</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await state.set_state(BroadcastState.waiting_for_confirmation)


@dp.callback_query(F.data == "confirm_broadcast", BroadcastState.waiting_for_confirmation)
async def run_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        return

    data = await state.get_data()
    msg_id = data.get("msg_id")

    users = get_all_users()
    count_success = 0
    count_fail = 0

    await callback.message.edit_text("🚀 Рассылка запущена... Пожалуйста, подождите.")

    for u_id, _, _ in users:
        try:
            await bot.copy_message(chat_id=u_id, from_chat_id=callback.from_user.id, message_id=msg_id)
            count_success += 1
            await asyncio.sleep(0.05)
        except Exception:
            count_fail += 1

    await callback.message.answer(
        f"✅ <b>РАССЫЛКА ЗАВЕРШЕНА!</b>\n\n"
        f"📊 Успешно доставлено: {count_success}\n"
        f"❌ Не доставлено (заблокировали бота): {count_fail}",
        parse_mode="HTML"
    )

    await send_log(f"📢 <b>Выполнена рассылка:</b>\nУспешно: {count_success} | Ошибок: {count_fail}")
    await state.clear()


# === 📦 АВТО-БЭКАП БАЗЫ В ЛОГ-КАНАЛ КАЖДЫЕ 3 ДНЯ ===
async def send_db_backup():
    if os.path.exists("tents.db") and LOG_CHANNEL_ID:
        try:
            db_file = FSInputFile("tents.db")
            await bot.send_document(
                chat_id=LOG_CHANNEL_ID,
                document=db_file,
                caption=(
                    f"📦 <b>АВТОМАТИЧЕСКИЙ БЭКАП БАЗЫ ДАННЫХ</b>\n\n"
                    f"📅 Дата: {datetime.now(MSK_TZ).strftime('%d.%m.%Y %H:%M:%S')} (МСК)\n"
                    f"<i>Резервная копия формируется каждые 3 дня.</i>"
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка бэкапа: {e}")


# === 🔔 АВТОМАТИЧЕСКИЕ НАПОМИНАНИЯ ИГРОКАМ В ЛС (В 12:00 МСК) ===
async def check_tent_expirations():
    tents = await get_all_tents_list()

    now_msk_date = datetime.now(MSK_TZ).date()

    for tent_id, tg_id, nick, end_date_str in tents:
        if not tg_id or not end_date_str:
            continue
        try:
            end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            days_left = (end_dt - now_msk_date).days

            if days_left == 1 and tg_id:
                kb = InlineKeyboardBuilder()
                kb.button(text="💳 Продлить сейчас", callback_data=f"renew_{tent_id}")
                
                await bot.send_message(
                    chat_id=tg_id,
                    text=(
                        f"⚠️ <b>ВНИМАНИЕ! СРОК АРЕНДЫ ИСТЕКАЕТ ЗАВТРА!</b>\n\n"
                        f"Уважаемый <b>{nick}</b>!\n"
                        f"Срок аренды <b>Палатки #{tent_id}</b> заканчивается <b>завтра ({end_dt.strftime('%d.%m.%Y')}) в 23:59:59</b>.\n\n"
                        f"Пожалуйста, свяжитесь с администрацией или продлите аренду в боте!"
                    ),
                    reply_markup=kb.as_markup(),
                    parse_mode="HTML"
                )
                await send_log(f"⏰ <b>Напоминание отправлено:</b> Игроку <code>{nick}</code> (Палатка №{tent_id})")
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления для {nick}: {e}")


# === ✏️ РУЧНОЕ ИЗМЕНЕНИЕ СРОКА АРЕНДЫ (АДМИН) ===
@dp.callback_query(F.data.startswith("edit_date_"))
async def start_edit_date(callback: types.CallbackQuery, state: FSMContext):
    if not can_manage_tents(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для изменения сроков!", show_alert=True)
        return

    tent_id = int(callback.data.split("_")[2])
    tent = await get_tent(tent_id)

    if not tent or not tent[2]:
        await callback.answer("❌ У этой палатки нет арендатора!", show_alert=True)
        return

    await state.update_data(edit_tent_id=tent_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить", callback_data="cancel_state")

    await callback.message.answer(
        f"✏️ <b>Изменение даты окончания аренды для Палатки №{tent_id} ({tent[2]})</b>\n\n"
        f"Текущая дата: <code>{tent[3]}</code>\n\n"
        f"Введите новую дату в формате <b>ГГГГ-ММ-ДД</b> (например: <code>2026-08-25</code>)\n"
        f"или <b>ДД.ММ.ГГГГ</b> (например: <code>25.08.2026</code>):",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await state.set_state(EditDateState.waiting_for_new_date)


@dp.message(EditDateState.waiting_for_new_date)
async def process_new_date(message: types.Message, state: FSMContext):
    if not can_manage_tents(message.from_user.id):
        return

    data = await state.get_data()
    tent_id = data.get("edit_tent_id")
    raw_date = message.text.strip()

    parsed_date = None
    try:
        parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass

    if not parsed_date:
        try:
            parsed_date = datetime.strptime(raw_date, "%d.%m.%Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    if not parsed_date:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отменить", callback_data="cancel_state")
        await message.answer(
            "❌ <b>Неверный формат даты!</b>\n"
            "Используйте формат <code>ГГГГ-ММ-ДД</code> (например <code>2026-08-25</code>) "
            "или <code>ДД.ММ.ГГГГ</code> (например <code>25.08.2026</code>). Попробуйте ещё раз:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )
        return

    tent = await get_tent(tent_id)
    await update_tent_date_db(tent_id, parsed_date)
    await sync_tent_row(tent_id)

    await send_log(
        f"✏️ <b>Администратор вручную изменил дату аренды:</b>\n"
        f"Палатка №{tent_id} ({tent[2]})\n"
        f"Старая дата: <code>{tent[3]}</code>\n"
        f"Новая дата: <code>{parsed_date} 23:59:59</code>"
    )

    await message.answer(
        f"✅ Дата окончания аренды палатки №{tent_id} успешно обновлена на: <b>{parsed_date} 23:59:59</b>",
        parse_mode="HTML"
    )

    if tent[1]:
        try:
            formatted_user_date = datetime.strptime(parsed_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            await bot.send_message(
                chat_id=tent[1],
                text=f"ℹ️ Администратор обновил срок аренды вашей Палатки №{tent_id}.\n📅 Новая дата окончания: {formatted_user_date} в 23:59:59"
            )
        except Exception:
            pass

    await state.clear()


# === 📊 РУЧНОЕ РЕДАКТИРОВАНИЕ СТАТИСТИКИ (ОФСЕТЫ) ===
@dp.callback_query(F.data == "edit_stats_menu")
async def edit_stats_menu(callback: types.CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ Редактирование статистики доступно только Главным Админам!", show_alert=True)
        return

    oil_off, deals_off = get_stats_offsets()

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить нефть (офсет)", callback_data="set_oil_offset")
    kb.button(text="✏️ Изменить продления (офсет)", callback_data="set_deals_offset")
    kb.button(text="🔙 В меню статистики", callback_data="adm_stats_menu")
    kb.adjust(1)

    text = (
        f"⚙️ <b>РУЧНАЯ КОРРЕКТИРОВКА СТАТИСТИКИ</b>\n\n"
        f"Текущая поправка нефти: <b>{oil_off} 🛢️</b>\n"
        f"Текущая поправка продлений: <b>{deals_off} шт.</b>\n\n"
        f"<i>Пример: Если хотите убавить 375 нефти, введите <code>-375</code>. "
        f"Чтобы добавить, введите число (например <code>100</code>).</i>"
    )

    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "set_oil_offset")
async def set_oil_offset_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        return
    await callback.message.answer("✏️ Введите число-поправку для собранной НЕФТИ (например <code>-375</code> или <code>0</code>):", parse_mode="HTML")
    await state.set_state(EditStatsState.waiting_for_oil)


@dp.message(EditStatsState.waiting_for_oil)
async def process_oil_offset(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    try:
        val = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число (положительное или отрицательное):")
        return

    oil_off, deals_off = get_stats_offsets()
    update_stats_offsets(val, deals_off)

    await send_log(f"🛠️ <b>Изменён офсет нефти:</b>\nСтарый: <code>{oil_off}</code> | Новый: <code>{val}</code>")
    await message.answer(f"✅ Поправка нефти установлена: <b>{val}</b>", parse_mode="HTML")
    await state.clear()


@dp.callback_query(F.data == "set_deals_offset")
async def set_deals_offset_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        return
    await callback.message.answer("✏️ Введите число-поправку для количества ПРОДЛЕНИЙ (например <code>-3</code> или <code>0</code>):", parse_mode="HTML")
    await state.set_state(EditStatsState.waiting_for_deals)


@dp.message(EditStatsState.waiting_for_deals)
async def process_deals_offset(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    try:
        val = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите целое число (положительное или отрицательное):")
        return

    oil_off, deals_off = get_stats_offsets()
    update_stats_offsets(oil_off, val)

    await send_log(f"🛠️ <b>Изменён офсет продлений:</b>\nСтарый: <code>{deals_off}</code> | Новый: <code>{val}</code>")
    await message.answer(f"✅ Поправка продлений установлена: <b>{val}</b>", parse_mode="HTML")
    await state.clear()


# === 🧹 ПОЛНАЯ ОЧИСТКА БАЗЫ ДАННЫХ ===
@dp.message(Command("clear_database"))
async def clear_database_cmd(message: types.Message):
    if not is_super_admin(message.from_user.id):
        await message.answer("❌ У вас нет прав на полную очистку базы данных!")
        return

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM payments_history")
    try:
        cursor.execute("DELETE FROM sqlite_sequence WHERE name='payments_history'")
    except Exception:
        pass
    update_stats_offsets(0, 0)
    conn.commit()
    conn.close()

    await send_log("🧹 <b>Супер-Админ выполнил полную очистку базы платежей.</b>")
    await message.answer("🧹 <b>База данных успешно очищена!</b> Все записи, платежи и поправки удалены.")


# === 📥 ЭКСПОРТ ФИНАНСОВОГО ОТЧЁТА В EXCEL ===
@dp.callback_query(F.data == "export_excel")
async def select_excel_period(callback: types.CallbackQuery):
    if not can_export_reports(callback.from_user.id):
        await callback.answer("❌ У вас нет доступа к выгрузке отчётов Налоговой!", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Отчёт за 7 дней (1 неделя)", callback_data="ex_period_7")
    kb.button(text="📄 Отчёт за 14 дней (2 недели)", callback_data="ex_period_14")
    kb.button(text="📄 Отчёт за 30 дней (1 месяц)", callback_data="ex_period_30")
    kb.button(text="📄 Отчёт за всё время", callback_data="ex_period_all")
    kb.button(text="🔙 Назад", callback_data="adm_stats_menu")
    kb.adjust(1)

    await callback.message.edit_text(
        "📊 <b>ФИНАНСОВЫЙ ОТЧЁТ ДЛЯ НАЛОГОВОЙ / ПРЕЗИДЕНТА</b>\n\n"
        "Выберите период, за который необходимо сформировать выгрузку в Excel:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("ex_period_"))
async def process_excel_export(callback: types.CallbackQuery):
    if not can_export_reports(callback.from_user.id):
        return

    period_code = callback.data.split("_")[2]
    await callback.answer("📊 Формируем финансовую ведомость...")

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tent_id, nickname, price, days, pay_date FROM payments_history ORDER BY id ASC")
    all_payments = cursor.fetchall()
    conn.close()

    now_msk = datetime.now(MSK_TZ).replace(tzinfo=None)

    filtered_payments = []
    if period_code == "all":
        period_title = "За всё время"
        filtered_payments = all_payments
    else:
        days = int(period_code)
        period_title = f"За {days} дней ({days // 7} нед.)"
        limit_date = now_msk - timedelta(days=days)

        for p in all_payments:
            p_date_str = p[4]
            try:
                dt = datetime.strptime(p_date_str, "%d.%m.%Y %H:%M")
                if dt >= limit_date:
                    filtered_payments.append(p)
            except Exception:
                pass

    aggregated = {}
    for tent_id, nick, price, days, pay_date in filtered_payments:
        key = (nick, tent_id)
        if key not in aggregated:
            aggregated[key] = {
                "nickname": nick,
                "tent_id": tent_id,
                "total_days": days,
                "total_oil": price,
                "last_date": pay_date,
                "deals_count": 1
            }
        else:
            aggregated[key]["total_days"] += days
            aggregated[key]["total_oil"] += price
            aggregated[key]["last_date"] = pay_date
            aggregated[key]["deals_count"] += 1

    total_oil_sum = sum(item["total_oil"] for item in aggregated.values())
    total_deals_sum = sum(item["deals_count"] for item in aggregated.values())
    total_players_count = len(aggregated)

    rows_data = []
    for idx, item in enumerate(aggregated.values(), 1):
        rows_data.append([
            idx,
            item["nickname"],
            f"Палатка #{item['tent_id']}",
            item["total_days"],
            item["total_oil"],
            item["last_date"]
        ])

    file_name = f"Nalogovay_Otchet_{datetime.now(MSK_TZ).strftime('%d_%m_%Y')}.xlsx"

    with pd.ExcelWriter(file_name, engine='openpyxl') as writer:
        columns = ["№ п/п", "Игровой Ник", "Палатка №", "Срок аренды (дней)", "Оплачено нефти (🛢️)", "Дата посл. операции"]
        df = pd.DataFrame(rows_data, columns=columns)
        df.to_excel(writer, sheet_name='Финансовый отчёт', index=False, startrow=7)

        workbook = writer.book
        worksheet = writer.sheets['Финансовый отчёт']

        font_header_title = Font(name='Arial', size=14, bold=True, color='1B365D')
        font_sub = Font(name='Arial', size=10, italic=True, color='555555')
        font_stat_label = Font(name='Arial', size=11, bold=True)
        font_stat_val = Font(name='Arial', size=11, bold=True, color='006100')
        
        font_th = Font(name='Arial', size=11, bold=True, color='FFFFFF')
        fill_th = PatternFill(start_color='1F4E78', end_color='1F4E78', fill_type='solid')

        font_total = Font(name='Arial', size=11, bold=True)
        fill_total = PatternFill(start_color='D9E1F2', end_color='D9E1F2', fill_type='solid')

        border_thin = Border(
            left=Side(style='thin', color='D9D9D9'),
            right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'),
            bottom=Side(style='thin', color='D9D9D9')
        )
        border_total = Border(
            top=Side(style='thin', color='000000'),
            bottom=Side(style='double', color='000000')
        )

        align_center = Alignment(horizontal='center', vertical='center')
        align_right = Alignment(horizontal='right', vertical='center')
        align_left = Alignment(horizontal='left', vertical='center')

        worksheet['A1'] = "СВОДНАЯ ФИНАНСОВАЯ ВЕДОМОСТЬ"
        worksheet['A1'].font = font_header_title
        
        worksheet['A2'] = f"Период отчёта: {period_title} | Дата формирования: {datetime.now(MSK_TZ).strftime('%d.%m.%Y %H:%M')} (МСК)"
        worksheet['A2'].font = font_sub

        worksheet['A4'] = "ИТОГО СОБРАНО НЕФТИ (НАЛОГОВ):"
        worksheet['A4'].font = font_stat_label
        worksheet['D4'] = f"{total_oil_sum} 🛢️"
        worksheet['D4'].font = font_stat_val

        worksheet['A5'] = "ВСЕГО АРЕНДАТОРОВ / ТРАНЗАКЦИЙ:"
        worksheet['A5'].font = font_stat_label
        worksheet['D5'] = f"{total_players_count} чел. / {total_deals_sum} сдел."
        worksheet['D5'].font = font_stat_label

        for col_num in range(1, 7):
            cell = worksheet.cell(row=8, column=col_num)
            cell.font = font_th
            cell.fill = fill_th
            cell.alignment = align_center

        data_start_row = 9
        for row_idx in range(data_start_row, data_start_row + len(rows_data)):
            worksheet.cell(row=row_idx, column=1).alignment = align_center
            worksheet.cell(row=row_idx, column=2).alignment = align_left
            worksheet.cell(row=row_idx, column=3).alignment = align_center
            worksheet.cell(row=row_idx, column=4).alignment = align_right
            worksheet.cell(row=row_idx, column=5).alignment = align_right
            worksheet.cell(row=row_idx, column=6).alignment = align_center

            for col_num in range(1, 7):
                worksheet.cell(row=row_idx, column=col_num).border = border_thin

        total_row = data_start_row + len(rows_data)
        worksheet.cell(row=total_row, column=1, value="").fill = fill_total
        worksheet.cell(row=total_row, column=2, value="ИТОГО ПО ВЕДОМОСТИ:").font = font_total
        worksheet.cell(row=total_row, column=2).fill = fill_total
        worksheet.cell(row=total_row, column=3, value="").fill = fill_total
        
        total_days_sum = sum(item["total_days"] for item in aggregated.values())
        worksheet.cell(row=total_row, column=4, value=total_days_sum).font = font_total
        worksheet.cell(row=total_row, column=4).alignment = align_right
        worksheet.cell(row=total_row, column=4).fill = fill_total
        
        worksheet.cell(row=total_row, column=5, value=total_oil_sum).font = font_total
        worksheet.cell(row=total_row, column=5).alignment = align_right
        worksheet.cell(row=total_row, column=5).fill = fill_total
        
        worksheet.cell(row=total_row, column=6, value="").fill = fill_total

        for col_num in range(1, 7):
            worksheet.cell(row=total_row, column=col_num).border = border_total

        for col in worksheet.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = col[0].column_letter
            worksheet.column_dimensions[col_letter].width = max(max_len + 4, 15)

    try:
        excel_file = FSInputFile(file_name)
        await callback.message.answer_document(
            document=excel_file,
            caption=(
                f"🏛️ <b>СВОДНЫЙ ФИНАНСОВЫЙ ОТЧЁТ</b>\n\n"
                f"📌 <b>Период:</b> {period_title}\n"
                f"🛢️ <b>Всего собрано:</b> {total_oil_sum} 🛢️\n"
                f"👥 <b>Активных арендаторов:</b> {total_players_count}\n"
                f"🧾 <b>Всего операций:</b> {total_deals_sum}\n\n"
                f"<i>Дублирующие операции объединены. Отчёт готов для Налоговой.</i>"
            ),
            parse_mode="HTML"
        )
        await send_log(f"🏛️ <b>Сформирован сводный отчёт для Налоговой</b> за период: {period_title}")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка при отправке Excel: {e}")
    finally:
        if os.path.exists(file_name):
            os.remove(file_name)


# === АДМИН-ПАНЕЛЬ И СТАТИСТИКА ===
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if can_export_reports(message.from_user.id) or message.from_user.id == VIEWER_ID:
        await show_stats_menu(message)


async def render_admin_panel(user_id: int):
    """Строит текст и inline-клавиатуру админ-панели с учётом прав конкретного user_id."""
    tents = await get_all_tents_list()

    kb = InlineKeyboardBuilder()
    for t_id, tg_id, nick, end in tents:
        btn_text = f"#{t_id} 🟢 {nick}" if nick else f"#{t_id} ⚪ Свободна"
        kb.button(text=btn_text, callback_data=f"adm_tent_{t_id}")

    kb.adjust(2)
    if is_super_admin(user_id):
        kb.row(
            types.InlineKeyboardButton(text="📢 Рассылка игрокам", callback_data="adm_broadcast"),
            types.InlineKeyboardButton(text="📊 Статистика ТЗ", callback_data="adm_stats_menu")
        )
        kb.row(
            types.InlineKeyboardButton(text="👥 Список игроков", callback_data="adm_users_list"),
            types.InlineKeyboardButton(text="🚫 Чёрный список", callback_data="adm_blacklist")
        )
    else:
        kb.row(types.InlineKeyboardButton(text="📊 Статистика ТЗ", callback_data="adm_stats_menu"))

    return "🛠️ УПРАВЛЕНИЕ ПАЛАТКАМИ И СИСТЕМОЙ:", kb.as_markup()


async def show_admin_panel(chat_id: int, user_id: int):
    """Отправляет админ-панель по chat_id, права проверяются по РЕАЛЬНОМУ user_id нажавшего
    кнопку — используйте эту функцию вместо cmd_admin(callback.message) внутри callback-хендлеров."""
    if not can_manage_tents(user_id):
        return
    text, markup = await render_admin_panel(user_id)
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not can_manage_tents(message.from_user.id):
        return
    text, markup = await render_admin_panel(message.from_user.id)
    await message.answer(text, reply_markup=markup)


# --- ЧЁРНЫЙ СПИСОК ---
@dp.callback_query(F.data == "adm_blacklist")
async def adm_blacklist_menu(callback: types.CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT b.tg_id, u.nickname, u.username FROM blacklist b LEFT JOIN users u ON b.tg_id = u.tg_id")
    banned = cursor.fetchall()
    conn.close()

    msg = "🚫 ЧЁРНЫЙ СПИСОК ИГРОКОВ:\n\n"
    kb = InlineKeyboardBuilder()

    if not banned:
        msg += "В чёрном списке пока никого нет."
    else:
        for tg_id, nick, uname in banned:
            name = nick or (f"@{uname}" if uname else f"ID: {tg_id}")
            msg += f"• {name} (ID: {tg_id})\n"
            kb.button(text=f"🟢 Разбанить {name}", callback_data=f"unban_{tg_id}")

    kb.button(text="🔙 Назад в меню", callback_data="back_admin")
    kb.adjust(1)

    await callback.message.edit_text(msg, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("ban_user_"))
async def process_ban_callback(callback: types.CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return

    user_id = int(callback.data.split("_")[2])
    ban_user(user_id)

    await send_log(f"🚫 <b>Заблокирован игрок:</b> ID <code>{user_id}</code>")
    await callback.answer("🚫 Игрок заблокирован!", show_alert=True)
    await adm_users_list(callback)


@dp.callback_query(F.data.startswith("unban_"))
async def process_unban_callback(callback: types.CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return

    user_id = int(callback.data.split("_")[1])
    unban_user(user_id)

    await send_log(f"🟢 <b>Разблокирован игрок:</b> ID <code>{user_id}</code>")
    await callback.answer("🟢 Игрок разблокирован!", show_alert=True)
    await adm_blacklist_menu(callback)


# --- СТАТИСТИКА И ПОДМЕНЮ ---
async def show_stats_menu(target):
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 За 7 дней (неделя)", callback_data="st_7")
    kb.button(text="📅 За 14 дней (2 недели)", callback_data="st_14")
    kb.button(text="📅 За 30 дней (месяц)", callback_data="st_30")
    kb.button(text="♾️ За всё время", callback_data="st_all")

    user_id = target.from_user.id if hasattr(target, 'from_user') else None
    if is_super_admin(user_id):
        kb.button(text="✏️ Корректировать числа вручную", callback_data="edit_stats_menu")
    if can_export_reports(user_id):
        kb.button(text="📥 Выгрузить отчёт в Excel", callback_data="export_excel")

    if can_manage_tents(user_id):
        kb.button(text="🔙 Назад в админку", callback_data="back_admin")

    kb.adjust(1)

    text = "📊 <b>ВЫБЕРИТЕ ПЕРИОД ДЛЯ ОТЧЁТА И СТАТИСТИКИ:</b>"
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "adm_stats_menu")
async def adm_stats_menu_cb(callback: types.CallbackQuery):
    await show_stats_menu(callback)


@dp.callback_query(F.data.startswith("st_"))
async def process_stats_period(callback: types.CallbackQuery):
    period_code = callback.data.split("_")[1]

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM users")
    users_count = cursor.fetchone()[0]

    cursor.execute("SELECT price, pay_date FROM payments_history")
    all_payments = cursor.fetchall()
    conn.close()

    all_tents = await get_all_tents_list()
    occupied_tents = [t for t in all_tents if t[1] is not None]
    occupied_count = len(occupied_tents)

    total_earned = 0
    total_deals = 0

    now_msk = datetime.now(MSK_TZ).replace(tzinfo=None)

    if period_code == "all":
        period_title = "За всё время"
        for price, p_date in all_payments:
            total_earned += price
            total_deals += 1
    else:
        days = int(period_code)
        period_title = f"За {days} дней"
        limit_date = now_msk - timedelta(days=days)

        for price, p_date in all_payments:
            try:
                dt = datetime.strptime(p_date, "%d.%m.%Y %H:%M")
                if dt >= limit_date:
                    total_earned += price
                    total_deals += 1
            except Exception:
                pass

    oil_off, deals_off = get_stats_offsets()
    total_earned = max(0, total_earned + oil_off)
    total_deals = max(0, total_deals + deals_off)

    expiring_list = ""
    for t_id, tg_id, nick, end_str in occupied_tents:
        try:
            end_date = datetime.strptime(end_str, "%Y-%m-%d")
            days_left = (end_date - now_msk).days + 1
            if days_left <= 2:
                expiring_list += f"\n• Палатка #{t_id} ({nick}): осталось {days_left} дн."
        except Exception:
            pass

    msg = (
        f"📊 <b>ОТЧЁТ СТАТИСТИКИ ({period_title.upper()}):</b>\n\n"
        f"🛢️ Собрано нефти: <b>{total_earned} 🛢️</b>\n"
        f"🧾 Сделок/Продлений: <b>{total_deals}</b>\n"
        f"⛺ Занято палаток: <b>{occupied_count} из 20</b> (Свободно: {20 - occupied_count})\n"
        f"👥 Всего зарегистрировано игроков: <b>{users_count}</b>\n"
    )

    if expiring_list:
        msg += f"\n⚠️ <b>Заканчивается срок аренды:</b>{expiring_list}"
    else:
        msg += "\n🟢 Срочных просрочек нет."

    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 К выбору периода", callback_data="adm_stats_menu")
    if can_manage_tents(callback.from_user.id):
        kb.button(text="🔙 В главное меню", callback_data="back_admin")
    kb.adjust(1)

    await callback.message.edit_text(msg, reply_markup=kb.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("history_menu_"))
async def history_period_menu(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        return

    tent_id = int(callback.data.split("_")[2])

    kb = InlineKeyboardBuilder()
    kb.button(text="📅 За последний месяц (30д)", callback_data=f"show_hist_{tent_id}_month")
    kb.button(text="♾️ За всё время", callback_data=f"show_hist_{tent_id}_all")
    kb.button(text="🔙 Назад к палатке", callback_data=f"adm_tent_{tent_id}")
    kb.adjust(1)

    await callback.message.edit_text(
        f"📜 История платежей палатки №{tent_id}\nВыберите период просмотра:",
        reply_markup=kb.as_markup()
    )


@dp.callback_query(F.data.startswith("show_hist_"))
async def show_history_photos(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        return

    parts = callback.data.split("_")
    tent_id = int(parts[2])
    period = parts[3]

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()

    cursor.execute(
        "SELECT nickname, price, days, photo_id, pay_date, is_document FROM payments_history WHERE tent_id = ? ORDER BY id DESC",
        (tent_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    filtered_rows = []
    now_msk = datetime.now(MSK_TZ).replace(tzinfo=None)

    if period == "month":
        limit_date = now_msk - timedelta(days=30)
        for row in rows:
            pay_date_str = row[4]
            try:
                dt = datetime.strptime(pay_date_str, "%d.%m.%Y %H:%M")
                if dt >= limit_date:
                    filtered_rows.append(row)
            except Exception:
                filtered_rows.append(row)
    else:
        filtered_rows = rows

    if not filtered_rows:
        await callback.answer("❌ За выбранный период подтверждённых чеков не найдено.", show_alert=True)
        return

    await callback.answer("📤 Отправка чеков...")

    for nick, price, days, photo_id, pay_date, is_document in filtered_rows:
        caption = (
            f"🧾 Оплата Палатки №{tent_id}\n"
            f"👤 Игрок: {nick}\n"
            f"💰 Сумма: {price} 🛢️ нефти ({days} дн.)\n"
            f"📅 Дата оплаты: {pay_date}"
        )
        try:
            if photo_id:
                await send_proof(callback.from_user.id, photo_id, is_document=bool(is_document), caption=caption)
            else:
                await bot.send_message(chat_id=callback.from_user.id, text=f"{caption}\n(Фото отсутствует)")
            await asyncio.sleep(0.3)
        except Exception as e:
            logging.error(f"Ошибка вывода чека: {e}")


@dp.callback_query(F.data.startswith("adm_tent_"))
async def adm_tent_manage(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        return

    tent_id = int(callback.data.split("_")[2])
    tent = await get_tent(tent_id)
    _, tg_id, nickname, end_date = tent

    kb = InlineKeyboardBuilder()

    if not nickname:
        text = f"⛺ Палатка №{tent_id}\nСтатус: ⚪ Свободна"

        all_tents = await get_all_tents_list()
        occupied_tg_ids = {t[1] for t in all_tents if t[1] is not None}

        conn = sqlite3.connect("tents.db")
        cursor = conn.cursor()
        cursor.execute("SELECT tg_id, nickname FROM users")
        all_users = cursor.fetchall()
        conn.close()
        unassigned = [(u_id, u_nick) for u_id, u_nick in all_users if u_id not in occupied_tg_ids]

        if unassigned:
            text += "\n\nИгроки без палатки:"
            for u_id, u_nick in unassigned:
                kb.button(text=f"➕ Выдать {u_nick}", callback_data=f"give_{tent_id}_{u_id}")
    else:
        formatted_end = end_date or "Не установлена"
        if end_date:
            try:
                formatted_end = datetime.strptime(end_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            except Exception:
                pass

        text = (
            f"⛺ Палатка №{tent_id}\n"
            f"👤 Игрок: {nickname}\n"
            f"📅 Оплачена до: {formatted_end} 23:59:59"
        )

        kb.button(text="✏️ Изменить дату аренды", callback_data=f"edit_date_{tent_id}")
        kb.button(text="📜 История платежей (Чеки)", callback_data=f"history_menu_{tent_id}")
        kb.button(text="✉️ Написать игроку", callback_data=f"dm_user_{tg_id}")
        kb.button(text="❌ Завершить аренду (Освободить)", callback_data=f"clear_{tent_id}_{tg_id}")

    kb.button(text="🔙 Назад в меню", callback_data="back_admin")
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("give_"))
async def process_give(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        return

    _, tent_id, user_id = callback.data.split("_")
    tent_id, user_id = int(tent_id), int(user_id)

    # Ник берём СВЕЖИЙ из базы по user_id, а не из текста кнопки — раньше ник встраивался
    # прямо в callback_data и если в нём была "_" (обычное дело для Minecraft-ников,
    # например "Cool_Guy"), разбор строки падал с ошибкой и выдача палатки просто не
    # срабатывала без какого-либо вменяемого сообщения администратору.
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        await callback.answer("❌ Игрок не найден в базе (возможно, был удалён). Обновите список.", show_alert=True)
        return
    nickname = row[0]

    await assign_tent_db(tent_id, user_id, nickname)
    await sync_tent_row(tent_id)

    tent = await get_tent(tent_id)
    end_date_str = tent[3] if tent else None
    try:
        formatted_end = datetime.strptime(end_date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        formatted_end = end_date_str or "не установлена"

    # Уведомляем самого игрока о том, что ему выдали палатку — раньше он узнавал об этом
    # только зайдя в бота случайно, никакого сообщения не приходило.
    delivered = await safe_send(
        user_id,
        f"🎉 <b>Вам выдана палатка!</b>\n\n"
        f"⛺ Палатка №{tent_id}\n"
        f"📅 Оплачена до: {formatted_end} 23:59:59\n\n"
        f"Откройте /start → «⛺ Моя палатка», чтобы посмотреть детали или продлить аренду."
    )
    if not delivered:
        logging.error(f"❌ Не удалось уведомить игрока {user_id} о выдаче палатки {tent_id} (возможно, заблокировал бота)")

    await send_log(f"⛺ <b>Выдана палатка:</b>\nПалатка №{tent_id} выдана игроку <code>{nickname}</code> (ID: <code>{user_id}</code>)")
    await callback.answer(f"Палатка №{tent_id} выдана {nickname}!" + ("" if delivered else " (⚠️ уведомить не удалось)"))
    await show_admin_panel(callback.message.chat.id, callback.from_user.id)


@dp.callback_query(F.data.startswith("clear_"))
async def process_clear(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        return

    _, tent_id, user_id = callback.data.split("_")
    tent_id = int(tent_id)
    tent = await get_tent(tent_id)
    await clear_tent_db(tent_id)
    await sync_tent_row(tent_id)

    if tent and tent[1]:
        try:
            await bot.send_message(
                chat_id=int(user_id),
                text=f"📦 Администратор завершил вашу аренду Палатки №{tent_id}.\n\n⚠️ Пожалуйста, уберите весь ваш товар с палатки в течение 2 дней!"
            )
        except Exception as e:
            logging.error(f"❌ Не удалось уведомить игрока {user_id} об освобождении палатки: {e}")

    await send_log(f"🧹 <b>Освобождена палатка:</b>\nАдминистратор освободил палатку №{tent_id} (была у <code>{tent[2]}</code>)")
    await callback.answer(f"Палатка №{tent_id} освобождена!")
    await show_admin_panel(callback.message.chat.id, callback.from_user.id)


@dp.callback_query(F.data == "adm_users_list")
async def adm_users_list(callback: types.CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return

    users = get_all_users()

    if not users:
        await callback.message.edit_text(
            "👥 Зарегистрированных игроков пока нет.",
            reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="back_admin").as_markup()
        )
        return

    msg = "👥 <b>СПИСОК ЗАРЕГИСТРИРУЕМЫХ ИГРОКОВ:</b>\n\n"
    kb = InlineKeyboardBuilder()

    for u_id, u_name, nick in users:
        msg += f"• <b>{nick}</b> (@{u_name})\n"
        kb.button(text=f"✉️ {nick}", callback_data=f"dm_user_{u_id}")
        kb.button(text=f"🚫 Забанить {nick}", callback_data=f"ban_user_{u_id}")
        kb.button(text=f"🗑️ Удалить {nick}", callback_data=f"del_user_{u_id}")

    kb.button(text="📊 Отчёт статистики", callback_data="adm_stats_menu")
    kb.button(text="🔙 Назад в меню", callback_data="back_admin")
    kb.adjust(2)

    await callback.message.edit_text(msg, reply_markup=kb.as_markup(), parse_mode="HTML")


# === УДАЛЕНИЕ ИГРОКА С УКАЗАНИЕМ ПРИЧИНЫ ===
@dp.callback_query(F.data.startswith("del_user_"))
async def process_del_user_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        return

    user_id = int(callback.data.split("_")[2])
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    nick = row[0] if row else "Игрок"

    await state.update_data(del_user_id=user_id, del_user_nick=nick)
    await state.set_state(DeleteUserState.waiting_for_reason)

    kb = InlineKeyboardBuilder()
    kb.button(text="🗑️ Удалить без указания причины", callback_data="del_user_no_reason")
    kb.button(text="❌ Отменить", callback_data="cancel_state")
    kb.adjust(1)

    await callback.message.answer(
        f"✏️ Укажите причину удаления игрока <b>{nick}</b> следующим сообщением — "
        f"она будет автоматически отправлена ему в личные сообщения.\n\n"
        f"Либо удалите без объяснения причины:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()


async def finalize_delete_user(chat_id: int, user_id: int, nick: str, reason: str | None):
    await delete_user_db(user_id)
    reason_txt = reason.strip() if reason and reason.strip() else "не указана"

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🚫 <b>Вы были удалены из базы игроков Торговой Зоны.</b>\n\n"
                f"Причина: {reason_txt}\n\n"
                "Если считаете это ошибкой — свяжитесь с администрацией."
            ),
            parse_mode="HTML"
        )
        delivered = True
    except Exception as e:
        logging.error(f"❌ Не удалось уведомить удалённого игрока {user_id}: {e}")
        delivered = False

    await send_log(f"🗑️ <b>Удаление аккаунта:</b> {nick} (ID: <code>{user_id}</code>)\nПричина: {reason_txt}")
    note = "" if delivered else "\n⚠️ Не удалось доставить уведомление игроку (возможно, он заблокировал бота)."
    await bot.send_message(chat_id=chat_id, text=f"❌ Игрок {nick} удалён из базы.\nПричина: {reason_txt}{note}")


@dp.callback_query(F.data == "del_user_no_reason")
async def process_del_user_no_reason(callback: types.CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        return
    data = await state.get_data()
    await state.clear()
    user_id = data.get("del_user_id")
    nick = data.get("del_user_nick", "Игрок")
    if not user_id:
        await callback.answer("⚠️ Сессия удаления устарела, начните заново.", show_alert=True)
        return
    await callback.answer()
    await finalize_delete_user(callback.message.chat.id, user_id, nick, None)


@dp.message(DeleteUserState.waiting_for_reason)
async def process_del_user_reason(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("del_user_id")
    nick = data.get("del_user_nick", "Игрок")
    await state.clear()
    if not user_id:
        await message.answer("⚠️ Сессия удаления устарела, начните заново через список игроков.")
        return
    await finalize_delete_user(message.chat.id, user_id, nick, message.text)


@dp.callback_query(F.data == "back_admin")
async def back_admin(callback: types.CallbackQuery, state: FSMContext):
    if not can_manage_tents(callback.from_user.id):
        return
    await state.clear()
    await show_admin_panel(callback.message.chat.id, callback.from_user.id)


# === 🚀 ЗАПУСК БОТА И ПЛАНИРОВЩИКА ===
async def main():
    init_db()

    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(send_db_backup, 'interval', days=3)
    scheduler.add_job(check_tent_expirations, 'cron', hour=12, minute=0)
    scheduler.start()

    # Одноразовая безопасная миграция: досоздаёт в Firestore только те палатки, которых
    # там ещё вообще нет. Если документ для номера палатки уже существует — он считается
    # авторитетным (например, уже введён через веб-приложение) и НЕ перезаписывается.
    if firestore_sync.is_configured():
        try:
            report = await firestore_sync.migrate_sqlite_if_missing()
            if report.get("no_firestore"):
                logging.error("❌ Firestore недоступен при старте — проверьте ключ FIREBASE_SERVICE_ACCOUNT_FILE и доступ в интернет.")
            elif report.get("migrated"):
                await send_log(
                    f"🔄 <b>Миграция в Firestore при старте:</b>\n"
                    f"Досозданы палатки: {report['migrated']}\n"
                    f"Уже существовали (не тронуты): {report['skipped_existing'] or '—'}"
                )
        except Exception as e:
            logging.error(f"❌ Ошибка миграции SQLite → Firestore при старте: {e}")
    else:
        logging.warning("⚠️ FIREBASE_SERVICE_ACCOUNT_FILE не настроен — бот не сможет читать/писать палатки!")

    if sheets_sync.is_configured():
        try:
            for tid in range(1, 21):
                await sync_tent_row(tid)
        except Exception as e:
            logging.error(f"❌ Не удалось выполнить стартовую синхронизацию с Google Sheets: {e}")

    await send_log("🟢 <b>Бот успешно запущен и готов к работе!</b>")
    print("🤖 Бот запущен! Планировщик (бэкапы и напоминания) активен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())