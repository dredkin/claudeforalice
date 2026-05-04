"""
In-memory session store that keeps per-user conversation history.

Alice identifies users via ``session.user.user_id`` (a persistent anonymous ID
available in every request). Each session is a list of alternating
``{"role": "user"|"assistant", "content": "..."}`` dicts that is passed
directly to the Claude Messages API.

Sessions automatically expire after ``config.SESSION_TIMEOUT_MINUTES`` minutes
of inactivity. The next request after expiry starts a fresh session.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import config

# { user_id: [{"role": ..., "content": ...}, ...] }
_store: Dict[str, List[Dict[str, str]]] = {}

# { user_id: last_reply_text }  — кэш последнего ответа для функции «повтори»
_last_reply: Dict[str, str] = {}

# { user_id: last_active_timestamp (float) }
_last_active: Dict[str, float] = {}

# { user_id: {"old_q": str, "new_q": str} }  — disambiguation context
_pending_disambig: Dict[str, Dict[str, str]] = {}

_lock = threading.Lock()


def _is_expired(user_id: str) -> bool:
    """Return True if the session for *user_id* has timed out (no lock required)."""
    ts = _last_active.get(user_id)
    if ts is None:
        return False
    timeout = config.SESSION_TIMEOUT_MINUTES * 60
    return (time.time() - ts) > timeout


def _touch(user_id: str) -> None:
    """Update last-active timestamp (no lock required, caller must hold it)."""
    _last_active[user_id] = time.time()


def _expire(user_id: str) -> None:
    """Wipe session data for *user_id* (no lock required, caller must hold it)."""
    _store.pop(user_id, None)
    _last_reply.pop(user_id, None)
    _last_active.pop(user_id, None)
    _pending_disambig.pop(user_id, None)


# ── Disambiguation helpers ─────────────────────────────────────────────────

def set_pending_disambig(user_id: str, old_q: str, new_q: str) -> None:
    """Store disambiguation context: old pending question vs new utterance."""
    with _lock:
        _pending_disambig[user_id] = {"old_q": old_q, "new_q": new_q}


def get_pending_disambig(user_id: str) -> Optional[Dict[str, str]]:
    """Return disambiguation context for *user_id*, or None if not set."""
    with _lock:
        return _pending_disambig.get(user_id)


def clear_pending_disambig(user_id: str) -> None:
    """Remove disambiguation context for *user_id*."""
    with _lock:
        _pending_disambig.pop(user_id, None)


# ── Session expiry ─────────────────────────────────────────────────────────

def check_and_expire(user_id: str) -> bool:
    """
    Check whether *user_id*'s session has timed out. If so, wipe it and return
    True so the caller can start a fresh DB session via ``dialog_log.new_session()``.
    """
    with _lock:
        if _is_expired(user_id):
            _expire(user_id)
            return True
    return False


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
    ``config.MAX_HISTORY_TURNS`` pairs and updates the last-active timestamp.
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
        _touch(user_id)


def clear_history(user_id: str) -> None:
    """Wipe the conversation history and last reply cache for *user_id*."""
    with _lock:
        _expire(user_id)
