import logging

from chief_of_staff.config.log import configure_logging, redact_log_text


_TOKEN = "123456789:AAHexampleTelegramBotTokenValue99"
_OPENAI = "sk-proj-abcdefghijklmnopqrstuvwxyz012345"
_DB = "postgresql+asyncpg://postgres.abc:super-secret@aws-0.pooler.supabase.com:6543/postgres"


def test_redacts_telegram_getupdates_url() -> None:
    raw = (
        f'HTTP Request: GET https://api.telegram.org/bot{_TOKEN}/getUpdates '
        '"HTTP/1.1 409 Conflict"'
    )
    redacted = redact_log_text(raw)
    assert _TOKEN not in redacted
    assert "https://api.telegram.org/bot<redacted>/getUpdates" in redacted


def test_redacts_openai_key_and_database_url() -> None:
    raw = f"openai_api_key={_OPENAI} DATABASE_URL={_DB}"
    redacted = redact_log_text(raw)
    assert _OPENAI not in redacted
    assert "super-secret" not in redacted
    assert _DB not in redacted


def test_http_client_loggers_are_warning_even_when_app_is_debug() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("openai._base_client").level == logging.WARNING
