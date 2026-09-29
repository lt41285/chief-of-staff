# Chief of Staff

Telegram AI assistant for task management (Ukrainian/English, text and voice): tasks inside projects, people and aliases, deadlines and reminders, `/today` daily plan, complete / postpone / waiting lifecycle, project archive. Deployed on Railway from `main`.

The AI router interprets each message; Python services perform every write and build every confirmation. Working rules for contributors and AI agents live in `CLAUDE.md`.

## Architecture

| Layer | Package | Responsibility |
| --- | --- | --- |
| Composition | `chief_of_staff.main` | Logging, settings, polling |
| Presentation | `chief_of_staff.bot` | Telegram updates |
| Application | `chief_of_staff.services` | Use-cases: intake, lifecycle, queries, planning, reminders |
| Domain | `chief_of_staff.models` | Pydantic entities, no I/O |
| Infrastructure | `chief_of_staff.infrastructure` | Postgres and OpenAI adapters |
| Config / prompts | `chief_of_staff.config`, `chief_of_staff.prompts` | Env, logging, LLM instructions |


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
pip install -e .
cp .env.example .env
```

Set `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, and `DATABASE_URL`.

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

Send `/start`, a task in plain text, or a voice message.

## Environment variables

| Variable | Required to start | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | Bot API token |
| `OPENAI_API_KEY` | yes | OpenAI Responses API |
| `OPENAI_MODEL` | no | Model id (default `gpt-4.1`) |
| `DATABASE_URL` | yes | Postgres URL |
| `OPENAI_TRANSCRIPTION_MODEL` | no | Voice model (default `gpt-4o-mini-transcribe`) |
| `REMINDER_POLL_SECONDS` | no | Reminder scan interval, 60–300 (default 120) |
| `LOG_LEVEL` | no | Loguru level (default `INFO`) |
