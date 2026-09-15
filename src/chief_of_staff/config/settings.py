"""Typed environment configuration (pydantic-settings + python-dotenv)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Process-wide settings from the environment and the project-root `.env`."""

    model_config = SettingsConfigDict(
        env_file=(_PROJECT_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str = Field(..., min_length=1, description="Token issued by @BotFather")
    openai_api_key: str | None = Field(
        default=None,
        description="OpenAI API key (required when the Responses client is used)",
    )
    openai_model: str = Field(default="gpt-4.1", description="Responses API model id")
    openai_transcription_model: str = Field(
        default="gpt-4o-mini-transcribe",
        description="Speech-to-text model for Telegram voice messages",
    )
    database_url: str | None = Field(
        default=None,
        description="Postgres URL; async and sync driver prefixes are accepted",
    )
    reminder_poll_seconds: int = Field(default=120, ge=60, le=300)
    log_level: str = Field(default="INFO", min_length=1, description="Loguru / process log level")

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
        return value

    def require_openai_api_key(self) -> str:
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        return self.openai_api_key

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        return self.database_url

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy asyncio URL (`postgresql+asyncpg://`)."""
        return _to_asyncpg_url(self.require_database_url())

    @property
    def sync_database_url(self) -> str:
        """Alembic / psycopg URL (`postgresql+psycopg://`)."""
        return _to_psycopg_url(self.require_database_url())


def _strip_driver(url: str) -> str:
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
        "postgresql://",
    ):
        if url.startswith(prefix):
            return url.removeprefix(prefix)
    raise ValueError("DATABASE_URL must be a PostgreSQL connection string")


def _to_asyncpg_url(url: str) -> str:
    return f"postgresql+asyncpg://{_strip_driver(url)}"


def _to_psycopg_url(url: str) -> str:
    return f"postgresql+psycopg://{_strip_driver(url)}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
