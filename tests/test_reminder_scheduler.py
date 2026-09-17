"""Application lifecycle for the deadline reminder scheduler (PTB 21.x)."""

import asyncio
import inspect

import pytest
from telegram.ext import Application

from chief_of_staff.bot.app import (
    REMINDER_TASK_KEY,
    REMINDER_TASK_NAME,
    _install_reminder_lifecycle,
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


async def test_lifecycle_hooks_start_after_running_and_cancel_before_stop() -> None:
    app = FakeApplication(running=False)
    _install_reminder_lifecycle(app)  # type: ignore[arg-type]
    await app.start()
    assert app.start_calls == 1
    assert app.running is True
    task = app.bot_data[REMINDER_TASK_KEY]
    assert isinstance(task, asyncio.Task)
    assert not task.done()
    assert app.create_task_calls == []
    await app.stop()
    assert app.stop_calls == 1
    assert task.cancelled()
    assert REMINDER_TASK_KEY not in app.bot_data


def test_build_application_uses_post_init_not_create_task_in_startup_source() -> None:
    from chief_of_staff import bot as bot_pkg
    from chief_of_staff.bot import app as app_mod

    source = inspect.getsource(app_mod.build_application)
    assert ".post_init(_on_post_init)" in source
    assert "create_task" not in source
    init_source = inspect.getsource(app_mod._on_post_init)
    assert "create_task" not in init_source
    assert app_mod.start_reminder_scheduler is not None
    assert bot_pkg.build_application is app_mod.build_application


def test_reminder_loop_is_not_started_via_application_create_task() -> None:
    from chief_of_staff.bot import app as app_mod

    start_source = inspect.getsource(app_mod.start_reminder_scheduler)
    assert "asyncio.create_task" in start_source
    assert "application.create_task" not in start_source
