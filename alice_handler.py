"""
Parse an incoming Yandex Alice webhook request and build the response dict.

Alice protocol reference:
  https://yandex.ru/dev/dialogs/alice/doc/request.html
  https://yandex.ru/dev/dialogs/alice/doc/response.html
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import balance_client
import claude_client
import config
import pending_store
import session_manager

logger = logging.getLogger(__name__)

# Phrases that check account/billing status
BALANCE_PHRASES = {
    "сколько денег", "сколько денег осталось", "сколько денег на счету",
    "проверь счёт", "проверь счет", "состояние счёта", "состояние счета",
    "баланс", "проверь баланс", "сколько осталось денег",
    "деньги на счету", "хватит ли денег", "есть ли деньги",
    "работает ли клод", "клод работает", "проверь клода",
}

# Phrases that reset the conversation history
RESET_PHRASES = {"сбрось историю", "новый диалог", "начни сначала", "очисти историю"}

# Phrases that trigger a repeat of the last reply (no Claude call)
REPEAT_PHRASES = {
    "повтори", "повтори пожалуйста", "повтори ещё раз", "повтори еще раз",
    "ещё раз", "еще раз", "не расслышал", "не расслышала", "не слышно",
    "громче", "скажи ещё раз", "скажи еще раз", "можешь повторить",
    "что ты сказал", "что ты сказала", "повтори ответ",
}

# Phrases that ask for a deferred answer
READY_PHRASES = {
    "что ответил", "что ты ответил", "готово", "ты готов", "ты закончил",
    "клод ответил", "есть ответ", "что там", "ну как","ответ есть", "ответ готов",
    "говори", "давай", "слушаю", "а сейчас готов", "а сейчас", "уже готов", "уже готово",
    "ну когда уже", "ну когда","готов","когда уже","когда"
}

# What we say on the very first launch
WELCOME_TEXT = (
    "Привет! Я Алиса с интеллектом Клода. "
    "Задай любой вопрос — и я отвечу."
)

THINKING_TEXT = (
    "Клод думает над ответом. "
    "Спросите меня через несколько секунд: ответ готов?"
)
STILL_THINKING_TEXT = (
    "Клод ещё думает. Подождите ещё немного и спросите: ответ готов?"
)
REPEAT_NO_HISTORY_TEXT = "Я ещё ничего не говорил. Задай свой первый вопрос!"
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

    # ── Balance / account status check ─────────────────────────────────────
    if utterance in BALANCE_PHRASES:
        status = balance_client.get_status()
        return _build_response(status, version=version, session=session_meta)

    # ── Repeat last reply (no Claude call, no token cost) ──────────────────
    if utterance in REPEAT_PHRASES:
        last = session_manager.get_last_reply(user_id) if user_id else None
        text = last if last else REPEAT_NO_HISTORY_TEXT
        return _build_response(text, version=version, session=session_meta)

    # ── History reset commands ──────────────────────────────────────────────
    if utterance in RESET_PHRASES:
        if user_id:
            session_manager.clear_history(user_id)
        return _build_response(RESET_TEXT, version=version, session=session_meta)

    # ── Check for deferred answer ──────────────────────────────────────────
    if utterance in READY_PHRASES and user_id:
        state = pending_store.get_state(user_id)
        if state == pending_store.State.READY:
            answer = pending_store.pop_answer(user_id)
            if answer and user_id:
                # The utterance that triggered this deferred call was already
                # saved in pending_store; history was appended there too.
                return _build_response(answer, version=version, session=session_meta)
        elif state == pending_store.State.PENDING:
            return _build_response(STILL_THINKING_TEXT, version=version, session=session_meta)
        # No pending — fall through to normal Claude call below

    # ── Normal turn: ask Claude (inline wait → async fallback) ────────────
    history = session_manager.get_history(user_id) if user_id else []
    logger.info("user_id=%s utterance=%r history_len=%d", user_id, utterance, len(history))

    if not user_id:
        # Anonymous session — synchronous fallback (no history stored)
        reply = claude_client.ask(history, utterance)
        return _build_response(reply, version=version, session=session_meta)

    def _bg_ask():
        result = claude_client.ask(history, utterance)
        session_manager.append_turn(user_id, utterance, result)
        return result

    pending_store.submit(user_id, _bg_ask)
    future = pending_store.get_future(user_id)
    if future:
        try:
            future.result(timeout=config.ALICE_REPLY_TIMEOUT)
            reply = pending_store.pop_answer(user_id)
            if reply:
                return _build_response(reply, version=version, session=session_meta)
        except Exception:
            pass  # timed out — fall through to "thinking"
    return _build_response(THINKING_TEXT, version=version, session=session_meta)
