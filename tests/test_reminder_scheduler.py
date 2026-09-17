"""Application lifecycle for the deadline reminder scheduler (PTB 21.x)."""

import asyncio
import inspect
import sys

import pytest
from telegram.ext import Application

from chief_of_staff.bot.app import (
    REMINDER_TASK_KEY,
    REMINDER_TASK_NAME,
    ChiefOfStaffApplication,
    _on_post_init,
    start_reminder_scheduler,
    stop_reminder_scheduler,
)


class FakeApplication:
    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.bot = object()
        self.bot_data: dict[str, object] = {"reminder_poll_seconds": 60}
        self.create_task_calls: list[str | None] = []
        self.start_calls = 0
        self.stop_calls = 0

    def create_task(self, coroutine: object, name: str | None = None) -> None:
        self.create_task_calls.append(name)
        if inspect.iscoroutine(coroutine):
            coroutine.close()

    async def start(self) -> None:
        self.start_calls += 1
        self.running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False


def test_ptb_version_has_post_init_stop_shutdown_but_no_post_start() -> None:
    sig = inspect.signature(Application.__init__)
    assert "post_init" in sig.parameters
    assert "post_stop" in sig.parameters
    assert "post_shutdown" in sig.parameters
    assert "post_start" not in sig.parameters


def test_create_task_warns_when_application_is_not_running() -> None:
    source = inspect.getsource(Application._Application__create_task)
    collapsed = " ".join(source.split())
    assert "if self.running:" in source
    assert "automatically awaited" in collapsed
    assert "create_task" in collapsed


async def test_post_init_does_not_create_a_scheduler_task() -> None:
    app = FakeApplication()
    await _on_post_init(app)  # type: ignore[arg-type]
    assert app.create_task_calls == []
    assert app.bot_data.get(REMINDER_TASK_KEY) is None
    assert app.bot_data["reminders"] is not None


async def test_scheduler_requires_running_application() -> None:
    app = FakeApplication(running=False)
    with pytest.raises(RuntimeError, match="after Application.start"):
        start_reminder_scheduler(app)  # type: ignore[arg-type]


async def test_scheduler_starts_once_and_cancels_on_stop() -> None:
    app = FakeApplication(running=True)
    first = start_reminder_scheduler(app)  # type: ignore[arg-type]
    second = start_reminder_scheduler(app)  # type: ignore[arg-type]
    assert first is second
    assert first.get_name() == REMINDER_TASK_NAME
    assert app.bot_data[REMINDER_TASK_KEY] is first
    assert not first.done()
    await stop_reminder_scheduler(app)  # type: ignore[arg-type]
    assert first.cancelled()
    assert REMINDER_TASK_KEY not in app.bot_data
    await stop_reminder_scheduler(app)  # type: ignore[arg-type]


async def test_subclass_starts_scheduler_after_running_and_cancels_before_stop() -> None:
    start_source = inspect.getsource(ChiefOfStaffApplication.start)
    stop_source = inspect.getsource(ChiefOfStaffApplication.stop)
    assert "await super().start()" in start_source
    assert "start_reminder_scheduler(self)" in start_source
    assert start_source.index("await super().start()") < start_source.index(
        "start_reminder_scheduler(self)"
    )
    assert "await stop_reminder_scheduler(self)" in stop_source
    assert "await super().stop()" in stop_source
    assert stop_source.index("await stop_reminder_scheduler(self)") < stop_source.index(
        "await super().stop()"
    )


def test_ptb_application_start_and_stop_are_read_only() -> None:
    """Python 3.13 slotted Application rejects instance assignment of start/stop.

    That is the production crash: wrapping Application.start after build().
    """
    if sys.version_info < (3, 13):
        pytest.skip("Application.start is instance-assignable before Python 3.13")
    app = Application.builder().token("1:AA").build()
    with pytest.raises(AttributeError, match="read-only"):
        app.start = app.start  # type: ignore[method-assign]
    with pytest.raises(AttributeError, match="read-only"):
        app.stop = app.stop  # type: ignore[method-assign]


def test_builder_returns_chief_of_staff_application() -> None:
    app = (
        Application.builder()
        .application_class(ChiefOfStaffApplication)
        .token("1:AA")
        .build()
    )
    assert type(app) is ChiefOfStaffApplication


def test_build_application_uses_post_init_not_create_task_in_startup_source() -> None:
    from chief_of_staff import bot as bot_pkg
    from chief_of_staff.bot import app as app_mod

    source = inspect.getsource(app_mod.build_application)
    assert ".post_init(_on_post_init)" in source
    assert ".application_class(ChiefOfStaffApplication)" in source
    assert "create_task" not in source
    assert "application.start =" not in inspect.getsource(app_mod)
    assert "application.stop =" not in inspect.getsource(app_mod)
    init_source = inspect.getsource(app_mod._on_post_init)
    assert "create_task" not in init_source
    assert app_mod.start_reminder_scheduler is not None
    assert bot_pkg.build_application is app_mod.build_application


def test_reminder_loop_is_not_started_via_application_create_task() -> None:
    from chief_of_staff.bot import app as app_mod

    start_source = inspect.getsource(app_mod.start_reminder_scheduler)
    assert "asyncio.create_task" in start_source
    assert "application.create_task" not in start_source
