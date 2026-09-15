"""Startup/config: Settings must keep fields that main.py reads."""

from chief_of_staff.config.log import configure_logging
from chief_of_staff.config.settings import Settings
from chief_of_staff.main import main


def test_log_level_defaults_to_info_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    settings = Settings(_env_file=None, telegram_bot_token="test-token")
    assert settings.log_level == "INFO"


def test_log_level_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    settings = Settings(_env_file=None, telegram_bot_token="test-token")
    assert settings.log_level == "DEBUG"


def test_startup_configure_logging_uses_settings_log_level(monkeypatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    settings = Settings(_env_file=None, telegram_bot_token="test-token")
    configure_logging(settings.log_level)

    captured: dict[str, object] = {}

    class FakeApp:
        def run_polling(self, **kwargs: object) -> None:
            captured["started"] = True

    monkeypatch.setattr("chief_of_staff.main.get_settings", lambda: settings)
    monkeypatch.setattr("chief_of_staff.main.configure_logging", lambda level: captured.setdefault("level", level))
    monkeypatch.setattr("chief_of_staff.main.build_application", lambda _settings: FakeApp())

    main()
    assert captured["level"] == settings.log_level
    assert captured["started"] is True
