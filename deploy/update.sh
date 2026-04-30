#!/usr/bin/env bash
# =============================================================================
# update.sh — обновление файлов навыка на рабочем сервере
#
# Использование:
#   bash deploy/update.sh [опции]
#
# Опции:
#   --no-restart   — не перезапускать сервис после обновления
#   --deps         — переустановить Python-зависимости (если изменился requirements.txt)
#   --apache       — скопировать конфиги Apache и перезагрузить apache2
#   --all          — --deps + --apache + перезапуск сервиса
#
# Требования:
#   - sudo права (запускать от обычного пользователя, sudo попросит пароль при необходимости)
#   - rsync
# =============================================================================
set -euo pipefail

# ── Корень проекта ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Параметры ─────────────────────────────────────────────────────────────
APP_DIR="/srv/claudeforalice"
SERVICE_NAME="claudeforalice"

DO_RESTART=true
DO_DEPS=false
DO_APACHE=false

# ── Helpers ───────────────────────────────────────────────────────────────
info()    { echo -e "\e[32m[INFO]\e[0m  $*"; }
warn()    { echo -e "\e[33m[WARN]\e[0m  $*"; }
error()   { echo -e "\e[31m[ERROR]\e[0m $*" >&2; exit 1; }
success() { echo -e "\e[32m[OK]\e[0m    $*"; }

# ── Аргументы ─────────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --no-restart) DO_RESTART=false ;;
        --deps)       DO_DEPS=true ;;
        --apache)     DO_APACHE=true ;;
        --all)        DO_DEPS=true; DO_APACHE=true ;;
        --help|-h)
            grep '^#' "$0" | head -20 | sed 's/^# \?//'
            exit 0
            ;;
        *) error "Неизвестный аргумент: $arg. Используйте --help." ;;
    esac
done

# ── Проверки ──────────────────────────────────────────────────────────────
[[ -d "$APP_DIR" ]] || error "Каталог $APP_DIR не найден. Сначала запустите deploy/install.sh"
command -v rsync &>/dev/null || error "rsync не установлен: sudo apt-get install rsync"

# ── 1. Синхронизация файлов приложения ────────────────────────────────────
info "Синхронизация файлов приложения → $APP_DIR ..."

rsync -a --checksum \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='deploy' \
    --exclude='.env' \
    --exclude='*.pyc' \
    "$PROJECT_DIR/" "$APP_DIR/"

success "Файлы приложения обновлены."

# ── 2. Синхронизация systemd-юнита (если изменился) ───────────────────────
UNIT_SRC="$SCRIPT_DIR/claudeforalice.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME.service"

if [[ -f "$UNIT_SRC" ]]; then
    if ! diff -q "$UNIT_SRC" "$UNIT_DST" &>/dev/null 2>&1; then
        info "Обнаружены изменения в systemd-юните, обновляем..."
        sudo cp "$UNIT_SRC" "$UNIT_DST"
        sudo systemctl daemon-reload
        success "Юнит обновлён, daemon перезагружен."
    else
        info "Systemd-юнит не изменился — пропускаем."
    fi
fi

# ── 3. Переустановка зависимостей (опционально) ────────────────────────────
if $DO_DEPS; then
    info "Обновление Python-зависимостей..."
    sudo -u claudeforalice "$APP_DIR/.venv/bin/pip" install \
        --upgrade --quiet -r "$APP_DIR/requirements.txt"
    success "Зависимости обновлены."
fi

# ── 4. Обновление конфигов Apache (опционально) ────────────────────────────
if $DO_APACHE; then
    CONF_SRC="$SCRIPT_DIR/apache/conf-available/claude.conf"
    CONF_DST="/etc/apache2/conf-available/claude.conf"

    if [[ -f "$CONF_SRC" ]]; then
        info "Обновление Apache conf-available/claude.conf..."
        sudo cp "$CONF_SRC" "$CONF_DST"
    fi

    # sites-available
    for f in claude.conf claude-ssl.conf; do
        SRC="$SCRIPT_DIR/apache/sites-available/$f"
        DST="/etc/apache2/sites-available/$f"
        if [[ -f "$SRC" ]]; then
            info "Обновление sites-available/$f..."
            sudo cp "$SRC" "$DST"
        fi
    done

    info "Проверка конфигурации Apache..."
    sudo apache2ctl configtest
    info "Перезагрузка Apache..."
    sudo systemctl reload apache2
    success "Apache обновлён и перезагружен."
fi

# ── 5. Права на файлы приложения ──────────────────────────────────────────
sudo chown -R claudeforalice:claudeforalice "$APP_DIR"

# ── 6. Перезапуск сервиса ─────────────────────────────────────────────────
if $DO_RESTART; then
    # Always reload systemd before restart to pick up any unit file changes
    sudo systemctl daemon-reload
    info "Перезапуск сервиса $SERVICE_NAME..."
    sudo systemctl restart "$SERVICE_NAME"
    sleep 1
    if sudo systemctl is-active --quiet "$SERVICE_NAME"; then
        success "Сервис $SERVICE_NAME запущен."
    else
        error "Сервис $SERVICE_NAME НЕ запустился. Лог: sudo journalctl -u $SERVICE_NAME -n 30"
    fi
else
    info "Перезапуск пропущен (--no-restart)."
fi

# ── Итог ──────────────────────────────────────────────────────────────────
echo ""
info "=== Обновление завершено ==="
echo "  Каталог : $APP_DIR"
echo "  Сервис  : sudo systemctl status $SERVICE_NAME"
echo "  Логи    : sudo journalctl -u $SERVICE_NAME -f"
