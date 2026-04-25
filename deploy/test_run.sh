#!/usr/bin/env bash
# =============================================================================
# test_run.sh — тестовый запуск навыка прямо в консоли (без systemd)
#
# Использование (из корня проекта или из /srv/claudeforalice):
#   bash deploy/test_run.sh
#
# Переменные окружения:
#   APP_DIR   — каталог приложения (по умолчанию: текущий каталог)
#   PORT      — порт Flask (по умолчанию: 5000)
# =============================================================================
set -euo pipefail

# ── Параметры ─────────────────────────────────────────────────────────────
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PORT="${PORT:-37842}"
VENV="$APP_DIR/.venv"
ENV_FILE="$APP_DIR/.env"

# ── Helpers ───────────────────────────────────────────────────────────────
info()  { echo -e "\e[32m[INFO]\e[0m  $*"; }
warn()  { echo -e "\e[33m[WARN]\e[0m  $*"; }
error() { echo -e "\e[31m[ERROR]\e[0m $*" >&2; exit 1; }

# ── Проверки ──────────────────────────────────────────────────────────────
[[ -d "$APP_DIR" ]] || error "Каталог приложения не найден: $APP_DIR"
[[ -f "$APP_DIR/app.py" ]] || error "app.py не найден в $APP_DIR"

# Виртуальное окружение
if [[ ! -d "$VENV" ]]; then
    warn "Виртуальное окружение не найдено. Создаём $VENV..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip -q
    "$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" -q
    info "Зависимости установлены."
fi

# .env
if [[ -f "$ENV_FILE" ]]; then
    info "Загружаем переменные из $ENV_FILE"
    # shellcheck disable=SC2046
    export $(grep -v '^\s*#' "$ENV_FILE" | grep -v '^\s*$' | xargs)
else
    warn ".env не найден. Убедитесь, что ANTHROPIC_API_KEY задан в окружении."
fi

# Ключ API
if [[ -z "${ANTHROPIC_API_KEY:-}" || "${ANTHROPIC_API_KEY}" == "sk-ant-..." ]]; then
    error "ANTHROPIC_API_KEY не задан. Заполните $ENV_FILE или экспортируйте переменную."
fi

# ── Запуск ────────────────────────────────────────────────────────────────
export FLASK_DEBUG=true
export FLASK_PORT="$PORT"

info "Запуск Flask dev-сервера на порту $PORT..."
info "  Webhook URL для Alice: http://<ваш-IP>:$PORT/alice"
info "  Healthcheck          : http://localhost:$PORT/health"
info "  Остановка            : Ctrl+C"
echo ""

cd "$APP_DIR"
exec "$VENV/bin/python" app.py
