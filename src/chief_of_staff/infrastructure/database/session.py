"""Async SQLAlchemy engine and session factory for Supabase Postgres.

Engine and sessionmaker are process singletons. Do not call these from bot
handlers until persistence is wired.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from chief_of_staff.config.settings import get_settings


def _prepared_statement_name() -> str:
    """Unique per prepare so PgBouncer transaction mode cannot collide names."""
    return f"__asyncpg_{uuid4().hex}__"


def asyncpg_pooler_connect_args() -> dict[str, Any]:
    """DBAPI kwargs for SQLAlchemy's asyncpg dialect (PgBouncer transaction mode).

    ``prepared_statement_cache_size`` is a SQLAlchemy asyncpg DBAPI argument
    (not a dialect kwarg). ``0`` disables that cache. Remaining keys are passed
    through to ``asyncpg.connect``; ``statement_cache_size=0`` disables asyncpg's
    own LRU. Unique statement names avoid ``DuplicatePreparedStatementError``
    when backends are reused.
    """
    return {
        "prepared_statement_cache_size": 0,
        "statement_cache_size": 0,
        "prepared_statement_name_func": _prepared_statement_name,
    }


def asyncpg_pooler_engine_kwargs() -> Mapping[str, Any]:
    """PgBouncer already pools; SQLAlchemy must not keep a second pool."""
    return {
        "poolclass": NullPool,
        "pool_pre_ping": True,
        "connect_args": asyncpg_pooler_connect_args(),
    }


def create_async_engine_from_url(url: str) -> AsyncEngine:
    return create_async_engine(url, **asyncpg_pooler_engine_kwargs())


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return create_async_engine_from_url(get_settings().async_database_url)


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
