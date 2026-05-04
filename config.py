import os
import secrets
from dotenv import load_dotenv

load_dotenv()

# Directory where this config file (and thus the whole app) lives
_APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Session timeout — inactivity longer than this starts a new session
SESSION_TIMEOUT_MINUTES: int = int(os.getenv("SESSION_TIMEOUT_MINUTES", "5"))

# Dashboard
DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")
# Flask secret key for sessions (auto-generated if not set, but won't survive restarts)
SECRET_KEY: str = os.getenv("SECRET_KEY", secrets.token_hex(32))
# SQLite database file for dialog history — defaults to <app_dir>/dialogs.db
DB_PATH: str = os.getenv("DB_PATH", os.path.join(_APP_DIR, "dialogs.db"))

# Web search via Anthropic's built-in tool
# WARNING: additional cost ~$10 per 1000 searches (separate from token cost)
ENABLE_WEB_SEARCH: bool = os.getenv("ENABLE_WEB_SEARCH", "false").lower() == "true"
# Max number of web search results per query (1-5, default 3)
WEB_SEARCH_MAX_RESULTS: int = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "3"))

# Anthropic / Claude settings
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")
CLAUDE_MAX_TOKENS: int = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))

# User location injected into system prompt (city, country or any description)
USER_LOCATION: str = os.getenv("USER_LOCATION", "")

# Timezone name for current date/time display (e.g. "Europe/Moscow")
USER_TIMEZONE: str = os.getenv("USER_TIMEZONE", "Europe/Moscow")

# System prompt base (date/time and location are appended dynamically at request time)
CLAUDE_SYSTEM_PROMPT_BASE: str = os.getenv(
    "CLAUDE_SYSTEM_PROMPT",
    (
        "Ты голосовой ИИ-ассистент внутри навыка Яндекс Алисы. "
        "Отвечай кратко, по-русски, без markdown-разметки, "
        "без эмодзи и специальных символов — только чистый текст, "
        "пригодный для синтеза речи. "
        "Максимум — три-четыре предложения, если не попросят подробнее."
    ),
)

# Keep backward compat alias
CLAUDE_SYSTEM_PROMPT: str = CLAUDE_SYSTEM_PROMPT_BASE

# Conversation history
MAX_HISTORY_TURNS: int = int(os.getenv("MAX_HISTORY_TURNS", "20"))  # user+assistant pairs

# Flask
# How long (seconds) to wait for Claude before returning "thinking" to Alice.
# Keep well below Alice's webhook timeout (~5 s). Claude calls typically take
# 2–8 s; set lower to be safe, user can ask "что ответил" for slower replies.
ALICE_REPLY_TIMEOUT: float = float(os.getenv("ALICE_REPLY_TIMEOUT", "3.0"))

FLASK_HOST: str = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT: int = int(os.getenv("FLASK_PORT", "37842"))
FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "false").lower() == "true"

# Optional: shared secret to verify Alice requests
ALICE_SKILL_TOKEN: str = os.getenv("ALICE_SKILL_TOKEN", "")
