"""Import the occupied tent snapshot supplied from the manager UI."""
import asyncio
import time

import firestore_sync


SITE_TENTS = {
    1: {"player": "Saitomina", "endDate": "2026-12-11", "amount": 400, "totalPaid": 1000},
    3: {"player": "somi_som", "endDate": "2026-09-15", "amount": 185, "totalPaid": 185},
    4: {"player": "rewetti", "endDate": "2026-09-05", "amount": 95, "totalPaid": 245},
    6: {"player": "Slaffneft", "endDate": "2026-09-14", "amount": 185, "totalPaid": 285},
    8: {"player": "Alexandra240413", "endDate": "2026-09-13", "amount": 95, "totalPaid": 240},
    13: {"player": "Kot_Jora228", "endDate": "2026-09-06", "amount": 95, "totalPaid": 195},
    15: {"player": "LLIuMA27_", "endDate": "2026-09-08", "amount": 50, "totalPaid": 280},
    18: {"player": "Tw1stPl4y", "endDate": "2026-09-12", "amount": 95, "totalPaid": 245},
    19: {"player": "prostosteve_", "endDate": "2026-09-13", "amount": 50, "totalPaid": 300},
    20: {"player": "AbuNeft", "endDate": "2026-09-27", "amount": 90, "totalPaid": 90},
}


async def main():
    existing = {int(row.get("tentNum")): row for row in await firestore_sync.list_bot_documents("tents")}
    imported = 0
    skipped = 0
    now_ms = int(time.time() * 1000)
    for tent_num, source in SITE_TENTS.items():
        if existing.get(tent_num, {}).get("occupied"):
            skipped += 1
            continue
        payment = {
            "amount": source["totalPaid"],
            "date": now_ms,
            "endDate": source["endDate"],
            "note": "Импорт из сайта: сумма за всё время",
            "player": source["player"],
        }
        await firestore_sync.upsert_bot_document("tents", f"site-import-{tent_num}", {
            "tentNum": tent_num,
            "occupied": True,
            "player": source["player"],
            "tgId": None,
            "endDate": source["endDate"],
            "note": "Импортировано с сайта, владелец не привязан",
            "amount": source["amount"],
            "payments": [payment],
            "source": "site_import",
            "createdAt": now_ms,
        })
        imported += 1
    print(f"Imported: {imported}; skipped existing occupied: {skipped}")


if __name__ == "__main__":
    asyncio.run(main())
