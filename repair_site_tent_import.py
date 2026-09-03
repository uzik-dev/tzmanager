"""Merge the site import documents into the existing tent documents."""
import asyncio

import firestore_sync


async def main():
    rows = await firestore_sync.list_bot_documents("tents")
    by_num = {}
    for row in rows:
        by_num.setdefault(int(row.get("tentNum") or 0), []).append(row)
    repaired = 0
    removed = 0
    for tent_num, items in by_num.items():
        imported = [item for item in items if item.get("id", "").startswith("site-import-")]
        originals = [item for item in items if not item.get("id", "").startswith("site-import-")]
        if not imported or not originals:
            continue
        source = imported[0]
        target = originals[0]
        await firestore_sync.upsert_bot_document("tents", target["id"], {
            key: value for key, value in source.items()
            if key not in {"id", "createdAt"}
        })
        for duplicate in imported:
            await firestore_sync.delete_bot_document("tents", duplicate["id"])
            removed += 1
        repaired += 1
    print(f"Repaired: {repaired}; duplicate imports removed: {removed}")


if __name__ == "__main__":
    asyncio.run(main())
