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
    MODERATOR_IDS, SUPER_ADMIN_ID, TAX_OFFICER_IDS
)

logging.basicConfig(level=logging.WARNING)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Часовой пояс МСК (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))

# === РОЛЕВАЯ СИСТЕМА И ПРОВЕРКА ПРАВ ===
def is_super_admin(user_id: int) -> bool:
    return user_id == SUPER_ADMIN_ID or user_id == ADMIN_ID

def can_manage_tents(user_id: int) -> bool:
    return is_super_admin(user_id) or user_id in MODERATOR_IDS

def can_export_reports(user_id: int) -> bool:
    return is_super_admin(user_id) or user_id in TAX_OFFICER_IDS


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


async def send_request_card_to_admins(photo_id: str, caption: str, reply_markup):
    """Отправляет карточку заявки (с фото чека и кнопками Подтвердить/Отклонить) всем админам/модераторам.
    Подтверждение на одной карточке помечает заявку обработанной — остальные копии
    при попытке нажать покажут 'Эта заявка уже обработана' (это безопасно)."""
    for admin_id in get_admin_recipients():
        try:
            await bot.send_photo(chat_id=admin_id, photo=photo_id, caption=caption, reply_markup=reply_markup)
        except Exception as e:
            logging.error(f"❌ Не удалось отправить карточку заявки админу {admin_id}: {e}")


# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()

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

    cursor.execute("PRAGMA table_info(payments_history)")
    columns = [col[1] for col in cursor.fetchall()]
    if "photo_id" not in columns:
        cursor.execute("ALTER TABLE payments_history ADD COLUMN photo_id TEXT")

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


def delete_user_db(tg_id):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
    cursor.execute("UPDATE tents SET tg_id = NULL, nickname = NULL, end_date = NULL WHERE tg_id = ?", (tg_id,))
    conn.commit()
    conn.close()


def get_all_users():
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tg_id, username, nickname FROM users")
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_user_tent(tg_id):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, tg_id, nickname, end_date FROM tents WHERE tg_id = ?", (tg_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_tent(tent_id):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, tg_id, nickname, end_date FROM tents WHERE id = ?", (tent_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def assign_tent_db(tent_id, tg_id, nickname, days=7):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    end_date = (datetime.now(MSK_TZ) + timedelta(days=days)).strftime("%Y-%m-%d")
    cursor.execute("UPDATE tents SET tg_id = ?, nickname = ?, end_date = ? WHERE id = ?", (tg_id, nickname, end_date, tent_id))
    conn.commit()
    conn.close()


def clear_tent_db(tent_id):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE tents SET tg_id = NULL, nickname = NULL, end_date = NULL WHERE id = ?", (tent_id,))
    conn.commit()
    conn.close()


def update_tent_date_db(tent_id, new_date_str):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE tents SET end_date = ? WHERE id = ?", (new_date_str, tent_id))
    conn.commit()
    conn.close()


def extend_tent_db(tent_id, days, price, photo_id, nickname):
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT end_date FROM tents WHERE id = ?", (tent_id,))
    row = cursor.fetchone()
    end_date_str = row[0] if row else None

    now_msk = datetime.now(MSK_TZ)

    if end_date_str:
        try:
            curr_end = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=MSK_TZ)
            if curr_end < now_msk:
                new_end = now_msk + timedelta(days=days)
            else:
                new_end = curr_end + timedelta(days=days)
        except Exception:
            new_end = now_msk + timedelta(days=days)
    else:
        new_end = now_msk + timedelta(days=days)

    today = now_msk.strftime("%d.%m.%Y %H:%M")
    cursor.execute("UPDATE tents SET end_date = ? WHERE id = ?", (new_end.strftime("%Y-%m-%d"), tent_id))

    cursor.execute(
        "INSERT INTO payments_history (tent_id, nickname, price, days, photo_id, pay_date) VALUES (?, ?, ?, ?, ?, ?)",
        (tent_id, nickname, price, days, photo_id, today)
    )

    conn.commit()
    conn.close()
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

# ➕ Состояния для самостоятельного выбора и аренды свободной палатки
class RentTentState(StatesGroup):
    waiting_for_tent_choice = State()
    waiting_for_tariff = State()
    waiting_for_photo = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()

class EditDateState(StatesGroup):
    waiting_for_new_date = State()

class EditStatsState(StatesGroup):
    waiting_for_oil = State()
    waiting_for_deals = State()


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
        await cmd_admin(callback.message)
    else:
        conn = sqlite3.connect("tents.db")
        cursor = conn.cursor()
        cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (callback.from_user.id,))
        user = cursor.fetchone()
        conn.close()
        nick = user[0] if user else "Игрок"
        await show_main_menu(callback.message, nick)


# === МЕНЮ ПОЛЬЗОВАТЕЛЯ ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
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
        await message.answer("👋 Приветствуем в Торговой Зоне!\n\nПожалуйста, введите ваш игровой никнейм в Minecraft:")
        await state.set_state(RegisterState.waiting_for_nickname)
    else:
        await show_main_menu(message, user[0])


async def show_main_menu(message: types.Message, nickname: str):
    tent = get_user_tent(message.from_user.id)
    
    kb = InlineKeyboardBuilder()
    if not tent or not tent[1]:
        kb.button(text="⛺ Арендовать палатку", callback_data="choose_free_tent")
    else:
        kb.button(text="⛺ Моя палатка", callback_data="my_tent")
        
    kb.button(text="✏️ Изменить ник", callback_data="edit_nick")
    kb.adjust(1)
    await message.answer(f"👋 С возвращением, {nickname}!\nВыберите действие:", reply_markup=kb.as_markup())


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
    await show_main_menu(message, nickname)


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

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE tents SET nickname = ? WHERE tg_id = ?", (new_nick, message.from_user.id))
    conn.commit()
    conn.close()

    await send_log(f"✏️ <b>Смена ника:</b>\nИгрок ID <code>{message.from_user.id}</code> сменил ник на <code>{new_nick}</code>")

    await message.answer(f"✅ Ваш никнейм обновлён на: {new_nick}")
    await state.clear()
    await show_main_menu(message, new_nick)


# === ⛺ АРЕНДА СВОБОДНОЙ ПАЛАТКИ ИГРОКОМ ===
@dp.callback_query(F.data == "choose_free_tent")
async def show_free_tents_for_user(callback: types.CallbackQuery, state: FSMContext):
    existing_tent = get_user_tent(callback.from_user.id)
    if existing_tent and existing_tent[1]:
        await callback.answer("⚠️ У вас уже есть активная палатка!", show_alert=True)
        return

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    # ВАЖНО: исключаем палатки, на которые уже подана заявка и ждёт подтверждения админом —
    # иначе палатка показывается "свободной" даже когда её уже кто-то занимает (гонка заявок).
    cursor.execute("""
        SELECT id FROM tents
        WHERE tg_id IS NULL
        AND id NOT IN (SELECT tent_id FROM pending_requests)
        ORDER BY id ASC
    """)
    free_tents = cursor.fetchall()
    conn.close()

    if not free_tents:
        await callback.answer("❌ На данный момент свободных палаток нет!", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for (t_id,) in free_tents:
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
    await show_main_menu(callback.message, nick)


@dp.callback_query(F.data.startswith("user_rent_tent_"), RentTentState.waiting_for_tent_choice)
async def process_user_selected_tent(callback: types.CallbackQuery, state: FSMContext):
    tent_id = int(callback.data.split("_")[3])
    
    tent = get_tent(tent_id)
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

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить", callback_data="back_to_main_menu")

    await callback.message.edit_text(
        f"Вы выбрали тариф: {tariff['label']}\n\n"
        f"📸 Переведите {tariff['price']} 🛢️ нефти в игре и отправьте скриншот/фото чека прямо сюда в чат.",
        reply_markup=kb.as_markup()
    )
    await state.set_state(RentTentState.waiting_for_photo)


@dp.message(RentTentState.waiting_for_photo, F.photo)
async def process_user_rent_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tent_id = data.get("tent_id")
    tariff_code = data.get("tariff_code")

    if not tent_id or not tariff_code:
        await message.answer("❌ Произошла ошибка состояния. Попробуйте начать заново через /start.")
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    photo_id = message.photo[-1].file_id

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (message.from_user.id,))
    user_row = cursor.fetchone()
    user_nick = user_row[0] if user_row else "Неизвестно"

    cursor.execute(
        "INSERT INTO pending_requests (tent_id, user_id, tariff_code, photo_id) VALUES (?, ?, ?, ?)",
        (tent_id, message.from_user.id, tariff_code, photo_id)
    )
    req_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await message.answer("⏳ Ваша заявка на аренду палатки отправлена Администрации! Ожидайте подтверждения.")

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"appr_{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"rej_{req_id}")
    kb.adjust(2)

    username = message.from_user.username or "нет"
    caption_text = (
        f"📥 НОВАЯ ЗАЯВКА НА АРЕНДУ ПАЛАТКИ!\n\n"
        f"⛺ Запрошена Палатка №{tent_id}\n"
        f"👤 Игрок: @{username} (Ник: {user_nick})\n"
        f"Тариф: {tariff['label']}"
    )

    try:
        await send_request_card_to_admins(photo_id, caption_text, kb.as_markup())
    except Exception as e:
        logging.error(f"Ошибка отправки карточки заявки админу: {e}")

    await state.clear()


@dp.callback_query(F.data == "my_tent")
async def show_my_tent(callback: types.CallbackQuery):
    tent = get_user_tent(callback.from_user.id)
    if not tent or not tent[1]:
        await callback.message.answer("⏳ За вашим аккаунтом пока не закреплена палатка.\nВыберите в меню 'Арендовать палатку'.")
        return

    tent_id, tg_id, nickname, end_date_str = tent
    now_msk = datetime.now(MSK_TZ).replace(tzinfo=None)

    try:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        days_left = (end_date - now_msk).days + 1
        end_date_formatted = end_date.strftime("%d.%m.%Y")
    except Exception:
        days_left = 0
        end_date_formatted = "Не определено"

    if days_left < 0:
        status = f"🔴 ПРОСРОЧЕНА на {abs(days_left)} дн."
    elif days_left <= 2:
        status = f"🟡 Заканчивается (осталось {days_left} дн.)"
    else:
        status = f"🟢 Активна (осталось {days_left} дн.)"

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
    tent = get_tent(tent_id)

    clear_tent_db(tent_id)
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
    owned_tent = get_user_tent(callback.from_user.id)
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


@dp.message(RenewState.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tent_id = data.get("tent_id")
    tariff_code = data.get("tariff_code")

    if not tent_id or not tariff_code:
        await message.answer("❌ Ошибка состояния. Попробуйте снова через 'Моя палатка' -> 'Продлить аренду'.")
        await state.clear()
        return

    owned_tent = get_user_tent(message.from_user.id)
    if not owned_tent or owned_tent[1] is None or owned_tent[0] != tent_id:
        await message.answer("❌ Это не ваша палатка! Продление отменено.")
        await state.clear()
        return

    tariff = TARIFFS[tariff_code]
    photo_id = message.photo[-1].file_id

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO pending_requests (tent_id, user_id, tariff_code, photo_id) VALUES (?, ?, ?, ?)",
        (tent_id, message.from_user.id, tariff_code, photo_id)
    )
    req_id = cursor.lastrowid
    conn.commit()
    conn.close()

    await message.answer("⏳ Ваша заявка отправлена Администрации! Ожидайте подтверждения.")

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"appr_{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"rej_{req_id}")
    kb.adjust(2)

    tent = get_tent(tent_id)
    user_nick = tent[2] if tent and tent[2] else "Неизвестно"
    username = message.from_user.username or "нет"

    caption_text = (
        f"📥 НОВАЯ ЗАЯВКА НА ПРОДЛЕНИЕ!\n\n"
        f"⛺ Палатка №{tent_id}\n"
        f"👤 Игрок: @{username} (Ник: {user_nick})\n"
        f"Тариф: {tariff['label']}"
    )

    try:
        await send_request_card_to_admins(photo_id, caption_text, kb.as_markup())
    except Exception as e:
        logging.error(f"Ошибка отправки карточки: {e}")

    await state.clear()


@dp.callback_query(F.data.startswith("appr_"))
async def approve_payment(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        await callback.answer("❌ У вас нет прав для подтверждения платежей!", show_alert=True)
        return

    req_id = int(callback.data.split("_")[1])

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tent_id, user_id, tariff_code, photo_id FROM pending_requests WHERE id = ?", (req_id,))
    req = cursor.fetchone()

    if not req:
        await callback.answer("⚠️ Эта заявка уже обработана!")
        conn.close()
        return

    tent_id, user_id, tariff_code, photo_id = req
    tariff = TARIFFS[tariff_code]
    
    cursor.execute("SELECT nickname FROM users WHERE tg_id = ?", (user_id,))
    u_row = cursor.fetchone()
    user_nick = u_row[0] if u_row else "Игрок"

    tent_data = get_tent(tent_id)
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
        assign_tent_db(tent_id, user_id, user_nick, days=tariff["days"])
        
        now_msk = datetime.now(MSK_TZ)
        today = now_msk.strftime("%d.%m.%Y %H:%M")
        cursor.execute(
            "INSERT INTO payments_history (tent_id, nickname, price, days, photo_id, pay_date) VALUES (?, ?, ?, ?, ?, ?)",
            (tent_id, user_nick, tariff["price"], tariff["days"], photo_id, today)
        )
        conn.commit()
        
        updated_tent = get_tent(tent_id)
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
        new_end = extend_tent_db(tent_id, tariff["days"], tariff["price"], photo_id, user_nick)
        formatted_end_date = new_end.strftime('%d.%m.%Y')

    cursor.execute("DELETE FROM pending_requests WHERE id = ?", (req_id,))
    conn.commit()
    conn.close()

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
    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, tg_id, nickname, end_date FROM tents WHERE tg_id IS NOT NULL AND end_date IS NOT NULL")
    tents = cursor.fetchall()
    conn.close()

    now_msk_date = datetime.now(MSK_TZ).date()

    for tent_id, tg_id, nick, end_date_str in tents:
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
    tent = get_tent(tent_id)

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

    tent = get_tent(tent_id)
    update_tent_date_db(tent_id, parsed_date)

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


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not can_manage_tents(message.from_user.id):
        return

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, nickname, end_date FROM tents")
    tents = cursor.fetchall()
    conn.close()

    kb = InlineKeyboardBuilder()
    for t in tents:
        t_id, nick, end = t
        btn_text = f"#{t_id} 🟢 {nick}" if nick else f"#{t_id} ⚪ Свободна"
        kb.button(text=btn_text, callback_data=f"adm_tent_{t_id}")

    kb.adjust(2)
    if is_super_admin(message.from_user.id):
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

    await message.answer("🛠️ УПРАВЛЕНИЕ ПАЛАТКАМИ И СИСТЕМОЙ:", reply_markup=kb.as_markup())


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

    cursor.execute("SELECT COUNT(*) FROM tents WHERE nickname IS NOT NULL")
    occupied_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM users")
    users_count = cursor.fetchone()[0]

    cursor.execute("SELECT price, pay_date FROM payments_history")
    all_payments = cursor.fetchall()

    cursor.execute("SELECT id, nickname, end_date FROM tents WHERE nickname IS NOT NULL")
    tents = cursor.fetchall()
    conn.close()

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
    for t_id, nick, end_str in tents:
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
        "SELECT nickname, price, days, photo_id, pay_date FROM payments_history WHERE tent_id = ? ORDER BY id DESC",
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

    for nick, price, days, photo_id, pay_date in filtered_rows:
        caption = (
            f"🧾 Оплата Палатки №{tent_id}\n"
            f"👤 Игрок: {nick}\n"
            f"💰 Сумма: {price} 🛢️ нефти ({days} дн.)\n"
            f"📅 Дата оплаты: {pay_date}"
        )
        try:
            if photo_id:
                await bot.send_photo(chat_id=callback.from_user.id, photo=photo_id, caption=caption)
            else:
                await bot.send_message(chat_id=callback.from_user.id, text=f"{caption}\n(Фото отсутствует)")
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"Ошибка вывода чека: {e}")


@dp.callback_query(F.data.startswith("adm_tent_"))
async def adm_tent_manage(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        return

    tent_id = int(callback.data.split("_")[2])
    tent = get_tent(tent_id)
    _, tg_id, nickname, end_date = tent

    kb = InlineKeyboardBuilder()

    if not nickname:
        text = f"⛺ Палатка №{tent_id}\nСтатус: ⚪ Свободна"
        conn = sqlite3.connect("tents.db")
        cursor = conn.cursor()
        cursor.execute("SELECT u.tg_id, u.nickname FROM users u LEFT JOIN tents t ON u.tg_id = t.tg_id WHERE t.id IS NULL")
        unassigned = cursor.fetchall()
        conn.close()

        if unassigned:
            text += "\n\nИгроки без палатки:"
            for u_id, u_nick in unassigned:
                kb.button(text=f"➕ Выдать {u_nick}", callback_data=f"give_{tent_id}_{u_id}_{u_nick}")
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
        kb.button(text="❌ Завершить аренду (Освободить)", callback_data=f"clear_{tent_id}_{tg_id}")

    kb.button(text="🔙 Назад в меню", callback_data="back_admin")
    kb.adjust(1)

    await callback.message.edit_text(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("give_"))
async def process_give(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        return

    _, tent_id, user_id, nickname = callback.data.split("_")
    assign_tent_db(int(tent_id), int(user_id), nickname)

    tent = get_tent(int(tent_id))
    end_date_str = tent[3] if tent else None
    try:
        formatted_end = datetime.strptime(end_date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        formatted_end = end_date_str or "не установлена"

    # Уведомляем самого игрока о том, что ему выдали палатку — раньше он узнавал об этом
    # только зайдя в бота случайно, никакого сообщения не приходило.
    try:
        await bot.send_message(
            chat_id=int(user_id),
            text=(
                f"🎉 <b>Вам выдана палатка!</b>\n\n"
                f"⛺ Палатка №{tent_id}\n"
                f"📅 Оплачена до: {formatted_end} 23:59:59\n\n"
                f"Откройте /start → «⛺ Моя палатка», чтобы посмотреть детали или продлить аренду."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"❌ Не удалось уведомить игрока {user_id} о выдаче палатки: {e}")

    await send_log(f"⛺ <b>Выдана палатка:</b>\nПалатка №{tent_id} выдана игроку <code>{nickname}</code> (ID: <code>{user_id}</code>)")
    await callback.answer(f"Палатка №{tent_id} выдана {nickname}!")
    await cmd_admin(callback.message)


@dp.callback_query(F.data.startswith("clear_"))
async def process_clear(callback: types.CallbackQuery):
    if not can_manage_tents(callback.from_user.id):
        return

    _, tent_id, user_id = callback.data.split("_")
    tent = get_tent(int(tent_id))
    clear_tent_db(int(tent_id))

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
    await cmd_admin(callback.message)


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
        kb.button(text=f"🚫 Забанить {nick}", callback_data=f"ban_user_{u_id}")
        kb.button(text=f"🗑️ Удалить {nick}", callback_data=f"del_user_{u_id}")

    kb.button(text="📊 Отчёт статистики", callback_data="adm_stats_menu")
    kb.button(text="🔙 Назад в меню", callback_data="back_admin")
    kb.adjust(2)

    await callback.message.edit_text(msg, reply_markup=kb.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("del_user_"))
async def process_del_user(callback: types.CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        return

    user_id = int(callback.data.split("_")[2])
    delete_user_db(user_id)

    await send_log(f"🗑️ <b>Удаление аккаунта:</b> Пользователь ID <code>{user_id}</code> был удалён из базы.")
    await callback.answer("❌ Игрок удалён из базы!")
    await adm_users_list(callback)


@dp.callback_query(F.data == "back_admin")
async def back_admin(callback: types.CallbackQuery, state: FSMContext):
    if not can_manage_tents(callback.from_user.id):
        return
    await state.clear()
    await cmd_admin(callback.message)


# === 🚀 ЗАПУСК БОТА И ПЛАНИРОВЩИКА ===
async def main():
    init_db()

    scheduler = AsyncIOScheduler(timezone=MSK_TZ)
    scheduler.add_job(send_db_backup, 'interval', days=3)
    scheduler.add_job(check_tent_expirations, 'cron', hour=12, minute=0)
    scheduler.start()

    await send_log("🟢 <b>Бот успешно запущен и готов к работе!</b>")
    print("🤖 Бот запущен! Планировщик (бэкапы и напоминания) активен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())