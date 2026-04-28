"""
Async answer cache for deferred Claude responses.

When a Claude request takes longer than Alice's webhook timeout (~5 s) we:
  1. Start the Claude call in a background thread.
  2. Immediately return a "thinking" reply to Alice.
  3. Cache the result when Claude finishes.
  4. On the next user turn ("что ответил?") return the cached answer.

States per user_id:
  PENDING  — Claude is still processing
  READY    — answer is available
  (absent) — no deferred request
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="claude-worker")
_lock = threading.Lock()


class State(Enum):
    PENDING = auto()
    READY = auto()


@dataclass
class _Entry:
    state: State
    answer: Optional[str] = field(default=None)
    future: Optional[Future] = field(default=None, repr=False)


# { user_id: _Entry }
_store: Dict[str, _Entry] = {}


# ── Public API ─────────────────────────────────────────────────────────────

def submit(user_id: str, fn, *args, **kwargs) -> None:
    """
    Run *fn(*args, **kwargs)* in a background thread and cache the result.
    If there is already a pending request for *user_id* it is discarded and
    replaced with the new one.
    """
    with _lock:
        entry = _Entry(state=State.PENDING)
        _store[user_id] = entry

    future = _executor.submit(_run, user_id, fn, args, kwargs)
    with _lock:
        if user_id in _store:
            _store[user_id].future = future


def _run(user_id: str, fn, args, kwargs) -> None:
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.error("Background Claude call failed for user %s: %s", user_id, exc, exc_info=True)
        result = "Что-то пошло не так при обращении к Клоду. Попробуйте задать вопрос снова."

    with _lock:
        entry = _store.get(user_id)
        if entry is not None:
            entry.state = State.READY
            entry.answer = result
    logger.info("Background Claude call finished for user %s", user_id)


def get_future(user_id: str):
    """Return the Future for *user_id*, or None if no pending request."""
    with _lock:
        entry = _store.get(user_id)
        return entry.future if entry else None


def get_state(user_id: str) -> Optional[State]:
    """Return current state or None if no deferred request exists."""
    with _lock:
        entry = _store.get(user_id)
        return entry.state if entry else None


def pop_answer(user_id: str) -> Optional[str]:
    """
    If a READY answer exists, remove it from the store and return it.
    Returns None if still pending or no request.
    """
    with _lock:
        entry = _store.get(user_id)
        if entry and entry.state == State.READY:
            del _store[user_id]
            return entry.answer
    return None


def cancel(user_id: str) -> None:
    """Forget any pending/ready entry for *user_id*."""
    with _lock:
        _store.pop(user_id, None)
