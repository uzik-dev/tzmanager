import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # подхватывает .env из корня проекта, если он есть
except ImportError:
    pass

# === СЕКРЕТЫ ===
# ВАЖНО: токен бота больше НЕ хранится в коде как значение по умолчанию — раньше
# здесь лежал настоящий рабочий BOT_TOKEN в открытом виде, и любой, кто получил бы
# этот файл (архив, репозиторий и т.п.), мог полностью управлять ботом от вашего
# имени. Токен нужно задавать только через переменную окружения BOT_TOKEN
# (например, через файл .env — см. .env.example).
# Если старый токен "8906417977:AAHLn8ufnCzszs0GgjfkgIuuSsIpDT9uftw" ещё нигде не
# отозван — обязательно отзовите его через @BotFather → /revoke и выпустите новый.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан! Укажите его в переменной окружения BOT_TOKEN "
        "(например, в файле .env — см. .env.example) и перезапустите бота."
    )

ADMIN_ID = int(os.environ.get("ADMIN_ID"))
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID"))
TAX_OFFICER_IDS = [int(x) for x in os.environ.get("TAX_OFFICER_IDS").split(",") if x.strip()]
MODERATOR_IDS = [int(x) for x in os.environ.get("MODERATOR_IDS", "").split(",") if x.strip()]  # ID Помощников (выдача/продление палаток)

# Ставка налога с валового сбора для отчёта Налоговой Инспекции
TAX_RATE = float(os.environ.get("TAX_RATE",))

GOOGLE_SHEETS_SPREADSHEET_ID = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")   # ID таблицы-реестра
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
GOOGLE_SHEETS_TENTS_WORKSHEET = os.environ.get("GOOGLE_SHEETS_TENTS_WORKSHEET")
GOOGLE_SHEETS_SHOPS_WORKSHEET = os.environ.get("GOOGLE_SHEETS_SHOPS_WORKSHEET",)
GOOGLE_SHEETS_PRODUCTS_SYNC_INTERVAL_MINUTES = int(os.environ.get("GOOGLE_SHEETS_PRODUCTS_SYNC_INTERVAL_MINUTES", "5"))

MINIAPP_URL = os.environ.get("MINIAPP_URL",)

FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE",)

# ID канала для логов (должен начинаться с -100)
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID",))

# ID "зрителя" статистики (доступ только к /stats, без прав администратора).
# Раньше был placeholder-значением "1" (несуществующий Telegram ID) — из-за этого
# функция была фактически мёртвым кодом. Если зритель не нужен — оставьте пустым (0).
VIEWER_ID = int(os.environ.get("VIEWER_ID",))

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
