"""
Account status checker for the Anthropic API.

Anthropic does not expose a public billing-balance endpoint, so we probe
the account by sending the smallest possible request (max_tokens=1).
The response or error tells us whether the key is valid and has funds.

Uses the shared Anthropic client and error classifier from claude_client.
"""
from __future__ import annotations

import logging

import claude_client  # shared client + _classify_error
import config

logger = logging.getLogger(__name__)

_PROBE_SYSTEM = "Ответь одним словом."
_PROBE_USER   = "Привет"

_OK_TEXT = (
    "Аккаунт Клода активен и работает. "
    "Точный остаток средств можно узнать в личном кабинете на сайте Anthropic: "
    "console dot anthropic dot com."
)


def get_status() -> str:
    """Probe the Anthropic API and return a human-readable account status."""
    try:
        claude_client.client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1,
            system=_PROBE_SYSTEM,
            messages=[{"role": "user", "content": _PROBE_USER}],
        )
        return _OK_TEXT
    except Exception as exc:  # noqa: BLE001
        return claude_client._classify_error(exc)
