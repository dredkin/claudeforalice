"""Thin wrapper around the Anthropic SDK for synchronous calls."""
from __future__ import annotations

import logging
from typing import List, Dict

import anthropic

import config

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# ── Human-readable error messages (spoken by Alice) ───────────────────────
_ERROR_MESSAGES = {
    "auth":        "Ошибка авторизации в сервисе ИИ. Проверьте правильность API-ключа Anthropic в настройках навыка.",
    "billing":     "На счёте сервиса ИИ закончились средства. Пополните баланс на сайте Anthropic.",
    "rate_limit":  "Сервис ИИ временно перегружен запросами. Подождите немного и попробуйте снова.",
    "overload":    "Серверы Anthropic сейчас перегружены. Попробуйте повторить запрос через несколько секунд.",
    "not_found":   "Указанная модель ИИ не найдена. Проверьте значение CLAUDE_MODEL в настройках.",
    "bad_request": "Запрос к ИИ сформирован неверно. Обратитесь к разработчику навыка.",
    "timeout":     "Сервис ИИ не ответил вовремя. Попробуйте повторить запрос.",
    "connection":  "Не удалось подключиться к сервису ИИ. Проверьте интернет-соединение сервера.",
    "generic":     "Произошла неизвестная ошибка при обращении к ИИ. Попробуй ещё раз позже.",
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

    try:
        response = _client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            system=config.CLAUDE_SYSTEM_PROMPT,
            messages=messages,
        )
        return response.content[0].text

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
