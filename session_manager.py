"""
In-memory session store that keeps per-user conversation history.

Alice identifies users via ``session.user.user_id`` (a persistent anonymous ID
available in every request). Each session is a list of alternating
``{"role": "user"|"assistant", "content": "..."}`` dicts that is passed
directly to the Claude Messages API.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

import config

# { user_id: [{"role": ..., "content": ...}, ...] }
_store: Dict[str, List[Dict[str, str]]] = {}

# { user_id: last_reply_text }  — кэш последнего ответа для функции «повтори»
_last_reply: Dict[str, str] = {}

_lock = threading.Lock()


def get_history(user_id: str) -> List[Dict[str, str]]:
    """Return a *copy* of the conversation history for *user_id*."""
    with _lock:
        return list(_store.get(user_id, []))


def get_last_reply(user_id: str) -> Optional[str]:
    """Return the last assistant reply for *user_id*, or None if not available."""
    with _lock:
        return _last_reply.get(user_id)


def append_turn(user_id: str, user_text: str, assistant_text: str) -> None:
    """
    Append a completed user→assistant turn to the session.

    Automatically trims the oldest turns when the history exceeds
    ``config.MAX_HISTORY_TURNS`` pairs.
    """
    with _lock:
        history = _store.setdefault(user_id, [])
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": assistant_text})

        # Keep only the most recent N turns (each turn = 2 messages)
        max_messages = config.MAX_HISTORY_TURNS * 2
        if len(history) > max_messages:
            _store[user_id] = history[-max_messages:]

        # Cache last reply for repeat requests
        _last_reply[user_id] = assistant_text


def clear_history(user_id: str) -> None:
    """Wipe the conversation history and last reply cache for *user_id*."""
    with _lock:
        _store.pop(user_id, None)
        _last_reply.pop(user_id, None)
