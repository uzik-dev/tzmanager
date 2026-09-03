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
import threading
import time
from datetime import datetime, timedelta, timezone

from config import FIREBASE_SERVICE_ACCOUNT_FILE

# Часовой пояс МСК (UTC+3) — все даты окончания аренды считаются по нему, а не по UTC,
# т.к. пользователю везде показывается "до ХХ.ХХ.ХХХХ 23:59:59 (МСК)". Раньше здесь
# использовался datetime.utcnow(), из-за чего в промежутке 00:00–02:59 по МСК (когда
# в UTC ещё "вчера") аренда/продление считались на календарный день короче, чем должны.
MSK_TZ = timezone(timedelta(hours=3))


def _msk_now_naive() -> datetime:
    """Текущее время в МСК без информации о часовом поясе (naive) — чтобы формат
    совпадал с уже сохранёнными в Firestore/SQLite строками дат."""
    return datetime.now(MSK_TZ).replace(tzinfo=None)

_app = None
_db = None
_init_failed = False
_init_lock = threading.Lock()


def is_configured() -> bool:
    return bool(FIREBASE_SERVICE_ACCOUNT_FILE)


def _ensure_init():
    """Ленивая синхронная инициализация Firebase Admin SDK. Вызывать только изнутри
    функций, уже обёрнутых в asyncio.to_thread."""
    global _app, _db, _init_failed
    if _db is not None or _init_failed:
        return
    with _init_lock:
        if _db is not None or _init_failed:
            return
        if not is_configured():
            _init_failed = True
            return
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
            try:
                _app = firebase_admin.get_app()
            except ValueError:
                cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_FILE)
                _app = firebase_admin.initialize_app(cred)
            _db = firestore.client(app=_app)
        except Exception as e:
            logging.error(f"❌ Не удалось инициализировать Firebase Admin SDK: {e}")
            _init_failed = True


def _find_tent_doc_sync(tent_id: int):
    """Ищет документ палатки по полю tentNum (а не по id документа) — так же, как
    ищет веб-приложение. Возвращает DocumentSnapshot | None."""
    _ensure_init()
    if _db is None:
        return None
    from google.cloud.firestore_v1.base_query import FieldFilter
    query = _db.collection("tents").where(filter=FieldFilter("tentNum", "==", tent_id)).limit(1).get()
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


def _tent_data_sync(tent_id: int) -> dict:
    doc = _find_tent_doc_sync(tent_id)
    return doc.to_dict() if doc is not None else {}


async def get_tent_data(tent_id: int) -> dict:
    return await asyncio.to_thread(_tent_data_sync, tent_id)


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
    from google.cloud.firestore_v1.base_query import FieldFilter
    query = (_db.collection("tents")
             .where(filter=FieldFilter("tgId", "==", tg_id))
             .where(filter=FieldFilter("occupied", "==", True))
             .limit(1).get())
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


def _get_tent_catalog_sources_sync() -> list:
    _ensure_init()
    if _db is None:
        return []
    result = []
    for doc in _db.collection("tents").stream():
        data = doc.to_dict() or {}
        if data.get("tentNum"):
            result.append({"id": doc.id, "tentNum": data.get("tentNum"), "player": data.get("player", ""), "shopSheet": data.get("shopSheet", "")})
    return result


async def get_tent_catalog_sources() -> list:
    return await asyncio.to_thread(_get_tent_catalog_sources_sync)


def _get_products_sync(tent_id: int) -> list:
    _ensure_init()
    if _db is None:
        return []
    from google.cloud.firestore_v1.base_query import FieldFilter
    result = []
    for doc in _db.collection("products").where(filter=FieldFilter("tentNum", "==", tent_id)).stream():
        result.append({"id": doc.id, **(doc.to_dict() or {})})
    return result


async def get_products(tent_id: int) -> list:
    return await asyncio.to_thread(_get_products_sync, tent_id)


def _get_all_products_sync() -> list:
    _ensure_init()
    if _db is None:
        return []
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in _db.collection("products").stream()]


async def get_all_products() -> list:
    return await asyncio.to_thread(_get_all_products_sync)


def _upsert_bot_document_sync(collection_name: str, document_id: str, data: dict):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    _db.collection(collection_name).document(document_id).set(data, merge=True)


async def upsert_bot_document(collection_name: str, document_id: str, data: dict):
    await asyncio.to_thread(_upsert_bot_document_sync, collection_name, document_id, data)


def _get_bot_document_sync(collection_name: str, document_id: str) -> dict:
    _ensure_init()
    if _db is None:
        return {}
    snapshot = _db.collection(collection_name).document(document_id).get()
    return {"id": snapshot.id, **(snapshot.to_dict() or {})} if snapshot.exists else {}


async def get_bot_document(collection_name: str, document_id: str) -> dict:
    return await asyncio.to_thread(_get_bot_document_sync, collection_name, document_id)


def _list_bot_documents_sync(collection_name: str) -> list:
    _ensure_init()
    if _db is None:
        return []
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in _db.collection(collection_name).stream()]


async def list_bot_documents(collection_name: str) -> list:
    return await asyncio.to_thread(_list_bot_documents_sync, collection_name)


def _delete_bot_document_sync(collection_name: str, document_id: str):
    _ensure_init()
    if _db is not None:
        _db.collection(collection_name).document(document_id).delete()


async def delete_bot_document(collection_name: str, document_id: str):
    await asyncio.to_thread(_delete_bot_document_sync, collection_name, document_id)


def _upsert_product_sync(product_id: str, data: dict):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    now_ms = int(time.time() * 1000)
    payload = {**data, "updatedAt": now_ms}
    if product_id:
        _db.collection("products").document(product_id).set(payload, merge=True)
        return product_id
    return _db.collection("products").add({**payload, "createdAt": now_ms})[1].id


async def upsert_product(product_id: str, data: dict) -> str:
    return await asyncio.to_thread(_upsert_product_sync, product_id, data)


def _sync_product_from_sheet_sync(product_id: str, data: dict):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    sync_at = int(data.get("sourceUpdatedAt") or time.time() * 1000)
    payload = {**data, "updatedAt": sync_at, "lastSheetSyncAt": sync_at}
    _db.collection("products").document(product_id).set(payload, merge=True)


async def sync_product_from_sheet(product_id: str, data: dict):
    await asyncio.to_thread(_sync_product_from_sheet_sync, product_id, data)


def _mark_product_sheet_synced_sync(product_id: str, sync_at: int):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    _db.collection("products").document(product_id).update({"lastSheetSyncAt": sync_at})


async def mark_product_sheet_synced(product_id: str, sync_at: int):
    await asyncio.to_thread(_mark_product_sheet_synced_sync, product_id, sync_at)


def _delete_product_sync(product_id: str):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    _db.collection("products").document(product_id).delete()


async def delete_product(product_id: str):
    await asyncio.to_thread(_delete_product_sync, product_id)


def _get_partner_requests_sync(tg_id: int) -> list:
    _ensure_init()
    if _db is None:
        return []
    from google.cloud.firestore_v1.base_query import FieldFilter
    result = []
    for doc in _db.collection("partner_requests").where(filter=FieldFilter("status", "==", "pending")).stream():
        data = doc.to_dict() or {}
        if int(data.get("ownerTgId", 0)) == int(tg_id) or int(data.get("requesterTgId", 0)) == int(tg_id):
            result.append({"id": doc.id, **data})
    return result


async def get_partner_requests(tg_id: int) -> list:
    return await asyncio.to_thread(_get_partner_requests_sync, tg_id)


def _get_pending_partner_requests_sync() -> list:
    _ensure_init()
    if _db is None:
        return []
    from google.cloud.firestore_v1.base_query import FieldFilter
    result = []
    query = _db.collection("partner_requests").where(filter=FieldFilter("status", "==", "pending")).stream()
    for doc in query:
        result.append({"id": doc.id, **(doc.to_dict() or {})})
    return result


async def get_pending_partner_requests() -> list:
    return await asyncio.to_thread(_get_pending_partner_requests_sync)


def _mark_partner_request_notified_sync(request_id: str, recipient: str):
    _ensure_init()
    if _db is None:
        return
    _db.collection("partner_requests").document(request_id).update({f"{recipient}NotifiedAt": int(time.time() * 1000)})


async def mark_partner_request_notified(request_id: str, recipient: str):
    await asyncio.to_thread(_mark_partner_request_notified_sync, request_id, recipient)


def _get_pending_rental_requests_sync() -> list:
    _ensure_init()
    if _db is None:
        return []
    from google.cloud.firestore_v1.base_query import FieldFilter
    result = []
    query = _db.collection("rental_requests").where(filter=FieldFilter("status", "==", "pending")).stream()
    for doc in query:
        result.append({"id": doc.id, **(doc.to_dict() or {})})
    return result


async def get_pending_rental_requests() -> list:
    return await asyncio.to_thread(_get_pending_rental_requests_sync)


def _create_tent_claim_sync(data: dict) -> str:
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    now_ms = int(time.time() * 1000)
    ref = _db.collection("tent_claim_requests").add({**data, "status": "pending", "createdAt": now_ms})[1]
    return ref.id


async def create_tent_claim(data: dict) -> str:
    return await asyncio.to_thread(_create_tent_claim_sync, data)


def _get_pending_tent_claims_sync() -> list:
    _ensure_init()
    if _db is None:
        return []
    from google.cloud.firestore_v1.base_query import FieldFilter
    return [{"id": doc.id, **(doc.to_dict() or {})}
            for doc in _db.collection("tent_claim_requests").where(filter=FieldFilter("status", "==", "pending")).stream()]


async def get_pending_tent_claims() -> list:
    return await asyncio.to_thread(_get_pending_tent_claims_sync)


def _resolve_tent_claim_sync(request_id: str, approve: bool, admin_id: int, reason: str = ""):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    request_ref = _db.collection("tent_claim_requests").document(request_id)
    request_snapshot = request_ref.get()
    request = request_snapshot.to_dict() or {}
    if request.get("status") != "pending":
        return request
    status = "approved" if approve else "rejected"
    now_ms = int(time.time() * 1000)
    if approve:
        tent_ref = _find_tent_doc_sync(int(request["tentNum"]))
        if tent_ref is None or not (tent_ref.to_dict() or {}).get("occupied"):
            raise ValueError("Палатка свободна или не найдена")
        tent_data = tent_ref.to_dict() or {}
        if tent_data.get("tgId") and int(tent_data["tgId"]) != int(request["requesterTgId"]):
            raise ValueError("У палатки уже есть владелец")
        for other_tent in _db.collection("tents").stream():
            other_data = other_tent.to_dict() or {}
            if int(other_data.get("tentNum") or 0) != int(request["tentNum"]) and int(other_data.get("tgId") or 0) == int(request["requesterTgId"]):
                raise ValueError("У этого Telegram уже есть привязанная палатка")
        tent_ref.reference.update({"tgId": int(request["requesterTgId"]), "ownerUsername": request.get("requesterUsername", ""), "ownerMinecraftNick": request.get("minecraftNick", ""), "updatedAt": now_ms})
    request_ref.update({"status": status, "resolvedAt": now_ms, "resolvedByTgId": admin_id, "reason": reason})
    return request


async def resolve_tent_claim(request_id: str, approve: bool, admin_id: int, reason: str = ""):
    return await asyncio.to_thread(_resolve_tent_claim_sync, request_id, approve, admin_id, reason)


def _get_pending_support_requests_sync() -> list:
    _ensure_init()
    if _db is None:
        return []
    from google.cloud.firestore_v1.base_query import FieldFilter
    result = []
    for doc in _db.collection("support_requests").where(filter=FieldFilter("status", "==", "pending")).stream():
        result.append({"id": doc.id, **(doc.to_dict() or {})})
    return result


async def get_pending_support_requests() -> list:
    return await asyncio.to_thread(_get_pending_support_requests_sync)


def _mark_support_request_notified_sync(request_id: str):
    _ensure_init()
    if _db is not None:
        _db.collection("support_requests").document(request_id).update({"adminNotifiedAt": int(time.time() * 1000)})


async def mark_support_request_notified(request_id: str):
    await asyncio.to_thread(_mark_support_request_notified_sync, request_id)


def _get_rental_request_sync(request_id: str) -> dict:
    _ensure_init()
    if _db is None:
        return {}
    snapshot = _db.collection("rental_requests").document(request_id).get()
    return {"id": snapshot.id, **(snapshot.to_dict() or {})} if snapshot.exists else {}


async def get_rental_request(request_id: str) -> dict:
    return await asyncio.to_thread(_get_rental_request_sync, request_id)


def _mark_rental_request_notified_sync(request_id: str):
    _ensure_init()
    if _db is not None:
        _db.collection("rental_requests").document(request_id).update({"adminNotifiedAt": int(time.time() * 1000)})


async def mark_rental_request_notified(request_id: str):
    await asyncio.to_thread(_mark_rental_request_notified_sync, request_id)


def _resolve_rental_request_sync(request_id: str, status: str):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    ref = _db.collection("rental_requests").document(request_id)
    snapshot = ref.get()
    data = snapshot.to_dict() or {}
    if data.get("status") != "pending":
        return data
    ref.update({"status": status, "resolvedAt": int(time.time() * 1000)})
    return data


async def resolve_rental_request(request_id: str, status: str) -> dict:
    return await asyncio.to_thread(_resolve_rental_request_sync, request_id, status)


def _create_partner_request_sync(data: dict) -> str:
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    now_ms = int(time.time() * 1000)
    ref = _db.collection("partner_requests").add({**data, "status": "pending", "createdAt": now_ms})[1]
    return ref.id


async def create_partner_request(data: dict) -> str:
    return await asyncio.to_thread(_create_partner_request_sync, data)


def _resolve_partner_request_sync(request_id: str, approve: bool):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    request_ref = _db.collection("partner_requests").document(request_id)
    snapshot = request_ref.get()
    data = snapshot.to_dict() or {}
    if data.get("status") != "pending":
        return
    status = "approved" if approve else "rejected"
    request_ref.update({"status": status, "resolvedAt": int(time.time() * 1000)})
    if approve:
        tent_ref = _find_tent_doc_sync(int(data["tentNum"]))
        if tent_ref is not None:
            tent_ref.reference.update({
                "partnerTgId": int(data["requesterTgId"]),
                "partnerUsername": data.get("requesterUsername", ""),
                "partnerMinecraftNick": data.get("requesterMinecraftNick", ""),
                "updatedAt": int(time.time() * 1000),
            })


async def resolve_partner_request(request_id: str, approve: bool):
    await asyncio.to_thread(_resolve_partner_request_sync, request_id, approve)


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


def _record_rental_history_sync(tent_id: int, player: str, amount: int, days: int,
                                end_date: str, operation_type: str = "renew",
                                source: str = "bot", tg_id: int | None = None,
                                photo_id: str | None = None, request_id: str | None = None,
                                note: str = ""):
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    operation_at = int(time.time() * 1000)
    payload = {
        "tentNum": int(tent_id),
        "player": player or "",
        "tgId": tg_id,
        "amount": int(amount or 0),
        "days": int(days or 0),
        "endDate": end_date,
        "operationType": operation_type,
        "operationAt": operation_at,
        "source": source,
        "note": note,
    }
    if photo_id:
        payload["photoId"] = photo_id
    if request_id:
        payload["requestId"] = request_id
    _db.collection("rental_history").add(payload)


async def record_rental_history(tent_id: int, player: str, amount: int, days: int,
                                end_date: str, operation_type: str = "renew",
                                source: str = "bot", tg_id: int | None = None,
                                photo_id: str | None = None, request_id: str | None = None,
                                note: str = ""):
    await asyncio.to_thread(
        _record_rental_history_sync, tent_id, player, amount, days, end_date,
        operation_type, source, tg_id, photo_id, request_id, note,
    )


def _assign_tent_sync(tent_id: int, tg_id: int, nickname: str, days: int, price: int, note: str):
    end_date = (_msk_now_naive() + timedelta(days=days)).strftime("%Y-%m-%d")
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

    now = _msk_now_naive()
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
    from google.cloud.firestore_v1.base_query import FieldFilter
    query = (_db.collection("tents")
             .where(filter=FieldFilter("tgId", "==", tg_id))
             .where(filter=FieldFilter("occupied", "==", True))
             .limit(1).get())
    for doc in query:
        doc.reference.update({"player": new_nickname, "updatedAt": int(time.time() * 1000)})


async def rename_player_in_tent(tg_id: int, new_nickname: str):
    """Обновляет ник арендатора в его текущей палатке (если она у него есть) —
    вызывается при смене ника, чтобы Firestore/веб-приложение/таблица не показывали
    устаревшее имя."""
    await asyncio.to_thread(_rename_player_sync, tg_id, new_nickname)


async def migrate_sqlite_if_missing() -> dict:
    return await asyncio.to_thread(_migrate_sync)


# ══════════════════════════════════════════════════════════════════════════
# === ЧАТ С АДМИНИСТРАЦИЕЙ (двусторонний, отображается прямо в Mini App) ===
# Коллекция "chat_messages": каждый документ — одно сообщение треда конкретного
# игрока. sender: "user" | "admin". Тред пользователя = все сообщения с его userId,
# отсортированные по createdAt — рендерится в Mini App как обычный чат.
# ══════════════════════════════════════════════════════════════════════════
def _add_chat_message_sync(user_id: int, sender: str, text: str, username: str = None, admin_name: str = None) -> str:
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    now_ms = int(time.time() * 1000)
    data = {
        "userId": int(user_id),
        "sender": sender,  # "user" | "admin"
        "text": text,
        "username": username,
        "adminName": admin_name,
        "createdAt": now_ms,
        # Для сообщений от игрока — флаг, что админ ещё не увидел/не получил уведомление в боте.
        "adminNotifiedAt": None if sender == "user" else now_ms,
    }
    _, ref = _db.collection("chat_messages").add(data)
    return ref.id


async def add_chat_message(user_id: int, sender: str, text: str, username: str = None, admin_name: str = None) -> str:
    return await asyncio.to_thread(_add_chat_message_sync, user_id, sender, text, username, admin_name)


def _get_pending_chat_messages_sync() -> list:
    _ensure_init()
    if _db is None:
        return []
    from google.cloud.firestore_v1.base_query import FieldFilter
    result = []
    query = (_db.collection("chat_messages")
             .where(filter=FieldFilter("sender", "==", "user"))
             .where(filter=FieldFilter("adminNotifiedAt", "==", None)))
    for doc in query.stream():
        result.append({"id": doc.id, **(doc.to_dict() or {})})
    return result


async def get_pending_chat_messages() -> list:
    """Новые сообщения от игроков, о которых ещё не уведомили администрацию в боте."""
    return await asyncio.to_thread(_get_pending_chat_messages_sync)


def _mark_chat_message_notified_sync(message_id: str):
    _ensure_init()
    if _db is not None:
        _db.collection("chat_messages").document(message_id).update({"adminNotifiedAt": int(time.time() * 1000)})


async def mark_chat_message_notified(message_id: str):
    await asyncio.to_thread(_mark_chat_message_notified_sync, message_id)


# ══════════════════════════════════════════════════════════════════════════
# === ЗАЯВКИ НА ИГРОВЫЕ ЛИЦЕНЗИИ ===
# Коллекция "license_requests": заявка игрока на получение лицензии, подаётся
# из Mini App, обрабатывается администрацией (одобрить/отклонить) через бота.
# ══════════════════════════════════════════════════════════════════════════
def _get_pending_license_requests_sync() -> list:
    _ensure_init()
    if _db is None:
        return []
    from google.cloud.firestore_v1.base_query import FieldFilter
    result = []
    query = (_db.collection("license_requests")
             .where(filter=FieldFilter("status", "==", "pending"))
             .where(filter=FieldFilter("adminNotifiedAt", "==", None)))
    for doc in query.stream():
        result.append({"id": doc.id, **(doc.to_dict() or {})})
    return result


async def get_pending_license_requests() -> list:
    return await asyncio.to_thread(_get_pending_license_requests_sync)


def _mark_license_request_notified_sync(request_id: str):
    _ensure_init()
    if _db is not None:
        _db.collection("license_requests").document(request_id).update({"adminNotifiedAt": int(time.time() * 1000)})


async def mark_license_request_notified(request_id: str):
    await asyncio.to_thread(_mark_license_request_notified_sync, request_id)


def _get_license_request_sync(request_id: str) -> dict:
    _ensure_init()
    if _db is None:
        return {}
    snapshot = _db.collection("license_requests").document(request_id).get()
    return {"id": snapshot.id, **(snapshot.to_dict() or {})} if snapshot.exists else {}


async def get_license_request(request_id: str) -> dict:
    return await asyncio.to_thread(_get_license_request_sync, request_id)


def _resolve_license_request_sync(request_id: str, status: str, admin_comment: str = None) -> dict:
    _ensure_init()
    if _db is None:
        raise RuntimeError("Firestore не настроен или недоступен")
    ref = _db.collection("license_requests").document(request_id)
    snapshot = ref.get()
    data = snapshot.to_dict() or {}
    if data.get("status") != "pending":
        return data
    update = {"status": status, "resolvedAt": int(time.time() * 1000)}
    if admin_comment:
        update["adminComment"] = admin_comment
    ref.update(update)
    return data


async def resolve_license_request(request_id: str, status: str, admin_comment: str = None) -> dict:
    return await asyncio.to_thread(_resolve_license_request_sync, request_id, status, admin_comment)


# ══════════════════════════════════════════════════════════════════════════
# === ТАРИФЫ (управляются из Админ-панели в Mini App, коллекция "tariffs") ===
# Единый источник правды и для бота, и для Mini App — если коллекция пуста,
# вызывающий код (main.py) сам подставляет резервные тарифы из config.py.
# ══════════════════════════════════════════════════════════════════════════
def _get_tariffs_sync() -> dict:
    _ensure_init()
    if _db is None:
        return {}
    result = {}
    for doc in _db.collection("tariffs").stream():
        data = doc.to_dict() or {}
        if data.get("active", True):
            result[doc.id] = {"label": data.get("label", doc.id), "days": int(data.get("days", 0)), "price": int(data.get("price", 0))}
    return result


async def get_tariffs() -> dict:
    return await asyncio.to_thread(_get_tariffs_sync)


# ══════════════════════════════════════════════════════════════════════════
# === ОБЩИЕ НАСТРОЙКИ ПРИЛОЖЕНИЯ (админ-панель → раздел «Настройки») ===
# ══════════════════════════════════════════════════════════════════════════
def _get_app_settings_sync() -> dict:
    _ensure_init()
    if _db is None:
        return {}
    snapshot = _db.collection("app_settings").document("general").get()
    return snapshot.to_dict() or {} if snapshot.exists else {}


async def get_app_settings() -> dict:
    return await asyncio.to_thread(_get_app_settings_sync)
