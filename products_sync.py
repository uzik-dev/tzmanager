import asyncio
import logging
import re
from datetime import datetime

import gspread

import firestore_sync
from config import (
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_SHEETS_PRODUCTS_SYNC_INTERVAL_MINUTES,
    GOOGLE_SHEETS_SHOPS_WORKSHEET,
    GOOGLE_SHEETS_SPREADSHEET_ID,
)

PRODUCT_HEADERS = ["Название товара", "Кол-во", "Цена", "Наличие"]
_client = None
_spreadsheet = None


def _get_spreadsheet():
    global _client, _spreadsheet
    if _spreadsheet is None:
        _client = gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_FILE)
        _spreadsheet = _client.open_by_key(GOOGLE_SHEETS_SPREADSHEET_ID)
    return _spreadsheet


def _normal(value):
    return str(value or "").strip()


def _safe_id(sheet_name, row, group):
    return "sheet_{}_{}_{}".format(re.sub(r"[^a-zA-Z0-9_-]", "_", sheet_name), row, group)


def _read_sources_sync():
    spreadsheet = _get_spreadsheet()
    registry = spreadsheet.worksheet(GOOGLE_SHEETS_SHOPS_WORKSHEET)
    rows = registry.get_all_values()
    sources = []
    in_tents_table = False
    for row in rows:
        first = _normal(row[0] if row else "")
        second = _normal(row[1] if len(row) > 1 else "")
        if first == "№" and "Номер палатки" in second:
            in_tents_table = True
            continue
        if not in_tents_table:
            continue
        if not any(_normal(value) for value in row):
            break
        tent_number = int(first) if first.isdigit() else None
        owner = _normal(row[2] if len(row) > 2 else "")
        sheet_name = _normal(row[5] if len(row) > 5 else "")
        if tent_number and sheet_name:
            sources.append((tent_number, owner, sheet_name))
    return sources


def _read_products_sync(sheet_name):
    worksheet = _get_spreadsheet().worksheet(sheet_name)
    rows = worksheet.get_all_values()
    result = []
    for row_number, row in enumerate(rows[1:], start=2):
        for group in range(0, len(row), 4):
            name = _normal(row[group] if group < len(row) else "")
            quantity = _normal(row[group + 1] if group + 1 < len(row) else "")
            price = _normal(row[group + 2] if group + 2 < len(row) else "")
            available = _normal(row[group + 3] if group + 3 < len(row) else "")
            if not name:
                continue
            result.append({
                "id": _safe_id(sheet_name, row_number, group // 4),
                "sourceSheet": sheet_name,
                "sourceRow": row_number,
                "sourceGroup": group // 4,
                "name": name,
                "quantity": quantity,
                "price": price,
                "available": available.upper() in {"TRUE", "ДА", "YES", "1"},
            })
    return result


def _write_product_sync(product):
    sheet_name = product.get("sourceSheet")
    row = int(product.get("sourceRow", 0))
    group = int(product.get("sourceGroup", 0))
    if not sheet_name:
        return
    worksheet = _get_spreadsheet().worksheet(sheet_name)
    start = group * 4
    values = [[product.get("name", ""), product.get("quantity", ""), product.get("price", ""), "TRUE" if product.get("available") else "FALSE"]]
    if row <= 0:
        worksheet.append_row(values[0])
        return len(worksheet.get_all_values())
    worksheet.update(f"{gspread.utils.rowcol_to_a1(row, start + 1)}:{gspread.utils.rowcol_to_a1(row, start + 4)}", values)
    return row


def _batch_write_products_sync(sheet_name, products):
    worksheet = _get_spreadsheet().worksheet(sheet_name)
    updates = []
    for product in products:
        row = int(product.get("sourceRow", 0))
        if row <= 0:
            continue
        start = int(product.get("sourceGroup", 0)) * 4
        updates.append({
            "range": f"{gspread.utils.rowcol_to_a1(row, start + 1)}:{gspread.utils.rowcol_to_a1(row, start + 4)}",
            "values": [[product.get("name", ""), product.get("quantity", ""), product.get("price", ""), "TRUE" if product.get("available") else "FALSE"]],
        })
    if updates:
        worksheet.batch_update(updates)


def _sync_sync(firestore_products: dict):
    """Синхронная часть (работа с Google Sheets через gspread, которая не умеет в
    asyncio) — только читает/пишет таблицы и решает, какие Firestore-операции
    нужны. Сами Firestore-операции здесь больше не выполняются (раньше на каждый
    товар вызывался отдельный asyncio.run(...), то есть создавался и уничтожался
    новый event loop десятки раз за один проход синхронизации — теперь все
    накопленные операции выполняются одним await в sync_products())."""
    if not GOOGLE_SHEETS_SPREADSHEET_ID or not GOOGLE_SERVICE_ACCOUNT_FILE:
        return {"sources": 0, "products": 0, "ops": []}

    ops = []  # список (op_name, args) — выполняется асинхронно одним махом позже
    changed = 0
    source_count = 0
    pending_writes = {}

    for tent_id, owner, sheet_name in _read_sources_sync():
        source_count += 1
        try:
            source_products = _read_products_sync(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            logging.warning("Лист прайса не найден: %s", sheet_name)
            continue
        for product in source_products:
            product["tentNum"] = tent_id or None
            product["ownerNickname"] = owner
            product["sourceUpdatedAt"] = int(datetime.now().timestamp() * 1000)
            product["lastSheetSyncAt"] = product["sourceUpdatedAt"]
            existing = firestore_products.get(product["id"])
            if existing and existing.get("updatedAt", 0) > existing.get("lastSheetSyncAt", 0):
                pending_writes.setdefault(existing.get("sourceSheet"), []).append(existing)
                product.update({k: existing.get(k, product[k]) for k in ("name", "quantity", "price", "available")})
            elif not existing or any(existing.get(key) != product.get(key) for key in ("name", "quantity", "price", "available", "tentNum")):
                ops.append(("sync_product_from_sheet", product["id"], dict(product)))
                changed += 1

    for sheet_name, products in pending_writes.items():
        write_succeeded = True
        try:
            _batch_write_products_sync(sheet_name, products)
        except gspread.exceptions.APIError as error:
            logging.warning("Не удалось записать изменения в защищённый лист %s: %s", sheet_name, error)
            write_succeeded = False
        if write_succeeded:
            for product in products:
                ops.append(("mark_product_sheet_synced", product["id"], product.get("updatedAt", int(datetime.now().timestamp() * 1000))))

    for product in firestore_products.values():
        if product.get("sourceRow") or not product.get("sourceSheet"):
            continue
        row = _write_product_sync(product)
        if row:
            ops.append(("upsert_product", product["id"], {"sourceRow": row, "sourceGroup": 0}))
            ops.append(("mark_product_sheet_synced", product["id"], product.get("updatedAt", int(datetime.now().timestamp() * 1000))))
            changed += 1

    return {"sources": source_count, "products": changed, "ops": ops}


async def _apply_ops(ops):
    for op in ops:
        try:
            if op[0] == "sync_product_from_sheet":
                _, product_id, product = op
                await firestore_sync.sync_product_from_sheet(product_id, product)
            elif op[0] == "mark_product_sheet_synced":
                _, product_id, updated_at = op
                await firestore_sync.mark_product_sheet_synced(product_id, updated_at)
            elif op[0] == "upsert_product":
                _, product_id, data = op
                await firestore_sync.upsert_product(product_id, data)
        except Exception:
            logging.exception("Ошибка применения Firestore-операции %s для товара", op[0])


async def sync_products():
    try:
        firestore_products = {product["id"]: product for product in await firestore_sync.get_all_products()}
        result = await asyncio.to_thread(_sync_sync, firestore_products)
        await _apply_ops(result["ops"])
        logging.info("Синхронизация товаров: листов %s, изменений %s", result["sources"], result["products"])
    except Exception:
        logging.exception("Ошибка синхронизации товаров с Google Sheets")


def sync_interval_minutes():
    return GOOGLE_SHEETS_PRODUCTS_SYNC_INTERVAL_MINUTES
