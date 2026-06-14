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


def _parse_csv(value: str) -> list[str]:
    if not value:
        return []
    return [chunk.strip() for chunk in value.split(",") if chunk.strip()]


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID") or 0) or None

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_ALLOW_EMPTY_API_KEY = os.getenv("OPENROUTER_ALLOW_EMPTY_API_KEY", "0").strip().lower() in ("1", "true", "yes")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash").strip()
OPENROUTER_IMAGE_MODEL = os.getenv("OPENROUTER_IMAGE_MODEL", "black-forest-labs/flux.2-klein-4b").strip()
OPENROUTER_TRANSLATE_MODEL = os.getenv("OPENROUTER_TRANSLATE_MODEL", "mistralai/ministral-14b-2512").strip()
IMAGE_GEN_LIMIT_PER_DAY = int(os.getenv("IMAGE_GEN_LIMIT_PER_DAY", "5"))
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "").strip()
GIVEAWAY_CHANNEL_ID = int(os.getenv("GIVEAWAY_CHANNEL_ID") or 1390391884245106758)
NEWS_CHECK_INTERVAL_HOURS = float(os.getenv("NEWS_CHECK_INTERVAL_HOURS", "4"))
OPENROUTER_FALLBACK_MODELS: list[str] = _parse_csv(os.getenv("OPENROUTER_FALLBACK_MODELS", ""))

ACTIVE_CHANNELS: set[int] = _parse_ids(os.getenv("ACTIVE_CHANNELS", ""))
ALLOWED_ROLES: set[int] = _parse_ids(os.getenv("ALLOWED_ROLES", ""))
ADMIN_ROLES: set[int] = _parse_ids(os.getenv("ADMIN_ROLES", ""))

HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "600"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.9"))
STREAM_EDIT_INTERVAL = float(os.getenv("STREAM_EDIT_INTERVAL", "1.2"))

# History storage
MAX_HISTORY_CHARS = int(os.getenv("MAX_HISTORY_CHARS", "4000"))
HISTORY_TTL = int(os.getenv("HISTORY_TTL", "86400"))
HISTORY_FILE = os.getenv("HISTORY_FILE", "history.json")

# Channel context injected into system prompt
CHANNEL_CONTEXT_LIMIT = int(os.getenv("CHANNEL_CONTEXT_LIMIT", "6"))

RANDOM_PING_CHANNEL = int(os.getenv("RANDOM_PING_CHANNEL") or 0)
RANDOM_PING_ROLE = int(os.getenv("RANDOM_PING_ROLE") or 0)
RANDOM_PING_INTERVAL_HOURS = float(os.getenv("RANDOM_PING_INTERVAL_HOURS", "12"))
BLURT_CHANNEL = int(os.getenv("BLURT_CHANNEL") or 0)
BLURT_INTERVAL_MIN_HOURS = float(os.getenv("BLURT_INTERVAL_MIN_HOURS", "2"))
BLURT_INTERVAL_MAX_HOURS = float(os.getenv("BLURT_INTERVAL_MAX_HOURS", "5"))
BLURT_QUIET_MINUTES = float(os.getenv("BLURT_QUIET_MINUTES", "30"))
RITUAL_CHANNEL = int(os.getenv("RITUAL_CHANNEL") or 0)
RITUAL_MORNING_HOUR = int(os.getenv("RITUAL_MORNING_HOUR", "9"))
RITUAL_NIGHT_HOUR = int(os.getenv("RITUAL_NIGHT_HOUR", "0"))
SPAR_DURATION_MINUTES = int(os.getenv("SPAR_DURATION_MINUTES", "10"))
USER_COOLDOWN = int(os.getenv("USER_COOLDOWN", "8"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "10"))
MAX_PROMPT_LEN = int(os.getenv("MAX_PROMPT_LEN", "3000"))
MENTION_REACT_CHANCE = float(os.getenv("MENTION_REACT_CHANCE", "0.3"))
EMOJI_REACT_CHANCE = float(os.getenv("EMOJI_REACT_CHANCE", "0.25"))
GROUP_DEBOUNCE_SECONDS = float(os.getenv("GROUP_DEBOUNCE_SECONDS", "6"))
CHANNEL_BUSY_THRESHOLD = int(os.getenv("CHANNEL_BUSY_THRESHOLD", "15"))
FLOOD_LIMIT_PER_HOUR = int(os.getenv("FLOOD_LIMIT_PER_HOUR", "30"))
AUTO_FACTS_CHANCE = float(os.getenv("AUTO_FACTS_CHANCE", "0.5"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "120"))
OPENROUTER_REQUEST_TIMEOUT = int(os.getenv("OPENROUTER_REQUEST_TIMEOUT", "60"))

CHRONICLE_CHANNEL = int(os.getenv("CHRONICLE_CHANNEL") or 0)
CHRONICLE_INTERVAL_HOURS = float(os.getenv("CHRONICLE_INTERVAL_HOURS", "24"))
CHRONICLE_LOOKBACK_DAYS = int(os.getenv("CHRONICLE_LOOKBACK_DAYS", "7"))

DISCORD_MSG_LIMIT = 2000

# Баланс: порог (в кредитах) при котором показываем предупреждение в /quota
OPENROUTER_LOW_BALANCE_THRESHOLD = int(os.getenv("OPENROUTER_LOW_BALANCE_THRESHOLD", "10"))


if not DISCORD_TOKEN or DISCORD_TOKEN.startswith("PASTE"):
    raise SystemExit(
        "DISCORD_TOKEN не задан. Скопируй .env.example в .env и впиши свой токен."
    )

if not OPENROUTER_API_KEY and not OPENROUTER_ALLOW_EMPTY_API_KEY:
    raise SystemExit(
        "OPENROUTER_API_KEY не задан. Получи ключ на https://openrouter.ai/keys и впиши в .env, или включи OPENROUTER_ALLOW_EMPTY_API_KEY=1 для локального OpenAI-совместимого сервера."
    )
if OPENROUTER_API_KEY.startswith("PASTE"):
    raise SystemExit(
        "OPENROUTER_API_KEY содержит заглушку. Замени на реальный ключ или включи OPENROUTER_ALLOW_EMPTY_API_KEY=1 для локального сервера."
    )
