# Alice × Claude — навык Яндекс Алисы на базе Claude AI

Навык позволяет пользователям разговаривать с моделью Anthropic Claude прямо через Яндекс Алису.  
Каждый пользователь получает свою историю диалога, которая автоматически обрезается до последних N пар.

---

## Архитектура

```
alice_webhook (POST /alice)
        │
        ▼
  alice_handler.py          ← разбирает запрос Alice, управляет командами
        │
        ├── session_manager.py  ← хранит историю диалога в памяти
        │
        └── claude_client.py    ← отправляет запрос в Anthropic API
                │
                ▼
          Anthropic Claude API
```

| Файл | Назначение |
|------|-----------|
| [`app.py`](app.py) | Flask-приложение, единственный маршрут `POST /alice` |
| [`alice_handler.py`](alice_handler.py) | Парсинг запроса Alice, генерация ответа |
| [`claude_client.py`](claude_client.py) | Обёртка над `anthropic` SDK |
| [`session_manager.py`](session_manager.py) | In-memory хранилище истории по `user_id` |
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
python app.py
```

**Продакшн (gunicorn):**
```bash
gunicorn app:app --bind 127.0.0.1:37842 --workers 4
```

---

## Регистрация навыка в Яндекс Диалогах

1. Зайти на [dialogs.yandex.ru](https://dialogs.yandex.ru/) → **Создать диалог → Навык Алисы**.
2. В разделе **Webhook URL** указать публичный адрес вашего сервера:  
   `https://your-domain.com/alice`
3. (Опционально) Скопировать OAuth-токен из настроек навыка в переменную `ALICE_SKILL_TOKEN` в `.env` — это включит верификацию запросов.
4. Сохранить и опубликовать (или протестировать в режиме черновика).

> **Совет:** для быстрого проброса локального сервера в интернет можно использовать [ngrok](https://ngrok.com/):  
> `ngrok http 37842`

---

## Доступные голосовые команды

| Фраза | Действие |
|-------|---------|
| Любой вопрос | Ответ от Claude |
| «Сбрось историю» / «Новый диалог» / «Начни сначала» / «Очисти историю» | Сброс истории диалога |
| «Стоп» / «Выход» / «Хватит» / «Закрыть» | Завершение сессии |

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
| `FLASK_HOST` | `0.0.0.0` | Адрес прослушивания |
| `FLASK_PORT` | `5000` | Порт |
| `FLASK_DEBUG` | `false` | Режим отладки Flask |
| `ALICE_SKILL_TOKEN` | `` | OAuth-токен для верификации (опционально) |

---

## Зависимости

- [anthropic](https://pypi.org/project/anthropic/) — официальный Python SDK Anthropic
- [Flask](https://flask.palletsprojects.com/) — веб-фреймворк
- [python-dotenv](https://pypi.org/project/python-dotenv/) — загрузка `.env`
- [gunicorn](https://gunicorn.org/) — WSGI-сервер для продакшна

---

## Лицензия

MIT
