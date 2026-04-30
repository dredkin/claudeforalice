"""
Parse an incoming Yandex Alice webhook request and build the response dict.

Alice protocol reference:
  https://yandex.ru/dev/dialogs/alice/doc/request.html
  https://yandex.ru/dev/dialogs/alice/doc/response.html
"""
from __future__ import annotations

import logging
import time as _time
from typing import Any, Dict, Optional

import balance_client
import claude_client
import config
import dialog_log
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


def _respond(
    text: str,
    user_id: Optional[str],
    *,
    end_session: bool = False,
    version: str = "1.0",
    session: Optional[Dict] = None,
    log_as_alice: bool = True,
) -> Dict[str, Any]:
    """Build response AND log Alice's reply to the persistent dialog log."""
    if user_id and log_as_alice:
        try:
            dialog_log.log_alice_message(user_id, text)
        except Exception as exc:
            logger.warning("Failed to log alice message: %s", exc)
    return _build_response(text, end_session=end_session, version=version, session=session)


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
        return _respond(WELCOME_TEXT, user_id, version=version, session=session_meta)

    # ── Log every non-Claude user utterance (commands, repeat, etc.) ────────
    # READY_PHRASES are excluded here — they're logged inside the READY handler
    # AFTER mark_delivered() so the user message appears after Claude's reply.
    _NON_CLAUDE_PHRASES = (
        {"стоп", "выход", "хватит", "закрыть"}
        | BALANCE_PHRASES | REPEAT_PHRASES | RESET_PHRASES
    )
    if utterance and user_id and utterance in _NON_CLAUDE_PHRASES:
        try:
            dialog_log.log_user_message(user_id, utterance)
        except Exception as exc:
            logger.warning("Failed to log user message: %s", exc)

    # ── Explicit exit commands ──────────────────────────────────────────────
    if utterance in {"стоп", "выход", "хватит", "закрыть"}:
        return _respond(END_TEXT, user_id, end_session=True, version=version, session=session_meta)

    # ── Balance / account status check ─────────────────────────────────────
    if utterance in BALANCE_PHRASES:
        status = balance_client.get_status()
        return _respond(status, user_id, version=version, session=session_meta)

    # ── Repeat last reply (no Claude call, no token cost) ──────────────────
    if utterance in REPEAT_PHRASES:
        last = session_manager.get_last_reply(user_id) if user_id else None
        if last:
            # Log repeat delivery as alice message so it appears in dialog log
            if user_id:
                try:
                    dialog_log.log_alice_message(user_id, f"🔁 Повтор: {last}")
                except Exception as exc:
                    logger.warning("Failed to log repeat: %s", exc)
            return _build_response(last, version=version, session=session_meta)
        return _respond(REPEAT_NO_HISTORY_TEXT, user_id, version=version, session=session_meta)

    # ── History reset commands ──────────────────────────────────────────────
    if utterance in RESET_PHRASES:
        if user_id:
            session_manager.clear_history(user_id)
            dialog_log.new_session(user_id)
        return _respond(RESET_TEXT, user_id, version=version, session=session_meta)

    # ── Check for deferred answer ──────────────────────────────────────────
    if utterance in READY_PHRASES:
        if user_id:
            state = pending_store.get_state(user_id)
            if state == pending_store.State.READY:
                stored = pending_store.pop_answer(user_id)
                if stored:
                    reply, msg_id = stored if isinstance(stored, tuple) else (stored, None)
                    # 1. Log user utterance — captures current time as user_ts
                    user_ts = int(_time.time())
                    try:
                        dialog_log.log_user_message(user_id, utterance)
                    except Exception as exc:
                        logger.warning("Failed to log ready utterance: %s", exc)
                    # 2. Mark delivered with user_ts - 1 so Claude's reply
                    #    sorts BEFORE the "готов" utterance
                    if msg_id:
                        try:
                            dialog_log.mark_delivered(msg_id, delivered_at=user_ts + 1)
                        except Exception as exc:
                            logger.warning("Failed to mark delivered: %s", exc)
                    return _build_response(reply, version=version, session=session_meta)
            elif state == pending_store.State.PENDING:
                try:
                    dialog_log.log_user_message(user_id, utterance)
                except Exception as exc:
                    logger.warning("Failed to log ready utterance: %s", exc)
                return _respond(STILL_THINKING_TEXT, user_id, version=version, session=session_meta)
        if user_id and utterance:
            try:
                dialog_log.log_user_message(user_id, utterance)
            except Exception as exc:
                logger.warning("Failed to log ready utterance: %s", exc)
        return _respond(
            "Нет ожидающего ответа. Сначала задайте вопрос.",
            user_id, version=version, session=session_meta,
        )

    # ── Normal turn: ask Claude (inline wait → async fallback) ────────────
    history = session_manager.get_history(user_id) if user_id else []
    logger.info("user_id=%s utterance=%r history_len=%d", user_id, utterance, len(history))

    if not user_id:
        # Anonymous session — synchronous fallback (no history stored)
        reply = claude_client.ask(history, utterance)
        return _build_response(reply, version=version, session=session_meta)

    # Log user message NOW (before background task) to preserve correct order
    try:
        dialog_log.log_user_message(user_id, utterance)
    except Exception as exc:
        logger.warning("Failed to log user message: %s", exc)

    def _bg_ask():
        result = claude_client.ask(history, utterance)
        session_manager.append_turn(user_id, utterance, result)
        # Log with delivered=0 — mark delivered only when sent to Alice
        try:
            msg_id = dialog_log.log_assistant_message(user_id, result)
        except Exception as exc:
            logger.warning("Failed to log assistant message: %s", exc)
            msg_id = None
        return result, msg_id  # return tuple so handler can mark delivered

    pending_store.submit(user_id, _bg_ask)
    future = pending_store.get_future(user_id)
    if future:
        try:
            future.result(timeout=config.ALICE_REPLY_TIMEOUT)
            # _run() stores the return value of _bg_ask() = (reply, msg_id)
            popped = pending_store.pop_answer(user_id)
            if popped:
                reply, msg_id = popped if isinstance(popped, tuple) else (popped, None)
                if msg_id:
                    try:
                        dialog_log.mark_delivered(msg_id)
                    except Exception as exc:
                        logger.warning("Failed to mark delivered: %s", exc)
                return _build_response(reply, version=version, session=session_meta)
        except Exception:
            pass  # timed out — fall through to "thinking"

    # Timed out — will be delivered later via READY_PHRASES
    return _respond(THINKING_TEXT, user_id, version=version, session=session_meta)
