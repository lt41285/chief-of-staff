# Chief of Staff

Telegram AI assistant for task management. Skeleton only: the bot replies `Chief of Staff is running`. Task logic is not implemented.

## Architecture

| Layer | Package | Responsibility |
| --- | --- | --- |
| Composition | `chief_of_staff.main` | Logging, settings, polling |
| Presentation | `chief_of_staff.bot` | Telegram updates |
| Application | `chief_of_staff.services` | Use-cases (health now; tasks later) |
| Domain | `chief_of_staff.models` | Pydantic entities, no I/O |
| Infrastructure | `chief_of_staff.infrastructure` | Postgres and OpenAI adapters |
| Config / prompts | `chief_of_staff.config`, `chief_of_staff.prompts` | Env, logging, LLM instructions |

The bot process imports presentation, config, and the health service only. Database and OpenAI stay unused until those use-cases exist.

```
.
├── .env.example
├── .gitignore
├── alembic.ini
├── alembic/
├── pyproject.toml
├── requirements.txt
├── README.md
└── src/chief_of_staff/
```

## Requirements

- Python 3.12+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Optional: OpenAI API key, Supabase (or other) Postgres

## Setup

```bash
cd "Chief of staff"
python3.12 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
cp .env.example .env
```

Set at least `TELEGRAM_BOT_TOKEN`. OpenAI and `DATABASE_URL` are not required to start the bot.

When you persist data, use any of:

```text
postgresql+asyncpg://postgres.[project-ref]:[PASSWORD]@aws-0-[region].pooler.supabase.com:6543/postgres
postgresql://postgres.[project-ref]:[PASSWORD]@aws-0-[region].pooler.supabase.com:6543/postgres
```

The app normalizes these to `asyncpg` (runtime) and `psycopg` (Alembic).

```bash
alembic upgrade head
python -m chief_of_staff
# or: chief-of-staff
```

Send `/start` or any text. The bot replies `Chief of Staff is running`.

## Environment variables

| Variable | Required to start | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | Bot API token |
| `OPENAI_API_KEY` | no | OpenAI Responses API |
| `OPENAI_MODEL` | no | Model id (default `gpt-4.1`) |
| `DATABASE_URL` | no | Postgres URL |
| `LOG_LEVEL` | no | Loguru level (default `INFO`) |

## File map

| Path | Role |
| --- | --- |
| `.gitignore` | venv, `.env`, bytecode, caches |
| `.env.example` | Secret template |
| `requirements.txt` | Installs this package (Nixpacks/Railway `pip install -r`) |
| `pyproject.toml` | src-layout package, runtime deps, and `chief-of-staff` script |
| `alembic.ini` | Alembic; DB URL is set from Settings |
| `alembic/env.py` | Loads metadata and the sync database URL |
| `alembic/script.py.mako` | New revision template |
| `alembic/versions/0001_create_users.py` | Initial `users` table |
| `src/chief_of_staff/__init__.py` | Package version |
| `src/chief_of_staff/__main__.py` | `python -m chief_of_staff` |
| `src/chief_of_staff/main.py` | Process entry |
| `src/chief_of_staff/py.typed` | Typed package marker |
| `config/settings.py` | Pydantic Settings; project-root `.env` |
| `config/log.py` | Loguru (not named `logging.py`, so stdlib is not shadowed) |
| `models/user.py` | Domain `User` |
| `services/health.py` | Running-message use-case |
| `infrastructure/database/base.py` | SQLAlchemy `DeclarativeBase` |
| `infrastructure/database/session.py` | Singleton async engine and session |
| `infrastructure/database/orm/user.py` | `users` table (`UserRow`) |
| `infrastructure/openai/client.py` | Async Responses API client |
| `prompts/system.py` | Future system instructions |
| `bot/app.py` | Application factory |
| `bot/handlers/start.py` | `/start` and text → running message |
