"""
Единый источник данных о палатках — Firestore (тот же проект, что использует
веб-приложение tz_manager_v2.html). Бот, веб-приложение и Google-таблица (через
sheets_sync.py, который читает данные уже ПОСЛЕ этого модуля) теперь показывают
одно и то же состояние палаток.

Схема документа в коллекции "tents" (совпадает с тем, что пишет веб-приложение):
    tentNum:   int        — номер палатки, 1..20 (используется для поиска документа,
                             а не id документа — так же, как делает веб-приложение)
    occupied:  bool
    player:    str         — ник игрока
    tgId:      int | None  — Telegram ID арендатора (поле добавлено ботом;
                             веб-приложение его просто не показывает, не мешает)
    label:     str         — цветовая метка ("none", "red", ...), не используется ботом напрямую
    endDate:   str "YYYY-MM-DD"
    note:      str
    amount:    number      — сумма последнего платежа
    payments:  list[{amount, date(ms epoch), endDate, note, player}]
    createdAt: int (ms epoch)
    updatedAt: int (ms epoch)

ВАЖНО: SQLite (tents.db → таблица tents) для статуса палаток больше НЕ используется
на чтение/запись в рабочем режиме — она играет роль только одноразового источника
миграции при самом первом запуске (см. migrate_sqlite_if_missing). Остальные таблицы
бота (users, pending_requests, blacklist, payments_history-зеркало для Excel,
stats_corrections, sent_reminders) остаются в SQLite как есть — они специфичны для
бота и в веб-приложении не отображаются, синхронизировать их не нужно.
"""
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timedelta

from config import FIREBASE_SERVICE_ACCOUNT_FILE

_app = None
_db = None
_init_failed = False


def is_configured() -> bool:
    return bool(FIREBASE_SERVICE_ACCOUNT_FILE)


def _ensure_init():
    """Ленивая синхронная инициализация Firebase Admin SDK. Вызывать только изнутри
    функций, уже обёрнутых в asyncio.to_thread."""
    global _app, _db, _init_failed
    if _db is not None or _init_failed:
        return
    if not is_configured():
        _init_failed = True
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_FILE)
        _app = firebase_admin.initialize_app(cred)
        _db = firestore.client()
    except Exception as e:
        logging.error(f"❌ Не удалось инициализировать Firebase Admin SDK: {e}")
        _init_failed = True


def _find_tent_doc_sync(tent_id: int):
    """Ищет документ палатки по полю tentNum (а не по id документа) — так же, как
    ищет веб-приложение. Возвращает DocumentSnapshot | None."""
    _ensure_init()
    if _db is None:
        return None
    query = _db.collection("tents").where("tentNum", "==", tent_id).limit(1).get()
    for doc in query:
        return doc
    return None


def _tuple_from_doc(tent_id: int, doc) -> tuple:
    """Приводит документ Firestore к тому же виду (id, tg_id, nickname, end_date),
    который раньше возвращала SQLite-функция get_tent — чтобы не переписывать
    весь код, который распаковывает этот кортеж по всему боту."""
    if doc is None:
        return (tent_id, None, None, None)
    data = doc.to_dict() or {}
    occupied = data.get("occupied", False)
    if not occupied:
        return (tent_id, None, None, None)
    return (tent_id, data.get("tgId"), data.get("player"), data.get("endDate"))


# === ЧТЕНИЕ ===

def _get_tent_sync(tent_id: int) -> tuple:
    doc = _find_tent_doc_sync(tent_id)
    return _tuple_from_doc(tent_id, doc)


async def get_tent(tent_id: int) -> tuple:
    return await asyncio.to_thread(_get_tent_sync, tent_id)


def _get_user_tent_sync(tg_id: int):
    _ensure_init()
    if _db is None:
        return None
    query = _db.collection("tents").where("tgId", "==", tg_id).where("occupied", "==", True).limit(1).get()
    for doc in query:
        data = doc.to_dict() or {}
        return (data.get("tentNum"), data.get("tgId"), data.get("player"), data.get("endDate"))
    return None


async def get_user_tent(tg_id: int):
    return await asyncio.to_thread(_get_user_tent_sync, tg_id)


def _get_all_tents_sync() -> list:
    """Возвращает все 20 палаток как список кортежей (id, tg_id, nickname, end_date),
    отсортированный по номеру — для админ-панели, /status, списков и т.п."""
    _ensure_init()
    result = {}
    if _db is not None:
        for doc in _db.collection("tents").stream():
            data = doc.to_dict() or {}
            n = data.get("tentNum")
            if n:
                result[n] = _tuple_from_doc(n, doc)
    return [result.get(n, (n, None, None, None)) for n in range(1, 21)]


async def get_all_tents() -> list:
    return await asyncio.to_thread(_get_all_tents_sync)


# === ЗАПИСЬ ===

def _write_payment_and_fields_sync(tent_id: int, fields: dict, payment: dict | None):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")

    doc = _find_tent_doc_sync(tent_id)
    now_ms = int(time.time() * 1000)
    fields = {**fields, "tentNum": tent_id, "updatedAt": now_ms}

    if doc is not None:
        ref = doc.reference
        data = doc.to_dict() or {}
        payments = list(data.get("payments") or [])
        if payment:
            payments.append(payment)
            fields["payments"] = payments
            fields["amount"] = payment["amount"]
        ref.update(fields)
    else:
        fields["createdAt"] = now_ms
        fields["payments"] = [payment] if payment else []
        if payment:
            fields["amount"] = payment["amount"]
        _db.collection("tents").add(fields)


def _make_payment(amount: int, end_date: str, note: str, player: str = None) -> dict:
    return {
        "amount": amount,
        "date": int(time.time() * 1000),
        "endDate": end_date,
        "note": note,
        "player": player,
    }


def _assign_tent_sync(tent_id: int, tg_id: int, nickname: str, days: int, price: int, note: str):
    end_date = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
    payment = _make_payment(price, end_date, note, nickname) if price else None
    _write_payment_and_fields_sync(
        tent_id,
        {"occupied": True, "player": nickname, "tgId": tg_id, "endDate": end_date, "note": ""},
        payment,
    )
    return end_date


async def assign_tent(tent_id: int, tg_id: int, nickname: str, days: int = 7, price: int = 0, note: str = "Занята палатка"):
    """Замена старой assign_tent_db — теперь пишет в Firestore и сразу добавляет
    запись в payments[], если передана цена (раньше платёж записывался отдельно,
    только в SQLite payments_history)."""
    return await asyncio.to_thread(_assign_tent_sync, tent_id, tg_id, nickname, days, price, note)


def _extend_tent_sync(tent_id: int, days: int, price: int, nickname: str, note: str):
    doc = _find_tent_doc_sync(tent_id)
    data = doc.to_dict() if doc else {}
    end_date_str = data.get("endDate") if data else None

    now = datetime.utcnow()
    if end_date_str:
        try:
            curr_end = datetime.strptime(end_date_str, "%Y-%m-%d")
            # ВАЖНО: та же бизнес-логика, что была в SQLite-версии — если аренда
            # уже истекла, новый срок считается от СЕГОДНЯ, а не "копится" дальше в прошлое.
            base = now if curr_end < now else curr_end
        except Exception:
            base = now
    else:
        base = now
    new_end = base + timedelta(days=days)
    new_end_str = new_end.strftime("%Y-%m-%d")

    payment = _make_payment(price, new_end_str, note, nickname) if price else None
    _write_payment_and_fields_sync(tent_id, {"endDate": new_end_str}, payment)
    return new_end


async def extend_tent(tent_id: int, days: int, price: int, nickname: str, note: str = "Продление"):
    """Замена старой extend_tent_db. Возвращает datetime новой даты окончания,
    как и раньше (вызывающий код форматирует его сам)."""
    return await asyncio.to_thread(_extend_tent_sync, tent_id, days, price, nickname, note)


def _clear_tent_sync(tent_id: int):
    _write_payment_and_fields_sync(
        tent_id,
        {"occupied": False, "player": "", "tgId": None, "endDate": None, "note": "", "amount": 0},
        None,
    )


async def clear_tent(tent_id: int):
    """Освобождает палатку. История платежей (payments[]) сохраняется — как и в
    веб-приложении, «Освободить» это мягкий сброс, а не удаление истории."""
    await asyncio.to_thread(_clear_tent_sync, tent_id)


def _update_tent_date_sync(tent_id: int, new_date_str: str):
    _write_payment_and_fields_sync(tent_id, {"endDate": new_date_str}, None)


async def update_tent_date(tent_id: int, new_date_str: str):
    await asyncio.to_thread(_update_tent_date_sync, tent_id, new_date_str)


# === ОДНОРАЗОВАЯ МИГРАЦИЯ ИЗ SQLite (только при первом запуске) ===

def _migrate_sync() -> dict:
    """Заполняет Firestore из локальной SQLite ТОЛЬКО для тех номеров палаток, для
    которых в Firestore ещё вообще нет документа. Если документ уже существует —
    Firestore считается более свежим/авторитетным и НЕ перезаписывается. Так мы не
    затираем данные, уже накопленные в веб-приложении, при первом подключении бота."""
    _ensure_init()
    report = {"migrated": [], "skipped_existing": [], "no_firestore": not bool(_db)}
    if _db is None:
        return report

    conn = sqlite3.connect("tents.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, tg_id, nickname, end_date FROM tents")
    sqlite_tents = {row[0]: row for row in cursor.fetchall()}
    conn.close()

    for tent_id in range(1, 21):
        existing = _find_tent_doc_sync(tent_id)
        if existing is not None:
            report["skipped_existing"].append(tent_id)
            continue
        row = sqlite_tents.get(tent_id)
        now_ms = int(time.time() * 1000)
        if row and row[1]:  # был занят в SQLite
            _, tg_id, nickname, end_date = row
            _db.collection("tents").add({
                "tentNum": tent_id, "occupied": True, "player": nickname, "tgId": tg_id,
                "endDate": end_date, "label": "none", "note": "", "amount": 0,
                "payments": [], "createdAt": now_ms, "updatedAt": now_ms,
            })
        else:
            _db.collection("tents").add({
                "tentNum": tent_id, "occupied": False, "player": "", "tgId": None,
                "endDate": None, "label": "none", "note": "", "amount": 0,
                "payments": [], "createdAt": now_ms, "updatedAt": now_ms,
            })
        report["migrated"].append(tent_id)
    return report


def _rename_player_sync(tg_id: int, new_nickname: str):
    _ensure_init()
    if _db is None:
        return
    query = _db.collection("tents").where("tgId", "==", tg_id).where("occupied", "==", True).limit(1).get()
    for doc in query:
        doc.reference.update({"player": new_nickname, "updatedAt": int(time.time() * 1000)})


async def rename_player_in_tent(tg_id: int, new_nickname: str):
    """Обновляет ник арендатора в его текущей палатке (если она у него есть) —
    вызывается при смене ника, чтобы Firestore/веб-приложение/таблица не показывали
    устаревшее имя."""
    await asyncio.to_thread(_rename_player_sync, tg_id, new_nickname)


async def migrate_sqlite_if_missing() -> dict:
    return await asyncio.to_thread(_migrate_sync)
