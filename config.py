import os

from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Конфигурация (берётся из .env, см. .env.example)
# ----------------------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", r".\ffmpeg\bin\ffmpeg.exe")
GUILD_ID = os.getenv("GUILD_ID")  # для мгновенной синхронизации команд

# Telegram-управление (опционально)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "")  # Telegram ID (можно через запятую)
VOICE_CHANNEL_ID = (
    int(os.getenv("VOICE_CHANNEL_ID")) if os.getenv("VOICE_CHANNEL_ID") else None
)
ALLOWED_TG_USERS = {
    int(x) for x in TELEGRAM_ADMIN_ID.split(",") if x.strip().isdigit()
}

if not TOKEN or not YANDEX_TOKEN:
    raise RuntimeError(
        "Не заданы DISCORD_TOKEN и/или YANDEX_TOKEN. "
        "Создай файл .env по образцу .env.example."
    )

# Опции FFmpeg для потокового HTTP-источника: переподключение при обрыве
FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    ),
    "options": "-vn",
}

# Слова, за которые кикаем автора сообщения — можно через запятую
KICK_TRIGGERS = ("банворд", "банворд")

# Слова для голосового кика (UPD: не работает из-за обновления Discord — 2026)
VOICE_TRIGGERS = ("слово", "слово")

# Путь к распакованной модели Vosk (UPD: не работает из-за обновления Discord — 2026)
VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "model")
