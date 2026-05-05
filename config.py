"""Загрузка конфигурации из .env."""
import os
from dotenv import load_dotenv

load_dotenv()


def _parse_ids(value: str) -> set[int]:
    if not value:
        return set()
    out = set()
    for chunk in value.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.add(int(chunk))
    return out


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID") or 0) or None

LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").strip()
LMSTUDIO_API_KEY = os.getenv("LMSTUDIO_API_KEY", "lm-studio").strip()
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "openai/gpt-oss-20b").strip()

ACTIVE_CHANNELS: set[int] = _parse_ids(os.getenv("ACTIVE_CHANNELS", ""))
ALLOWED_ROLES: set[int] = _parse_ids(os.getenv("ALLOWED_ROLES", ""))

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "40"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "900"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.65"))
STREAM_EDIT_INTERVAL = float(os.getenv("STREAM_EDIT_INTERVAL", "1.2"))

# Антиспам
USER_COOLDOWN = float(os.getenv("USER_COOLDOWN", "8"))          # сек между запросами одного юзера
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "10"))         # макс размер очереди
MAX_PROMPT_LEN = int(os.getenv("MAX_PROMPT_LEN", "500"))        # макс длина запроса

# Автопинг рандомного пользователя
RANDOM_PING_CHANNEL = int(os.getenv("RANDOM_PING_CHANNEL") or 0) or None
RANDOM_PING_ROLE = int(os.getenv("RANDOM_PING_ROLE") or 0) or None
RANDOM_PING_INTERVAL_HOURS = float(os.getenv("RANDOM_PING_INTERVAL_HOURS", "12"))

# Случайные вбросы в чат (бот сам пишет фразу-провокацию без пинга)
BLURT_CHANNEL = int(os.getenv("BLURT_CHANNEL") or 0) or None
BLURT_INTERVAL_MIN_HOURS = float(os.getenv("BLURT_INTERVAL_MIN_HOURS", "2"))
BLURT_INTERVAL_MAX_HOURS = float(os.getenv("BLURT_INTERVAL_MAX_HOURS", "5"))
# Бот не вбрасывает если в канале только что писали (минут)
BLURT_QUIET_MINUTES = int(os.getenv("BLURT_QUIET_MINUTES", "30"))

# Контекст канала: сколько последних сообщений канала добавлять в промпт
CHANNEL_CONTEXT_LIMIT = int(os.getenv("CHANNEL_CONTEXT_LIMIT", "6"))

# Реакция на упоминания других людей (вероятность 0.0-1.0)
MENTION_REACT_CHANCE = float(os.getenv("MENTION_REACT_CHANCE", "0.3"))

# /spar — длительность сессии травли в минутах
SPAR_DURATION_MINUTES = int(os.getenv("SPAR_DURATION_MINUTES", "10"))

# ====== Новые улучшения ======
# Эмодзи-реакции на короткие сообщения
EMOJI_REACT_CHANCE = float(os.getenv("EMOJI_REACT_CHANCE", "0.25"))
# Группировка коротких сообщений: ждём N секунд после первого, если юзер пишет ещё
GROUP_DEBOUNCE_SECONDS = float(os.getenv("GROUP_DEBOUNCE_SECONDS", "6"))
# Лимит активности канала: если в чате > N сообщений за минуту — бот молчит (кроме @упоминаний)
CHANNEL_BUSY_THRESHOLD = int(os.getenv("CHANNEL_BUSY_THRESHOLD", "15"))
# Anti-flood: лимит запросов на юзера в час
FLOOD_LIMIT_PER_HOUR = int(os.getenv("FLOOD_LIMIT_PER_HOUR", "30"))
# Health-check LM Studio: интервал в секундах
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "120"))
# Auto-facts: вероятность что после ответа извлекутся факты
AUTO_FACTS_CHANCE = float(os.getenv("AUTO_FACTS_CHANCE", "0.5"))
# Утренний/ночной ритуал
RITUAL_CHANNEL = int(os.getenv("RITUAL_CHANNEL") or 0) or None
RITUAL_MORNING_HOUR = int(os.getenv("RITUAL_MORNING_HOUR", "9"))
RITUAL_NIGHT_HOUR = int(os.getenv("RITUAL_NIGHT_HOUR", "0"))

# Погода для вбросов (город в формате wttr.in, например Moscow или Novosibirsk)
WEATHER_CITY = os.getenv("WEATHER_CITY", "Moscow").strip()

DISCORD_MSG_LIMIT = 2000


if not DISCORD_TOKEN or DISCORD_TOKEN.startswith("PASTE"):
    raise SystemExit(
        "DISCORD_TOKEN не задан. Скопируй .env.example в .env и впиши свой токен."
    )
