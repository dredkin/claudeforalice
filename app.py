"""
Flask entry point for the Yandex Alice → Claude AI skill.

Single POST endpoint: /alice
"""
from __future__ import annotations

import logging
import os
import sys

from flask import Flask, abort, jsonify, request

import alice_handler
import config

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────
app = Flask(__name__)


# ── Diagnostic GET /alice ──────────────────────────────────────────────────
@app.get("/alice")
def alice_info():
    """
    Browser-friendly diagnostic endpoint.
    Returns service status and config summary (without secrets).
    """
    import anthropic
    api_key = config.ANTHROPIC_API_KEY
    key_status = "✅ set" if api_key and api_key != "sk-ant-..." else "❌ NOT SET"

    info = {
        "service": "Alice × Claude AI skill",
        "status": "running",
        "endpoint": "POST /alice  (Yandex Alice webhook)",
        "diagnostics": {
            "python_version": sys.version,
            "anthropic_sdk_version": anthropic.__version__,
            "claude_model": config.CLAUDE_MODEL,
            "claude_max_tokens": config.CLAUDE_MAX_TOKENS,
            "flask_host": config.FLASK_HOST,
            "flask_port": config.FLASK_PORT,
            "max_history_turns": config.MAX_HISTORY_TURNS,
            "api_key_status": key_status,
            "token_auth_enabled": bool(config.ALICE_SKILL_TOKEN),
        },
        "hint": "Send a POST request with a valid Yandex Alice JSON payload to use this endpoint.",
    }
    return jsonify(info), 200


# ── Webhook POST /alice ────────────────────────────────────────────────────
@app.post("/alice")
def alice_webhook():
    """Receive an Alice skill request and return a Claude-powered response."""

    # Optional token-based verification
    if config.ALICE_SKILL_TOKEN:
        auth = request.headers.get("Authorization", "")
        expected = f"OAuth {config.ALICE_SKILL_TOKEN}"
        if auth != expected:
            logger.warning("Unauthorized request: bad or missing token")
            abort(403)

    event = request.get_json(silent=True)
    if not event:
        logger.warning("Received empty or non-JSON body")
        abort(400)

    logger.debug("Incoming event: %s", event)
    try:
        response = alice_handler.handle(event)
    except Exception as exc:
        logger.exception("Unhandled error in alice_handler: %s", exc)
        abort(500)
    logger.debug("Outgoing response: %s", response)

    return jsonify(response)


# ── Health ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    """Simple liveness probe."""
    return jsonify({"status": "ok"})


# ── Error handlers ─────────────────────────────────────────────────────────
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Bad Request", "detail": str(e), "hint": "Body must be valid JSON"}), 400


@app.errorhandler(403)
def forbidden(e):
    return jsonify({"error": "Forbidden", "detail": "Invalid or missing Authorization token"}), 403


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not Found", "available_routes": ["GET /alice", "POST /alice", "GET /health"]}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({
        "error": "Method Not Allowed",
        "detail": str(e),
        "hint": "POST /alice — for Yandex Alice webhook. GET /alice — for diagnostics.",
    }), 405


@app.errorhandler(500)
def internal_error(e):
    logger.error("500 Internal Server Error: %s", e)
    return jsonify({"error": "Internal Server Error", "detail": str(e)}), 500


# ── Dev server ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "Copy .env.example to .env and fill in the value."
        )
    app.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.FLASK_DEBUG,
    )
