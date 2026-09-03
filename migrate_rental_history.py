"""One-time backfill of the normalized rental history from tents.payments[]."""
import asyncio
import time

import firestore_sync


def payment_type(note):
    text = str(note or "").lower()
    return "renew" if "продлен" in text or "продлени" in text else "rent"


async def main():
    existing = await firestore_sync.list_bot_documents("rental_history")
    existing_ids = {item.get("id") for item in existing}
    tents = await firestore_sync.list_bot_documents("tents")
    imported = 0

    for tent in tents:
        tent_num = int(tent.get("tentNum") or 0)
        for index, payment in enumerate(tent.get("payments") or []):
            amount = int(payment.get("amount") or 0)
            if amount <= 0:
                continue
            payment_date = int(payment.get("date") or 0)
            history_id = f"legacy_tent_{tent_num}_{payment_date}_{index}"
            if history_id in existing_ids:
                continue
            await firestore_sync.upsert_bot_document("rental_history", history_id, {
                "tentNum": tent_num,
                "player": payment.get("player") or tent.get("player") or "",
                "tgId": tent.get("tgId"),
                "amount": amount,
                "days": int(payment.get("days") or 0),
                "endDate": payment.get("endDate") or tent.get("endDate") or "",
                "operationType": payment_type(payment.get("note")),
                "operationAt": payment_date or int(time.time() * 1000),
                "source": "import",
                "note": payment.get("note") or "Исторический платёж",
            })
            imported += 1

    print(f"Imported {imported} payments into rental_history")


if __name__ == "__main__":
    asyncio.run(main())
