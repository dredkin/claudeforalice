#!/usr/bin/env bash
# =============================================================================
# install.sh — установка навыка Claude for Alice на Ubuntu
# Запускать от root (или через sudo):
#   sudo bash deploy/install.sh
# =============================================================================
set -euo pipefail

# ── Корень проекта = каталог на уровень выше самого скрипта ───────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Параметры ─────────────────────────────────────────────────────────────
APP_USER="claudeforalice"
APP_GROUP="claudeforalice"
APP_DIR="/srv/claudeforalice"
LOG_DIR="/var/log/claudeforalice"
SERVICE_NAME="claudeforalice"
SERVICE_FILE="$SCRIPT_DIR/claudeforalice.service"
PYTHON="${PYTHON:-python3}"

# ── Helpers ───────────────────────────────────────────────────────────────
info()  { echo -e "\e[32m[INFO]\e[0m  $*"; }
warn()  { echo -e "\e[33m[WARN]\e[0m  $*"; }
error() { echo -e "\e[31m[ERROR]\e[0m $*" >&2; exit 1; }

# ── Проверка прав ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Скрипт должен выполняться от root (sudo)."

# ── 1. Системные зависимости ──────────────────────────────────────────────
info "Обновление списка пакетов и установка зависимостей..."
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip curl

# ── 2. Пользователь и группа ──────────────────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
    info "Создание системного пользователя '$APP_USER'..."
    useradd --system \
            --shell /usr/sbin/nologin \
            --home-dir "$APP_DIR" \
            --create-home \
            "$APP_USER"
else
    info "Пользователь '$APP_USER' уже существует."
fi

# ── 3. Каталог приложения ─────────────────────────────────────────────────
info "Создание каталога $APP_DIR..."
mkdir -p "$APP_DIR"

info "Копирование файлов проекта в $APP_DIR..."
# Копируем всё из корня проекта, кроме deploy/, .venv/, __pycache__/, .git/
rsync -a --exclude='.venv' \
         --exclude='__pycache__' \
         --exclude='.git' \
         --exclude='deploy' \
         "$PROJECT_DIR/" "$APP_DIR/"

# ── 4. Файл окружения ─────────────────────────────────────────────────────
if [[ ! -f "$APP_DIR/.env" ]]; then
    if [[ -f "$APP_DIR/.env.example" ]]; then
        info "Создание $APP_DIR/.env из .env.example (заполните ANTHROPIC_API_KEY вручную)."
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    else
        warn ".env не найден. Создайте $APP_DIR/.env вручную перед запуском сервиса."
    fi
else
    info "$APP_DIR/.env уже существует — пропускаем."
fi

# ── 5. Виртуальное окружение и зависимости ────────────────────────────────
info "Создание виртуального окружения $APP_DIR/.venv..."
"$PYTHON" -m venv "$APP_DIR/.venv"

info "Установка Python-зависимостей..."
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# ── 6. Директория логов ───────────────────────────────────────────────────
info "Создание каталога логов $LOG_DIR..."
mkdir -p "$LOG_DIR"

# ── 7. Права доступа ─────────────────────────────────────────────────────
info "Установка прав доступа..."
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chown -R "$APP_USER:$APP_GROUP" "$LOG_DIR"
chmod 750 "$APP_DIR"
chmod 640 "$APP_DIR/.env" 2>/dev/null || true

# ── 8. Systemd-сервис ─────────────────────────────────────────────────────
info "Установка systemd-юнита..."
cp "$SERVICE_FILE" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── 9. Запуск ─────────────────────────────────────────────────────────────
# Проверяем, задан ли ключ (не заглушка)
ENV_KEY=$(grep -E '^ANTHROPIC_API_KEY=' "$APP_DIR/.env" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
if [[ -z "$ENV_KEY" || "$ENV_KEY" == "sk-ant-..." ]]; then
    warn "ANTHROPIC_API_KEY не задан в $APP_DIR/.env"
    warn "Отредактируйте файл и затем выполните: sudo systemctl start $SERVICE_NAME"
else
    info "Запуск сервиса $SERVICE_NAME..."
    systemctl start "$SERVICE_NAME"
    systemctl status "$SERVICE_NAME" --no-pager
fi

echo ""
info "=== Установка завершена ==="
echo "  Каталог приложения : $APP_DIR"
echo "  Логи               : $LOG_DIR"
echo "  Управление         : systemctl {start|stop|restart|status} $SERVICE_NAME"
echo "  Настройки          : $APP_DIR/.env"
