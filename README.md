# Alice × Claude — навык Яндекс Алисы на базе Claude AI

Навык позволяет пользователям разговаривать с моделью Anthropic Claude прямо через Яндекс Алису.  
Каждый пользователь получает свою историю диалога, которая автоматически обрезается до последних N пар.

---

## Архитектура

```
Alice webhook (POST /alice)
        │
        ▼
  alice_handler.py          ← разбирает запрос, управляет командами
        │
        ├── session_manager.py   ← история диалога + кэш последнего ответа
        ├── pending_store.py     ← фоновые задачи (асинхронный ответ)
        ├── balance_client.py    ← проверка состояния аккаунта
        │
        └── claude_client.py     ← запросы к Anthropic API
                │
                ▼
          Anthropic Claude API
```

| Файл | Назначение |
|------|-----------|
| [`app.py`](app.py) | Flask-приложение: `POST /alice`, `GET /alice` (диагностика), `GET /health` |
| [`alice_handler.py`](alice_handler.py) | Парсинг запроса Alice, голосовые команды, асинхронный ответ |
| [`claude_client.py`](claude_client.py) | Обёртка над `anthropic` SDK, обработка ошибок API |
| [`session_manager.py`](session_manager.py) | In-memory история диалога и кэш последнего ответа |
| [`pending_store.py`](pending_store.py) | Фоновый `ThreadPoolExecutor` для медленных запросов Claude |
| [`balance_client.py`](balance_client.py) | Проверка активности аккаунта через тестовый запрос |
| [`config.py`](config.py) | Все настройки из переменных окружения |

---

## Быстрый старт

### 1. Клонировать / скопировать проект

```bash
git clone <repo-url>
cd <repo-dir>
```

### 2. Создать виртуальное окружение и установить зависимости

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Настроить переменные окружения

```bash
cp .env.example .env
# Открыть .env и вставить ANTHROPIC_API_KEY
```

### 4. Запустить сервер

**Разработка:**
```bash
bash deploy/test_run.sh
```

**Продакшн (установка и запуск через systemd):**
```bash
sudo bash deploy/install.sh
```

**Обновление после изменений:**
```bash
bash deploy/update.sh           # обновить код + перезапустить сервис
bash deploy/update.sh --deps    # + переустановить зависимости
bash deploy/update.sh --apache  # + применить конфиги Apache
bash deploy/update.sh --all     # всё вместе
```

---

## Регистрация навыка в Яндекс Диалогах

1. Зайти на [dialogs.yandex.ru](https://dialogs.yandex.ru/) → **Создать диалог → Навык Алисы**.
2. Задать **фразу активации** (выбрать 1–2 варианта):
   - «Умный Клод» → «Алиса, спроси у умного Клода, …»
   - «Дядя Клод» → «Алиса, спроси у дяди Клода, …»
   - «Доктор Клод» → «Алиса, спроси у доктора Клода, …»
3. В разделе **Webhook URL** указать:  
   `https://claude.dredkin.ru:4443/alice`
4. (Опционально) Скопировать OAuth-токен из настроек навыка в `ALICE_SKILL_TOKEN` в `.env`.
5. Сохранить и опубликовать (или тестировать в режиме черновика).

---

## Голосовые команды

### Обычный вопрос
Любая фраза → ответ от Claude.  
Если Claude не успел ответить за `ALICE_REPLY_TIMEOUT` секунд:  
> «Клод думает над ответом. Спросите: ответ готов?»

### Получить отложенный ответ
> «что ответил», «ответ готов», «что там», «говори», «давай», «слушаю» и др.

### Повторить последний ответ (без расхода токенов)
> «повтори», «ещё раз», «не расслышал», «не расслышала», «скажи ещё раз», «можешь повторить» и др.

### Проверить состояние аккаунта
> «сколько денег», «баланс», «проверь баланс», «работает ли Клод», «проверь Клода» и др.

### Сброс истории диалога
> «сбрось историю», «новый диалог», «начни сначала», «очисти историю»

### Завершить навык
> «стоп», «выход», «хватит», «закрыть»

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
| `FLASK_HOST` | `127.0.0.1` | Адрес прослушивания (только localhost — за Apache) |
| `FLASK_PORT` | `37842` | Порт gunicorn/Flask |
| `FLASK_DEBUG` | `false` | Режим отладки Flask |
| `ALICE_SKILL_TOKEN` | `` | OAuth-токен для верификации (опционально) |

### Настройка таймаута

Если Claude регулярно не укладывается в лимит Алисы (~5 с):
- Уменьшите `ALICE_REPLY_TIMEOUT` до `2.0`–`2.5` в `.env`
- Уменьшите `CLAUDE_MAX_TOKENS` — короче ответ, быстрее генерация
- Пользователь всегда может сказать «что ответил?» для получения полного ответа

---

## Диагностика

**GET `/alice`** в браузере возвращает JSON со статусом сервиса:
```json
{
  "service": "Alice × Claude AI skill",
  "status": "running",
  "diagnostics": {
    "claude_model": "claude-opus-4-5",
    "api_key_status": "✅ set",
    ...
  }
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
- [gunicorn](https://gunicorn.org/) — WSGI-сервер для продакшна

---

## Лицензия

MIT
