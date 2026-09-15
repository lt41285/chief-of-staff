from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.pool import NullPool

from chief_of_staff.infrastructure.database import session as session_mod
from chief_of_staff.infrastructure.database.session import (
    asyncpg_pooler_connect_args,
    asyncpg_pooler_engine_kwargs,
    create_async_engine_from_url,
    get_engine,
)


def test_asyncpg_connect_args_disable_prepared_statement_caches() -> None:
    args = asyncpg_pooler_connect_args()
    assert args["prepared_statement_cache_size"] == 0
    assert args["statement_cache_size"] == 0
    names = {args["prepared_statement_name_func"]() for _ in range(8)}
    assert len(names) == 8
    assert all(name.startswith("__asyncpg_") for name in names)


def test_async_engine_uses_null_pool_and_pooler_connect_args() -> None:
    url = "postgresql+asyncpg://user:secret@aws-0-eu.pooler.supabase.com:6543/postgres"
    engine = create_async_engine_from_url(url)
    try:
        assert engine.sync_engine.url.render_as_string(hide_password=False).startswith(
            "postgresql+asyncpg://"
        )
        assert "prepared_statement_cache_size" not in str(engine.sync_engine.url)
        assert isinstance(engine.sync_engine.pool, NullPool)
        kwargs = asyncpg_pooler_engine_kwargs()
        assert kwargs["poolclass"] is NullPool
        assert kwargs["connect_args"]["prepared_statement_cache_size"] == 0
    finally:
        engine.sync_engine.dispose()


def test_get_engine_does_not_alter_database_url(monkeypatch) -> None:
    captured: dict[str, object] = {}
    url = "postgresql+asyncpg://user:secret@host:6543/postgres"

    def fake_create(passed_url: str, **kwargs: object) -> MagicMock:
        captured["url"] = passed_url
        captured["kwargs"] = kwargs
        return MagicMock()

    monkeypatch.setattr(session_mod, "create_async_engine", fake_create)
    monkeypatch.setattr(
        session_mod,
        "get_settings",
        lambda: SimpleNamespace(async_database_url=url),
    )
    get_engine.cache_clear()
    try:
        get_engine()
        assert captured["url"] == url
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["poolclass"] is NullPool
        connect_args = kwargs["connect_args"]
        assert connect_args["prepared_statement_cache_size"] == 0
        assert connect_args["statement_cache_size"] == 0
        assert callable(connect_args["prepared_statement_name_func"])
    finally:
        get_engine.cache_clear()
