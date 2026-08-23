import asyncio
import sqlite3
import time

import firestore_sync


COLLECTIONS = {
    "users": "bot_users",
    "payments_history": "bot_payments",
    "pending_requests": "bot_pending_requests",
    "blacklist": "bot_blacklist",
    "sent_reminders": "bot_sent_reminders",
}


def read_rows():
    connection = sqlite3.connect("tents.db")
    connection.row_factory = sqlite3.Row
    result = {}
    for table in COLLECTIONS:
        result[table] = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()]
    result["stats_corrections"] = dict(connection.execute("SELECT * FROM stats_corrections WHERE id = 1").fetchone())
    connection.close()
    return result


async def migrate():
    rows = read_rows()
    report = {}
    for table, documents in ((name, values) for name, values in rows.items() if name != "stats_corrections"):
        collection = COLLECTIONS[table]
        migrated = 0
        for row in documents:
            if table == "users" or table == "blacklist":
                document_id = str(row["tg_id"])
            elif table == "pending_requests" or table == "payments_history":
                document_id = str(row["id"])
            else:
                document_id = f'{row["tent_id"]}_{row["milestone"]}_{row["end_date"]}'
            payload = {key: value for key, value in row.items()}
            payload["migratedAt"] = int(time.time() * 1000)
            await firestore_sync.upsert_bot_document(collection, document_id, payload)
            migrated += 1
        report[table] = migrated

    await firestore_sync.upsert_bot_document(
        "bot_settings",
        "stats_corrections",
        {**rows["stats_corrections"], "migratedAt": int(time.time() * 1000)},
    )
    report["stats_corrections"] = 1
    print("MIGRATION_REPORT", report)


if __name__ == "__main__":
    asyncio.run(migrate())
