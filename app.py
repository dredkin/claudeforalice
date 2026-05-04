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
  GET/POST /dashboard/settings        — edit .env settings
  POST /dashboard/restart             — restart systemd service
"""
from __future__ import annotations

import hashlib
import logging
import os
import signal
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
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
    level=logging.DEBUG if config.FLASK_DEBUG else logging.INFO,
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


# ── Settings routes ────────────────────────────────────────────────────────

# Keys shown in the UI (order matters)
_SETTINGS_KEYS = [
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "CLAUDE_MAX_TOKENS",
    "CLAUDE_SYSTEM_PROMPT",
    "ENABLE_WEB_SEARCH",
    "WEB_SEARCH_MAX_RESULTS",
    "SESSION_TIMEOUT_MINUTES",
    "MAX_HISTORY_TURNS",
    "ALICE_REPLY_TIMEOUT",
    "USER_LOCATION",
    "USER_TIMEZONE",
    "DASHBOARD_PASSWORD",
    "ALICE_SKILL_TOKEN",
    "FLASK_PORT",
    "FLASK_DEBUG",
    "DB_PATH",
]

# Keys that should NEVER be shown as plain text in the form
_SECRET_KEYS = {"ANTHROPIC_API_KEY", "DASHBOARD_PASSWORD"}

# ── Settings validation ────────────────────────────────────────────────────

def _sanitize_settings(raw: dict) -> tuple[dict, list[str]]:
    """
    Validate and sanitize form values before writing to .env.

    Returns (clean_dict, errors_list).
    Errors is empty on success.
    """
    import re as _re

    clean: dict = {}
    errors: list[str] = []

    def _single_line(val: str) -> str:
        """Remove any newline/carriage-return characters."""
        return val.replace("\r", "").replace("\n", " ").strip()

    def _safe_text(val: str) -> str:
        """
        Multiline text (system prompt): normalise line endings,
        strip null bytes and other control chars except tab/newline.
        """
        val = val.replace("\r\n", "\n").replace("\r", "\n")
        # Remove null bytes and other non-printable chars (keep \t and \n)
        val = "".join(ch for ch in val if ch == "\t" or ch == "\n" or ch >= " ")
        return val.strip()

    def _positive_int(key: str, val: str, min_val: int = 1, max_val: int = 100_000) -> str:
        val = val.strip()
        if not val:
            return val  # will keep existing
        try:
            n = int(val)
            if n < min_val or n > max_val:
                raise ValueError
            return str(n)
        except ValueError:
            errors.append(f"{key}: ожидается целое число от {min_val} до {max_val}, получено «{val}»")
            return val

    def _positive_float(key: str, val: str, min_val: float = 0.1, max_val: float = 60.0) -> str:
        val = val.strip()
        if not val:
            return val
        try:
            n = float(val)
            if n < min_val or n > max_val:
                raise ValueError
            return str(n)
        except ValueError:
            errors.append(f"{key}: ожидается число от {min_val} до {max_val}, получено «{val}»")
            return val

    def _api_key(val: str) -> str:
        val = _single_line(val)
        if val and val != "***":
            # Anthropic keys start with sk-ant-
            if not _re.match(r'^sk-ant-[A-Za-z0-9_\-]+$', val):
                errors.append("ANTHROPIC_API_KEY: неверный формат ключа (ожидается sk-ant-…)")
        return val

    def _iana_tz(key: str, val: str) -> str:
        val = _single_line(val)
        if val:
            # Basic IANA check: letters/digits/underscore/hyphen/slash, no spaces
            if not _re.match(r'^[A-Za-z][A-Za-z0-9/_\-+]*$', val):
                errors.append(f"{key}: неверный формат часового пояса (пример: Europe/Moscow)")
        return val

    def _port(key: str, val: str) -> str:
        return _positive_int(key, val, min_val=1024, max_val=65535)

    # Per-key rules
    for key in _SETTINGS_KEYS:
        v = raw.get(key, "")
        if key == "ANTHROPIC_API_KEY":
            clean[key] = _api_key(v)
        elif key == "CLAUDE_SYSTEM_PROMPT":
            clean[key] = _safe_text(v)
        elif key in ("CLAUDE_MODEL", "USER_LOCATION", "ALICE_SKILL_TOKEN", "DB_PATH"):
            clean[key] = _single_line(v)
        elif key == "CLAUDE_MAX_TOKENS":
            clean[key] = _positive_int(key, v, 1, 32_000)
        elif key == "MAX_HISTORY_TURNS":
            clean[key] = _positive_int(key, v, 1, 500)
        elif key == "SESSION_TIMEOUT_MINUTES":
            clean[key] = _positive_int(key, v, 1, 1440)
        elif key == "WEB_SEARCH_MAX_RESULTS":
            clean[key] = _positive_int(key, v, 1, 5)
        elif key == "FLASK_PORT":
            clean[key] = _port(key, v)
        elif key == "ALICE_REPLY_TIMEOUT":
            clean[key] = _positive_float(key, v, 0.5, 30.0)
        elif key == "USER_TIMEZONE":
            clean[key] = _iana_tz(key, v)
        elif key in ("ENABLE_WEB_SEARCH", "FLASK_DEBUG"):
            clean[key] = "true" if v == "true" else "false"
        elif key == "DASHBOARD_PASSWORD":
            # password: single line, no control chars, skip if empty/unchanged
            val = _single_line(v)
            clean[key] = val
        else:
            clean[key] = _single_line(v)

    return clean, errors


def _read_env() -> dict:
    """Read the current .env file into a dict. Missing keys get empty string."""
    env_path = Path(config.DB_PATH).parent / ".env"
    # fall back to cwd .env
    if not env_path.exists():
        env_path = Path(".env")
    values: dict = {k: "" for k in _SETTINGS_KEYS}
    if not env_path.exists():
        return values
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^([A-Z0-9_]+)\s*=\s*(.*)', line)
            if m:
                key, val = m.group(1), m.group(2)
                # strip surrounding quotes
                if val and val[0] in ('"', "'") and val[-1] == val[0]:
                    val = val[1:-1]
                if key in values:
                    values[key] = val
    # Mask secrets
    for k in _SECRET_KEYS:
        if values.get(k):
            values[k] = "***"
    return values


def _write_env(updates: dict) -> None:
    """
    Update .env in place.  Lines for known keys are updated; completely new
    keys are appended.  Secret keys with value '***' (unchanged placeholder)
    are skipped.
    """
    env_path = Path(config.DB_PATH).parent / ".env"
    if not env_path.exists():
        env_path = Path(".env")

    # Read existing lines
    existing: list[str] = []
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            existing = f.readlines()

    written_keys: set = set()
    new_lines: list[str] = []

    for line in existing:
        stripped = line.strip()
        m = re.match(r'^([A-Z0-9_]+)\s*=', stripped)
        if m:
            key = m.group(1)
            if key in updates:
                val = updates[key]
                # Skip secret keys when value is empty or placeholder — keep original
                if key in _SECRET_KEYS and (not val or val == "***"):
                    new_lines.append(line)
                else:
                    # write new value (quote if contains spaces or newlines)
                    if " " in val or "\n" in val:
                        val_out = f'"{val}"'
                    else:
                        val_out = val
                    new_lines.append(f"{key}={val_out}\n")
                written_keys.add(key)
                continue
        new_lines.append(line)

    # Append keys not yet in file (skip secrets if empty)
    for key, val in updates.items():
        if key not in written_keys and val and val != "***":
            if key in _SECRET_KEYS and not val:
                continue
            if " " in val or "\n" in val:
                val_out = f'"{val}"'
            else:
                val_out = val
            new_lines.append(f"{key}={val_out}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


@app.route("/dashboard/settings", methods=["GET", "POST"])
@_login_required
def dashboard_settings():
    message = None
    message_type = "success"

    # Handle reload/save feedback
    if request.args.get("reloaded"):
        message = "✅ Параметры применены без перезапуска сервиса!"
        message_type = "success"
    elif request.args.get("reload_error"):
        message = f"❌ Ошибка применения: {request.args.get('reload_error')}"
        message_type = "error"
    elif request.args.get("restarted"):
        message = "✅ Сервис успешно перезапущен!"
        message_type = "success"
    elif request.args.get("restart_error"):
        message = f"❌ Ошибка перезапуска: {request.args.get('restart_error')}"
        message_type = "error"

    if request.method == "POST":
        raw: dict = {}
        _BOOL_KEYS = {"ENABLE_WEB_SEARCH", "FLASK_DEBUG"}
        for key in _SETTINGS_KEYS:
            if key in _BOOL_KEYS:
                raw[key] = "true" if request.form.get(key) else "false"
            else:
                raw[key] = request.form.get(key, "")

        updates, val_errors = _sanitize_settings(raw)

        if val_errors:
            message = "⚠️ Ошибки валидации:<br>" + "<br>".join(val_errors)
            message_type = "error"
        else:
            try:
                _write_env(updates)
                message = "✅ Настройки сохранены в .env. Перезапустите сервис чтобы применить изменения."
                message_type = "success"
            except Exception as exc:
                logger.exception("Failed to write .env: %s", exc)
                message = f"❌ Ошибка при сохранении: {exc}"
                message_type = "error"

    env = _read_env()
    # Also populate FLASK_DEBUG for the template
    if "FLASK_DEBUG" not in env:
        env["FLASK_DEBUG"] = "false"
    return render_template(
        "settings.html",
        env=env,
        message=message,
        message_type=message_type,
    )


@app.get("/dashboard/api/models")
@_login_required
def dashboard_api_models():
    """Return list of available Claude models from Anthropic API + hardcoded fallback."""
    fallback = [
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-3-5",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
    ]
    models = fallback  # always start with fallback
    try:
        import claude_client as cc
        # models.list() was added in anthropic >= 0.40; guard with hasattr
        if hasattr(cc.client, "models") and hasattr(cc.client.models, "list"):
            page = cc.client.models.list(limit=100)
            api_models = sorted(
                [m.id for m in page.data if "claude" in m.id.lower()],
                reverse=True,
            )
            if api_models:
                # Merge: API list first, then any fallback entries not already present
                seen = set(api_models)
                extra = [m for m in fallback if m not in seen]
                models = api_models + extra
    except Exception as exc:
        logger.warning("Could not fetch model list from API: %s", exc)
        # models stays as fallback
    return jsonify({"models": models, "source": "api" if models != fallback else "fallback"})


@app.post("/dashboard/reload")
@_login_required
def dashboard_reload():
    """
    Reload configuration from .env without restarting the process.

    Re-reads .env, updates config module attributes, and re-creates the
    Anthropic client with the new API key.  No sudo required.
    """
    try:
        from dotenv import dotenv_values
        from pathlib import Path as _Path

        env_path = _Path(config.DB_PATH).parent / ".env"
        if not env_path.exists():
            env_path = _Path(".env")

        if not env_path.exists():
            return redirect(url_for("dashboard_settings") + "?reload_error=.env+not+found")

        new_vals = dotenv_values(env_path)

        # Helper to update a module-level attribute safely
        def _set(attr, val, cast=str):
            try:
                setattr(config, attr, cast(val))
            except Exception:
                pass

        if new_vals.get("ANTHROPIC_API_KEY"):
            _set("ANTHROPIC_API_KEY", new_vals["ANTHROPIC_API_KEY"])
        if new_vals.get("CLAUDE_MODEL"):
            _set("CLAUDE_MODEL", new_vals["CLAUDE_MODEL"])
        if new_vals.get("CLAUDE_MAX_TOKENS"):
            _set("CLAUDE_MAX_TOKENS", new_vals["CLAUDE_MAX_TOKENS"], int)
        if new_vals.get("CLAUDE_SYSTEM_PROMPT"):
            _set("CLAUDE_SYSTEM_PROMPT", new_vals["CLAUDE_SYSTEM_PROMPT"])
            _set("CLAUDE_SYSTEM_PROMPT_BASE", new_vals["CLAUDE_SYSTEM_PROMPT"])
        if new_vals.get("ENABLE_WEB_SEARCH") is not None:
            _set("ENABLE_WEB_SEARCH", new_vals.get("ENABLE_WEB_SEARCH","false").lower() == "true", bool)
        if new_vals.get("WEB_SEARCH_MAX_RESULTS"):
            _set("WEB_SEARCH_MAX_RESULTS", new_vals["WEB_SEARCH_MAX_RESULTS"], int)
        if new_vals.get("SESSION_TIMEOUT_MINUTES"):
            _set("SESSION_TIMEOUT_MINUTES", new_vals["SESSION_TIMEOUT_MINUTES"], int)
        if new_vals.get("MAX_HISTORY_TURNS"):
            _set("MAX_HISTORY_TURNS", new_vals["MAX_HISTORY_TURNS"], int)
        if new_vals.get("ALICE_REPLY_TIMEOUT"):
            _set("ALICE_REPLY_TIMEOUT", new_vals["ALICE_REPLY_TIMEOUT"], float)
        if new_vals.get("USER_LOCATION") is not None:
            _set("USER_LOCATION", new_vals["USER_LOCATION"])
        if new_vals.get("USER_TIMEZONE"):
            _set("USER_TIMEZONE", new_vals["USER_TIMEZONE"])
        if new_vals.get("ALICE_SKILL_TOKEN") is not None:
            _set("ALICE_SKILL_TOKEN", new_vals["ALICE_SKILL_TOKEN"])
        if new_vals.get("DASHBOARD_PASSWORD"):
            _set("DASHBOARD_PASSWORD", new_vals["DASHBOARD_PASSWORD"])
            # Re-generate secret key hint but don't change Flask session key mid-flight
        if new_vals.get("FLASK_DEBUG") is not None:
            _set("FLASK_DEBUG", new_vals.get("FLASK_DEBUG","false").lower() == "true", bool)

        # Recreate Anthropic client with new API key
        import claude_client
        import anthropic as _anthropic
        claude_client.client = _anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # Clear geo-detection cache so new location/tz takes effect
        import geo_detect
        geo_detect._cached = None

        logger.info("Configuration reloaded from %s", env_path)
        return redirect(url_for("dashboard_settings") + "?reloaded=1")

    except Exception as exc:
        logger.exception("Reload error: %s", exc)
        from urllib.parse import quote
        return redirect(url_for("dashboard_settings") + "?reload_error=" +
                        quote(str(exc)[:200], safe=""))


@app.post("/dashboard/restart")
@_login_required
def dashboard_restart():
    """Restart the claudeforalice systemd service.

    Sends a delayed SIGTERM to the gunicorn master process (os.getppid()).
    systemd catches the exit and restarts the service automatically
    because Restart=always is set in the unit file.
    The response is sent to the browser before the signal fires.
    """
    import threading, time as _time
    from urllib.parse import quote as _quote

    def _delayed_kill():
        _time.sleep(1.5)  # allow Flask to flush the redirect response
        try:
            ppid = os.getppid()
            logger.info("Sending SIGTERM to gunicorn master (pid=%d) for restart", ppid)
            os.kill(ppid, signal.SIGTERM)
        except Exception as exc:
            logger.error("Failed to send SIGTERM: %s", exc)

    threading.Thread(target=_delayed_kill, daemon=True).start()
    logger.info("Service restart scheduled from dashboard")
    return redirect(url_for("dashboard_settings") + "?restarted=1")


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
    d, rem  = divmod(elapsed, 86400)
    h, rem  = divmod(rem, 3600)
    m, s    = divmod(rem, 60)
    if d:
        uptime = f"{d}д {h}ч {m:02d}м"
    elif h:
        uptime = f"{h}ч {m:02d}м {s:02d}с"
    else:
        uptime = f"{m}м {s:02d}с"

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
        "service": "Claude for Alice AI skill",
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
