BOT_TOKEN = "8737106748:AAFf4YttEn59vmBiPn53MtTsTVgbOK-O--Y"
ADMIN_ID = 1  # Твой Telegram ID (Полные права)
VIEWER_ID = 1  # Telegram ID смотрящего (Только статистика)
SUPER_ADMIN_ID = 5857451420       # Ваш ID (Главный админ)
TAX_OFFICER_IDS = [511767566]    # ID Президента / Налоговой (доступ к Excel)
MODERATOR_IDS = []     # ID Помощников (выдача/продление палаток)

# ID канала для логов (должен начинаться с -100)
LOG_CHANNEL_ID = -1003954071413 

TARIFFS = {
    "1week": {"label": "1 нед. (7д) — 50 🛢️", "days": 7, "price": 50},
    "2weeks": {"label": "2 нед. (14д) — 95 🛢️", "days": 14, "price": 95},
    "3weeks": {"label": "3 нед. (21д) — 140 🛢️", "days": 21, "price": 140},
    "1month": {
        "label": "1 мес. (30д) — 185 🛢️ 🔥",
        "days": 30,
        "price": 185,
    },
}