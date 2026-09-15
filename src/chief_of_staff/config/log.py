"""Loguru setup used by the bot process."""

import logging
import re
import sys

from loguru import logger

_NOISY_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.http11",
    "httpcore.connection",
    "httpcore.proxy",
    "telegram.ext._updater",
    "telegram.ext._application",
    "openai",
    "openai._base_client",
)

_TELEGRAM_BOT_URL = re.compile(
    r"(https://api\.telegram\.org/bot)([^/\s]+)",
    re.IGNORECASE,
)
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_DB_URL = re.compile(
    r"(postgresql(?:\+\w+)?://[^:\s/]+:)([^@\s]+)(@)",
    re.IGNORECASE,
)
_ENV_SECRET = re.compile(
    r"(?i)\b(telegram_bot_token|bot_token|openai_api_key|api_key|database_url|password)\s*[=:]\s*\S+"
)


def redact_log_text(message: str) -> str:
    """Strip tokens, API keys, and DB passwords from a log line."""
    text = _TELEGRAM_BOT_URL.sub(r"\1<redacted>", message)
    text = _TELEGRAM_TOKEN.sub("<redacted-telegram-token>", text)
    text = _OPENAI_KEY.sub("<redacted-openai-key>", text)
    text = _DB_URL.sub(r"\1<redacted>\3", text)
    text = _ENV_SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return text


class InterceptHandler(logging.Handler):
    """Forward stdlib logging (telegram, sqlalchemy, alembic) into Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, redact_log_text(record.getMessage())
        )


def _patch_loguru(record: dict) -> None:
    record["message"] = redact_log_text(str(record["message"]))


def configure_logging(level: str = "INFO") -> None:
    logger.remove()
    logger.configure(patcher=_patch_loguru)
    logger.add(
        sys.stderr,
        level=level.upper(),
        backtrace=True,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
    )
    app_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(handlers=[InterceptHandler()], level=app_level, force=True)
    for name in _NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
