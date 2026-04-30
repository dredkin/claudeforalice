# Alice × Claude — навык Яндекс Алисы на базе Claude AI

Навык позволяет пользователям разговаривать с моделью Anthropic Claude прямо через Яндекс Алису.
Каждый пользователь получает свою историю диалога. Все сессии сохраняются в SQLite и доступны через веб-дашборд.

---

## Архитектура

```
Alice webhook (POST /alice)
        │
        ▼
  alice_handler.py         ← разбирает запрос, команды, асинхронный ответ
        │
        ├── session_manager.py   ← in-memory история + кэш последнего ответа
        ├── pending_store.py     ← фоновые задачи (обход таймаута Алисы)
        ├── dialog_log.py        ← SQLite-лог всех сессий и сообщений
        ├── balance_client.py    ← проверка состояния аккаунта Anthropic
        │
        └── claude_client.py     ← запросы к Anthropic API + обработка ошибок
                │
                ▼
          Anthropic Claude API
```

| Файл | Назначение |
|------|-----------|
| [`app.py`](app.py) | Flask: вебхук Alice, диагностика, веб-дашборд |
| [`alice_handler.py`](alice_handler.py) | Парсинг Alice, голосовые команды, async-ответ, логирование |
| [`claude_client.py`](claude_client.py) | Обёртка над `anthropic` SDK, классификация ошибок |
| [`session_manager.py`](session_manager.py) | In-memory история и кэш последнего ответа |
| [`pending_store.py`](pending_store.py) | `ThreadPoolExecutor` для медленных запросов |
| [`dialog_log.py`](dialog_log.py) | SQLite: сессии + сообщения + статус доставки |
| [`balance_client.py`](balance_client.py) | Пробный запрос для проверки аккаунта |
| [`config.py`](config.py) | Все настройки из `.env` |

---

## Быстрый старт

### 1. Клонировать / скопировать проект

```bash
git clone <repo-url>
cd <repo-dir>
```

### 2. Виртуальное окружение и зависимости

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

pip install -r requirements.txt
```

### 3. Настроить переменные окружения

```bash
cp .env.example .env
# Заполнить: ANTHROPIC_API_KEY, DASHBOARD_PASSWORD, SECRET_KEY
```

### 4. Запустить сервер

**Разработка:**
```bash
bash deploy/test_run.sh
```

**Продакшн (systemd):**
```bash
sudo bash deploy/install.sh
```

> ⚠️ **Важно:** gunicorn должен запускаться с `--workers 1 --threads 8 --worker-class gthread`.
> Несколько воркеров — разные процессы; `pending_store` и `session_manager` между ними не синхронизируются.
> Это уже настроено в [`deploy/claudeforalice.service`](deploy/claudeforalice.service).

**Обновление после изменений:**
```bash
bash deploy/update.sh           # код + перезапуск
bash deploy/update.sh --deps    # + зависимости
bash deploy/update.sh --apache  # + конфиги Apache
bash deploy/update.sh --all     # всё вместе
```

---

## Регистрация навыка в Яндекс Диалогах

1. Зайти на [dialogs.yandex.ru](https://dialogs.yandex.ru/) → **Создать диалог → Навык Алисы**.
2. Задать **фразу активации** (до 2 вариантов), которые хорошо склоняются после «спроси у …»:
   - **«Умный Клод»** → «Алиса, спроси у умного Клода, …»
   - **«Дядя Клод»** → «Алиса, спроси у дяди Клода, …»
   - **«Доктор Клод»** → «Алиса, спроси у доктора Клода, …»
3. Указать **Webhook URL**: `https://claude.dredkin.ru:4443/alice`
4. (Опционально) Скопировать OAuth-токен навыка в `ALICE_SKILL_TOKEN` в `.env`.
5. Сохранить и опубликовать.

---

## Голосовые команды

### Обычный вопрос
Любая фраза → ответ от Claude.
Если Claude не успел за `ALICE_REPLY_TIMEOUT` секунд:
> «Клод думает над ответом. Спросите: ответ готов?»

### Получить отложенный ответ
> «что ответил», «ответ готов», «что там», «говори», «давай», «слушаю», «а сейчас» и др.

### Повторить последний ответ (без расхода токенов)
> «повтори», «ещё раз», «не расслышал», «не расслышала», «скажи ещё раз», «можешь повторить» и др.

### Проверить состояние аккаунта
> «сколько денег», «баланс», «проверь баланс», «работает ли Клод», «проверь Клода» и др.

### Сброс истории диалога
> «сбрось историю», «новый диалог», «начни сначала», «очисти историю»

### Завершить навык
> «стоп», «выход», «хватит», «закрыть»

---

## Асинхронный ответ (обход таймаута Алисы)

Alice ждёт ответ ~5 с. Claude может генерировать дольше. Схема:

```
Пользователь задаёт вопрос
        │
        ▼
Claude запускается в фоне (ThreadPoolExecutor)
        │
   Ждём ALICE_REPLY_TIMEOUT секунд
        │
   ┌────┴────┐
успел       не успел
   │            │
   ▼            ▼
Ответ сразу   «Клод думает…»
              (фон продолжает работу)
                    │
              Пользователь: «что ответил?»
                    │
              Алиса зачитывает ответ
```

---

## Веб-дашборд

Доступен по адресу `/dashboard` (требует авторизации паролем).

### Блоки главной страницы

| Блок | Содержимое |
|------|-----------|
| Статус сервиса | Python, SDK, порт, uptime |
| Аккаунт Anthropic | Живая проверка + ссылка на billing |
| Настройки Claude | Модель, токены, таймаут, история |
| Активные сессии | Список пользователей в памяти |

### История диалогов (`/dashboard/dialogs`)

- Список всех сессий с пагинацией и превью последнего вопроса
- Детальный вид (`/dashboard/dialogs/<id>`) — пузырьки чата:
  - 👤 Пользователь
  - 🤖 Клод (с временем получения и временем доставки Алисе)
  - 💬 Алиса (промежуточные ответы: «думает», «повтор» и т.д.)
- Метка ⏳ на ответах Клода, которые ещё не доставлены пользователю
- Кнопка удаления диалога

---

## Настройка

Все параметры задаются через `.env` (см. [`.env.example`](.env.example)):

| Переменная | По умолчанию | Описание |
|-----------|-------------|---------|
| `ANTHROPIC_API_KEY` | — | **Обязательно.** Ключ Anthropic API |
| `CLAUDE_MODEL` | `claude-opus-4-5` | Модель Claude |
| `CLAUDE_MAX_TOKENS` | `1024` | Макс. токенов в ответе |
| `CLAUDE_SYSTEM_PROMPT` | (встроенный) | Системный промпт для голосового режима |
| `MAX_HISTORY_TURNS` | `20` | Кол-во пар user/assistant в памяти |
| `ALICE_REPLY_TIMEOUT` | `3.0` | Секунды ожидания Claude перед «думает» |
| `FLASK_HOST` | `127.0.0.1` | Только localhost — за Apache |
| `FLASK_PORT` | `37842` | Порт gunicorn/Flask |
| `FLASK_DEBUG` | `false` | Режим отладки Flask |
| `ALICE_SKILL_TOKEN` | `` | OAuth-токен для верификации (опционально) |
| `DASHBOARD_PASSWORD` | — | Пароль для веб-дашборда |
| `SECRET_KEY` | (random) | Flask session key (задать для стабильности) |
| `DB_PATH` | `dialogs.db` | Путь к SQLite-базе диалогов |

### Настройка таймаута

Если Claude регулярно не укладывается в лимит Алисы:
- Уменьшите `ALICE_REPLY_TIMEOUT` до `2.0`–`2.5` в `.env`
- Уменьшите `CLAUDE_MAX_TOKENS` — короче ответ, быстрее генерация

---

## Диагностика

**GET `/alice`** — JSON-статус без авторизации:
```json
{
  "service": "Alice × Claude AI skill",
  "status": "running",
  "diagnostics": { "api_key_status": "✅ set", ... }
}
```

**Логи сервиса:**
```bash
sudo journalctl -u claudeforalice -f
tail -f /var/log/claudeforalice/error.log
```

---

## Зависимости

- [anthropic](https://pypi.org/project/anthropic/) — официальный Python SDK Anthropic
- [Flask](https://flask.palletsprojects.com/) — веб-фреймворк
- [python-dotenv](https://pypi.org/project/python-dotenv/) — загрузка `.env`
- [gunicorn](https://gunicorn.org/) — WSGI-сервер

---

## Лицензия

MIT
