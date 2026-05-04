"""Thin wrapper around the Anthropic SDK for synchronous calls."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anthropic

import config
import geo_detect

logger = logging.getLogger(__name__)

# Shared singleton — imported by balance_client as well
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _build_system_prompt() -> str:
    """
    Build the system prompt for each request, injecting current date/time
    and user location so Claude can answer time/place-sensitive questions.

    Location and timezone come from config (set via .env). If not configured,
    they are auto-detected from the server's public IP on first call.
    """
    # Resolve location and timezone — prefer .env, fall back to auto-detect
    location = config.USER_LOCATION
    tz_name  = config.USER_TIMEZONE

    if not location or tz_name in ("", "Europe/Moscow"):
        # Only call geo if at least one value is missing / default
        auto_loc, auto_tz = geo_detect.detect()
        if not location:
            location = auto_loc
        if tz_name == "Europe/Moscow" and auto_tz and auto_tz != "UTC":
            tz_name = auto_tz

    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        tz = timezone.utc

    now = datetime.now(tz=tz)
    # Russian weekday names
    weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    weekday_ru = weekdays_ru[now.weekday()]
    date_str = now.strftime(f"%-d %B %Y года, {weekday_ru}, %H:%M")

    context_lines = [f"Текущая дата и время: {date_str}."]
    if location:
        context_lines.append(f"Местоположение пользователя: {location}.")

    context = " ".join(context_lines)
    return f"{config.CLAUDE_SYSTEM_PROMPT_BASE}\n\n{context}"

# ── Human-readable error messages (spoken by Alice) ───────────────────────
_ERROR_MESSAGES = {
    "auth":        "Клод не смог подключиться: неверный ключ доступа. Попросите владельца навыка проверить настройки.",
    "billing":     "На счёте Клода закончились деньги. Попросите владельца навыка пополнить баланс на сайте Anthropic.",
    "rate_limit":  "Клод сейчас очень занят и не успевает отвечать. Подождите немного и попробуйте снова.",
    "overload":    "Серверы Клода сейчас перегружены. Попробуйте повторить вопрос через несколько секунд.",
    "not_found":   "Выбранная версия Клода не найдена. Попросите владельца навыка проверить настройки.",
    "bad_request": "Клод не смог понять запрос. Попробуйте переформулировать вопрос.",
    "timeout":     "Клод думал слишком долго и не успел ответить. Попробуйте повторить вопрос.",
    "connection":  "Не удалось связаться с Клодом. Проверьте интернет-соединение и попробуйте позже.",
    "generic":     "Что-то пошло не так при обращении к Клоду. Попробуйте ещё раз чуть позже.",
}


def _classify_error(exc: Exception) -> str:
    """Return a user-friendly Russian message for the given exception."""
    if isinstance(exc, anthropic.AuthenticationError):
        logger.error("Anthropic auth error (bad API key?): %s", exc)
        return _ERROR_MESSAGES["auth"]

    if isinstance(exc, anthropic.PermissionDeniedError):
        # HTTP 403 — often means billing / account blocked
        logger.error("Anthropic permission denied (billing?): %s", exc)
        return _ERROR_MESSAGES["billing"]

    if isinstance(exc, anthropic.RateLimitError):
        logger.warning("Anthropic rate limit hit: %s", exc)
        return _ERROR_MESSAGES["rate_limit"]

    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        body = str(exc).lower()

        if status == 402 or "credit" in body or "billing" in body or "balance" in body:
            logger.error("Anthropic billing/credits error (HTTP %s): %s", status, exc)
            return _ERROR_MESSAGES["billing"]

        if status == 401:
            logger.error("Anthropic unauthorized (HTTP 401): %s", exc)
            return _ERROR_MESSAGES["auth"]

        if status == 404:
            logger.error("Anthropic model not found (HTTP 404): %s", exc)
            return _ERROR_MESSAGES["not_found"]

        if status == 400:
            logger.error("Anthropic bad request (HTTP 400): %s", exc)
            return _ERROR_MESSAGES["bad_request"]

        if status == 529 or "overloaded" in body:
            logger.warning("Anthropic overloaded (HTTP %s): %s", status, exc)
            return _ERROR_MESSAGES["overload"]

        if status and status >= 500:
            logger.error("Anthropic server error (HTTP %s): %s", status, exc)
            return _ERROR_MESSAGES["overload"]

        logger.error("Anthropic API status error (HTTP %s): %s", status, exc)
        return _ERROR_MESSAGES["generic"]

    if isinstance(exc, anthropic.APITimeoutError):
        logger.error("Anthropic request timed out: %s", exc)
        return _ERROR_MESSAGES["timeout"]

    if isinstance(exc, anthropic.APIConnectionError):
        logger.error("Anthropic connection error: %s", exc)
        return _ERROR_MESSAGES["connection"]

    logger.error("Unexpected error calling Anthropic: %s", exc, exc_info=True)
    return _ERROR_MESSAGES["generic"]


def ask(history: List[Dict[str, str]], user_text: str) -> str:
    """
    Send *user_text* to Claude, appending it to *history*, and return the
    assistant reply as plain text.

    Parameters
    ----------
    history:
        List of ``{"role": "user"|"assistant", "content": "..."}`` dicts
        representing the conversation so far (will NOT be mutated).
    user_text:
        The latest utterance from the Yandex Alice user.

    Returns
    -------
    str
        Plain-text reply from Claude (or a diagnostic error message).
    """
    messages: List[Dict[str, str]] = list(history) + [
        {"role": "user", "content": user_text}
    ]

    # Build optional tools list
    tools = []
    if config.ENABLE_WEB_SEARCH:
        tools.append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": config.WEB_SEARCH_MAX_RESULTS,
        })

    try:
        kwargs: Dict = dict(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=_build_system_prompt(),
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)

        # Extract text — may be mixed with tool_use blocks when search is on
        text_parts = [
            block.text for block in response.content
            if hasattr(block, "text") and block.text
        ]
        return " ".join(text_parts) if text_parts else _ERROR_MESSAGES["generic"]

    except (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.RateLimitError,
        anthropic.APIStatusError,
        anthropic.APITimeoutError,
        anthropic.APIConnectionError,
        anthropic.APIError,
    ) as exc:
        return _classify_error(exc)

    except Exception as exc:  # noqa: BLE001
        return _classify_error(exc)
