"""
Parse an incoming Yandex Alice webhook request and build the response dict.

Alice protocol reference:
  https://yandex.ru/dev/dialogs/alice/doc/request.html
  https://yandex.ru/dev/dialogs/alice/doc/response.html
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import claude_client
import session_manager

logger = logging.getLogger(__name__)

# Phrases that reset the conversation history
RESET_PHRASES = {"сбрось историю", "новый диалог", "начни сначала", "очисти историю"}

# What we say on the very first launch
WELCOME_TEXT = (
    "Привет! Я Алиса с интеллектом Claude. "
    "Задай любой вопрос — и я отвечу."
)

RESET_TEXT = "История диалога очищена. Начинаем заново!"
END_TEXT = "До свидания!"


def _get_user_id(event: Dict[str, Any]) -> Optional[str]:
    """Extract the persistent user ID from the Alice event."""
    try:
        return event["session"]["user"]["user_id"]
    except KeyError:
        # Unauthorised / anonymous users fall back to session_id
        return event.get("session", {}).get("session_id")


def _is_new_session(event: Dict[str, Any]) -> bool:
    return bool(event.get("session", {}).get("new", False))


def _get_utterance(event: Dict[str, Any]) -> str:
    """Return the normalised user utterance (lower-cased, stripped)."""
    return (
        event.get("request", {}).get("original_utterance", "")
        .strip()
        .lower()
    )


def _build_response(
    text: str,
    end_session: bool = False,
    version: str = "1.0",
    session: Optional[Dict] = None,
) -> Dict[str, Any]:
    return {
        "version": version,
        "session": session or {},
        "response": {
            "text": text,
            "tts": text,           # TTS == text (no SSML)
            "end_session": end_session,
        },
    }


def handle(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entry point called by the Flask view.

    Parameters
    ----------
    event:
        Parsed JSON body of the Alice webhook POST request.

    Returns
    -------
    dict
        Alice-compatible response dict (will be JSON-serialised by Flask).
    """
    version = event.get("version", "1.0")
    session_meta = event.get("session", {})
    user_id = _get_user_id(event)

    utterance = _get_utterance(event)

    # ── New session ─────────────────────────────────────────────────────────
    # If the skill was launched with a phrase like
    # "Алиса, спроси клода, <вопрос>" the utterance arrives together with
    # the new-session flag — process it immediately instead of greeting.
    if _is_new_session(event) and not utterance:
        return _build_response(WELCOME_TEXT, version=version, session=session_meta)


    # ── Explicit exit commands ──────────────────────────────────────────────
    if utterance in {"стоп", "выход", "хватит", "закрыть"}:
        return _build_response(END_TEXT, end_session=True, version=version, session=session_meta)

    # ── History reset commands ──────────────────────────────────────────────
    if utterance in RESET_PHRASES:
        if user_id:
            session_manager.clear_history(user_id)
        return _build_response(RESET_TEXT, version=version, session=session_meta)

    # ── Normal turn: ask Claude ────────────────────────────────────────────
    history = session_manager.get_history(user_id) if user_id else []

    logger.info("user_id=%s utterance=%r history_len=%d", user_id, utterance, len(history))

    reply = claude_client.ask(history, utterance)

    if user_id:
        session_manager.append_turn(user_id, utterance, reply)

    return _build_response(reply, version=version, session=session_meta)
