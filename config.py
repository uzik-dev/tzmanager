BOT_TOKEN = "8906417977:AAGNCwcryzXj6gpfeFLo-AiABlkbY6qOAPI"
ADMIN_ID = 5857451420
VIEWER_ID = 1  
SUPER_ADMIN_ID = 5857451420      
TAX_OFFICER_IDS = [511767566]    
MODERATOR_IDS = []     # ID Помощников (выдача/продление палаток) 

# Ставка налога с валового сбора для отчёта Налоговой Инспекции 
TAX_RATE = 0.10

GOOGLE_SHEETS_SPREADSHEET_ID = "1eppEzF1kYcTIZn7b2kf3mdc_GRQ9rAY1XJi6-t3vMT0"   # вставьте ID вашей таблицы-реестра сюда
GOOGLE_SERVICE_ACCOUNT_FILE = "tzmanager-21fab68a3686.json"
GOOGLE_SHEETS_TENTS_WORKSHEET = "Палатки"  
GOOGLE_SHEETS_SHOPS_WORKSHEET = "Реестр магазинов"
GOOGLE_SHEETS_PRODUCTS_SYNC_INTERVAL_MINUTES = 5


MINIAPP_URL = "https://tzprograma-e7b58.web.app"

FIREBASE_SERVICE_ACCOUNT_FILE = "tzprograma-e7b58-firebase-adminsdk-fbsvc-53328d989e.json"

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