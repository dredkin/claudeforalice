"""
Flask entry point for the Yandex Alice → Claude AI skill.

Routes:
  POST /alice                         — Yandex Alice webhook
  GET  /alice                         — diagnostic JSON
  GET  /health                        — liveness probe
  GET  /dashboard                     — web dashboard (requires login)
  GET/POST /dashboard/login
  GET  /dashboard/logout
  GET  /dashboard/dialogs             — dialog history list
  GET  /dashboard/dialogs/<id>        — dialog detail
  POST /dashboard/dialogs/<id>/delete — delete dialog
"""
from __future__ import annotations

import hashlib
import logging
import sys
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any

import anthropic
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import alice_handler
import balance_client
import claude_client
import config
import dialog_log
import pending_store
import session_manager

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = config.SECRET_KEY
_start_time = time.time()

# ── Jinja filters ──────────────────────────────────────────────────────────
@app.template_filter("fmtts")
def _fmtts(ts: int) -> str:
    """Format Unix timestamp as human-readable Moscow-ish datetime."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(ts)


# ── DB init ────────────────────────────────────────────────────────────────
with app.app_context():
    dialog_log.init_db()


# ── Dashboard auth helpers ─────────────────────────────────────────────────

def _check_password(pw: str) -> bool:
    if not config.DASHBOARD_PASSWORD:
        return False
    expected = hashlib.sha256(config.DASHBOARD_PASSWORD.encode()).hexdigest()
    given    = hashlib.sha256(pw.encode()).hexdigest()
    return expected == given


def _login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("dashboard_auth"):
            return redirect(url_for("dashboard_login"))
        return f(*args, **kwargs)
    return decorated


# ── Dashboard routes ───────────────────────────────────────────────────────

@app.route("/dashboard/login", methods=["GET", "POST"])
def dashboard_login():
    if not config.DASHBOARD_PASSWORD:
        return "DASHBOARD_PASSWORD не задан в .env", 503

    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if _check_password(pw):
            session["dashboard_auth"] = True
            return redirect(url_for("dashboard_index"))
        error = "Неверный пароль"
    return render_template("login.html", error=error)


@app.get("/dashboard/logout")
def dashboard_logout():
    session.pop("dashboard_auth", None)
    return redirect(url_for("dashboard_login"))


# ── Dialog history routes ──────────────────────────────────────────────────
_PER_PAGE = 30


@app.get("/dashboard/dialogs")
@_login_required
def dashboard_dialogs():
    page    = max(1, request.args.get("page", 1, type=int))
    offset  = (page - 1) * _PER_PAGE
    total   = dialog_log.count_sessions()
    rows    = dialog_log.list_sessions(limit=_PER_PAGE, offset=offset)

    # Attach last user message preview
    for row in rows:
        msgs = dialog_log.get_messages(row["id"])
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        row["preview"] = last_user[:100] if last_user else ""

    return render_template(
        "dialogs.html",
        sessions=rows,
        total=total,
        page=page,
        per_page=_PER_PAGE,
    )


@app.get("/dashboard/dialogs/<int:sid>")
@_login_required
def dashboard_dialog_detail(sid: int):
    sess = dialog_log.get_session(sid)
    if not sess:
        abort(404)
    msgs = dialog_log.get_messages(sid)
    return render_template("dialog_detail.html", session=sess, messages=msgs)


@app.post("/dashboard/dialogs/<int:sid>/delete")
@_login_required
def dashboard_dialog_delete(sid: int):
    dialog_log.delete_session(sid)
    return redirect(url_for("dashboard_dialogs"))


@app.get("/dashboard")
@_login_required
def dashboard_index():
    # ── Uptime ──
    elapsed = int(time.time() - _start_time)
    h, rem = divmod(elapsed, 3600)
    m, s   = divmod(rem, 60)
    uptime  = f"{h}ч {m:02d}м {s:02d}с"

    # ── Anthropic account probe ──
    probe_text = balance_client.get_status()
    if "активен" in probe_text.lower():
        account_dot, account_title = "green", "Аккаунт активен"
    elif "деньги" in probe_text.lower() or "средств" in probe_text.lower():
        account_dot, account_title = "red", "Нет средств"
    elif "ключ" in probe_text.lower():
        account_dot, account_title = "red", "Неверный ключ"
    else:
        account_dot, account_title = "yellow", "Требует внимания"

    # ── Sessions ──
    from session_manager import _store, _lock as _sm_lock
    with _sm_lock:
        store_copy = {uid: list(hist) for uid, hist in _store.items()}

    sessions_data = []
    for uid, hist in store_copy.items():
        turns = len(hist) // 2
        last_user = ""
        for msg in reversed(hist):
            if msg["role"] == "user":
                last_user = msg["content"]
                break
        is_pending = pending_store.get_state(uid) == pending_store.State.PENDING
        sessions_data.append({
            "user_id":  uid[:12] + "…" if len(uid) > 12 else uid,
            "turns":    turns,
            "last_user": last_user,
            "pending":  is_pending,
        })

    # ── API key mask ──
    key = config.ANTHROPIC_API_KEY
    if key and len(key) > 8:
        api_key_masked = key[:7] + "…" + key[-4:]
    elif key:
        api_key_masked = "***"
    else:
        api_key_masked = "не задан ❌"

    return render_template(
        "dashboard.html",
        uptime          = uptime,
        python_version  = sys.version.split()[0],
        sdk_version     = anthropic.__version__,
        flask_port      = config.FLASK_PORT,
        flask_debug     = "вкл" if config.FLASK_DEBUG else "выкл",
        account_dot     = account_dot,
        account_title   = account_title,
        account_text    = probe_text,
        claude_model    = config.CLAUDE_MODEL,
        claude_max_tokens = config.CLAUDE_MAX_TOKENS,
        reply_timeout   = config.ALICE_REPLY_TIMEOUT,
        max_history     = config.MAX_HISTORY_TURNS,
        alice_token_set = "задан ✅" if config.ALICE_SKILL_TOKEN else "не задан",
        api_key_masked  = api_key_masked,
        sessions        = sessions_data,
    )


# ── Diagnostic GET /alice ──────────────────────────────────────────────────
@app.get("/alice")
def alice_info():
    """Browser-friendly diagnostic JSON (no auth required)."""
    key = config.ANTHROPIC_API_KEY
    key_status = "✅ set" if key and key != "sk-ant-..." else "❌ NOT SET"
    return jsonify({
        "service": "Alice × Claude AI skill",
        "status": "running",
        "endpoint": "POST /alice  (Yandex Alice webhook)",
        "dashboard": "/dashboard",
        "diagnostics": {
            "python_version":    sys.version,
            "anthropic_sdk":     anthropic.__version__,
            "claude_model":      config.CLAUDE_MODEL,
            "claude_max_tokens": config.CLAUDE_MAX_TOKENS,
            "reply_timeout":     config.ALICE_REPLY_TIMEOUT,
            "flask_port":        config.FLASK_PORT,
            "max_history_turns": config.MAX_HISTORY_TURNS,
            "api_key_status":    key_status,
            "token_auth_enabled": bool(config.ALICE_SKILL_TOKEN),
        },
        "hint": "Send a POST with a valid Yandex Alice JSON payload to use this endpoint.",
    }), 200


# ── Webhook POST /alice ────────────────────────────────────────────────────
@app.post("/alice")
def alice_webhook():
    """Receive an Alice skill request and return a Claude-powered response."""
    if config.ALICE_SKILL_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"OAuth {config.ALICE_SKILL_TOKEN}":
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
    return jsonify({"status": "ok"})


# ── Error handlers ─────────────────────────────────────────────────────────
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Bad Request", "hint": "Body must be valid JSON"}), 400

@app.errorhandler(403)
def forbidden(e):
    return jsonify({"error": "Forbidden", "detail": "Invalid or missing Authorization token"}), 403

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not Found", "routes": ["GET /alice", "POST /alice", "GET /health", "GET /dashboard"]}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method Not Allowed", "hint": "POST /alice for webhook, GET /alice for diagnostics"}), 405

@app.errorhandler(500)
def internal_error(e):
    logger.error("500: %s", e)
    return jsonify({"error": "Internal Server Error", "detail": str(e)}), 500


# ── Dev server ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG)
