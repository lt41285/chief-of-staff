from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.reminder import TaskReminderRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.reminder import ReminderType
from chief_of_staff.models.task_command import TaskIntentKind
from chief_of_staff.services.actual_time import parse_actual_minutes
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.daily_planning import DailyPlanningService, PlanKind
from chief_of_staff.services.lifecycle_format import ALREADY_DONE, ASK_ACTUAL, CANCELLED
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.planning_session import InMemoryPlanningSessionStore
from chief_of_staff.services.reminder_policy import at_kyiv
from chief_of_staff.services.reminders import ReminderService
from chief_of_staff.services.task_command_intent import parse_task_intent_deterministic
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleKind, TaskLifecycleService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.fakes import FakeTaskRepository
from tests.test_reminders import FrozenClock, RecordingNotifier
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

BUDGET = "Поговорити з Тарасом про бюджет"
LETTER = "Підготувати лист до IFC"
ANDRIY = "Call Andriy about the meeting"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture
def repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlAlchemyTaskRepository:
    return SqlAlchemyTaskRepository(session_factory)


def _clock(moment: datetime | None = None) -> Clock:
    return FrozenClock(moment or at_kyiv(date(2026, 9, 2), 12, 0))


def _life(
    repository: SqlAlchemyTaskRepository,
    clock: Clock | None = None,
) -> TaskLifecycleService:
    return TaskLifecycleService(repository, InMemoryLifecycleStore(), clock or _clock())


async def _task(
    repository: SqlAlchemyTaskRepository,
    title: str,
    *,
    user: int = 1,
    chat: int = 1,
    people: tuple[str, ...] = ("Тарас",),
    project: str = "Unity Center",
) -> object:
    return await persist(
        repository,
        complete_draft(
            task_title=title,
            people=people,
            project=project,
            estimated_minutes=20,
            deadline=date(2026, 9, 2),
        ),
        user=user,
        chat=chat,
    )


def test_parses_complete_intents() -> None:
    for text in (
        "виконано поговорити з Тарасом про бюджет",
        "познач задачу про бюджет як виконану",
        "Я вже поговорив з Тарасом про бюджет, познач як виконане.",
        "Завдання поговорити з Тарасом про бюджет — виконано.",
        "готово — підготувати лист до IFC",
        "Done: call Andriy about the meeting.",
        "done call Andriy",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.COMPLETE_TASK


def test_parses_actual_durations() -> None:
    assert parse_actual_minutes("10 хв") == 10
    assert parse_actual_minutes("пів години") == 30
    assert parse_actual_minutes("45 хвилин") == 45
    assert parse_actual_minutes("1 година") == 60
    assert parse_actual_minutes("1 год 20 хв") == 80
    assert parse_actual_minutes("десь 45 хвилин") == 45
    assert parse_actual_minutes("10", allow_bare=True) == 10
    assert parse_actual_minutes("10") is None
    assert parse_actual_minutes("не пам'ятаю") is None
    assert parse_actual_minutes("пропустити") is None


def test_unsupported_intents_are_classified() -> None:
    assert parse_task_intent_deterministic("скасуй задачу про бюджет").kind == (
        TaskIntentKind.CANCEL_TASK
    )
    assert parse_task_intent_deterministic("відклади задачу про бюджет").kind == (
        TaskIntentKind.POSTPONE_TASK
    )
    assert parse_task_intent_deterministic("онови задачу про бюджет").kind == (
        TaskIntentKind.UPDATE_TASK
    )


async def test_exact_match_asks_confirmation_without_db_change(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    life = _life(repository)
    result = await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert BUDGET in result.text
    assert "Unity Center" in result.text
    assert result.show_confirm_buttons
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "inbox"
        assert row.completed_at is None


async def test_fuzzy_natural_reference_resolves(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, BUDGET)
    life = _life(repository)
    result = await life.handle_user_text(
        1, 1, "Я вже поговорив з Тарасом про бюджет, познач як виконане."
    )
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert BUDGET in result.text


async def test_ambiguous_reference_asks_to_choose(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, "Поговорити з Тарасом про бюджет")
    await persist(
        repository,
        complete_draft(
            task_title="Надіслати бюджетний файл Тарасу",
            people=("Тарас",),
            project="Unity Center",
        ),
        user=1,
        chat=1,
    )
    life = _life(repository)
    result = await life.handle_user_text(1, 1, "познач задачу про бюджет як готову")
    assert result.kind == LifecycleKind.ASK_WHICH
    assert result.show_choice_buttons
    assert "Поговорити з Тарасом про бюджет" in result.text
    assert "Надіслати бюджетний файл Тарасу" in result.text


async def test_only_own_tasks_can_be_selected(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await _task(repository, BUDGET, user=1, chat=1)
    await _task(repository, BUDGET, user=2, chat=2)
    life = _life(repository)
    asked = await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    done = await life.confirm(1, 1)
    assert done.kind == LifecycleKind.ASK_ACTUAL
    async with session_factory() as session:
        own = await session.get(TaskRow, mine)
        assert own is not None
        assert own.status == "done"
        others = await repository.list_user_tasks(2)
        assert len(others) == 1
        assert others[0].status != "done"


async def test_done_and_cancelled_excluded_from_open_resolution(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    done_id = await _task(repository, BUDGET)
    cancelled_id = await persist(
        repository,
        complete_draft(task_title=LETTER, people=(), project="IFC"),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            done = await session.get(TaskRow, done_id)
            cancelled = await session.get(TaskRow, cancelled_id)
            assert done and cancelled
            done.status = "done"
            cancelled.status = "cancelled"
    life = _life(repository)
    already = await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    assert already.kind == LifecycleKind.INFO
    assert ALREADY_DONE in already.text
    missing = await life.handle_user_text(1, 1, "готово — підготувати лист до IFC")
    assert missing.kind == LifecycleKind.INFO
    assert "не знайшов" in missing.text.casefold()


async def test_cancel_confirmation_changes_nothing(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    life = _life(repository)
    await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    cancelled = life.cancel(1, 1)
    assert cancelled.kind == LifecycleKind.CANCELLED
    assert cancelled.text == CANCELLED
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "inbox"
        assert row.completed_at is None
        assert row.actual_minutes is None


async def test_confirm_sets_done_and_completed_at(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    moment = at_kyiv(date(2026, 9, 2), 15, 30)
    life = _life(repository, _clock(moment))
    await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    result = await life.confirm(1, 1)
    assert result.kind == LifecycleKind.ASK_ACTUAL
    assert ASK_ACTUAL in result.text
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "done"
        assert row.completed_at is not None
        assert row.actual_minutes is None


async def test_actual_time_saved_and_skip_leaves_null(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _task(repository, BUDGET)
    life = _life(repository)
    await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    await life.confirm(1, 1)
    saved = await life.handle_user_text(1, 1, "пів години")
    assert saved.kind == LifecycleKind.DONE
    async with session_factory() as session:
        row = await session.get(TaskRow, first)
        assert row is not None
        assert row.actual_minutes == 30

    second = await persist(
        repository,
        complete_draft(task_title=LETTER, people=(), project="IFC"),
        user=1,
        chat=1,
    )
    await life.handle_user_text(1, 1, "готово — підготувати лист до IFC")
    await life.confirm(1, 1)
    skipped = await life.handle_user_text(1, 1, "пропустити")
    assert skipped.kind == LifecycleKind.DONE
    async with session_factory() as session:
        row = await session.get(TaskRow, second)
        assert row is not None
        assert row.status == "done"
        assert row.actual_minutes is None


async def test_hours_and_minutes_actual_time(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    life = _life(repository)
    await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    await life.confirm(1, 1)
    await life.handle_user_text(1, 1, "1 год 20 хв")
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.actual_minutes == 80


async def test_combined_completion_and_actual_time(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    life = _life(repository)
    asked = await life.handle_user_text(
        1, 1, "Я поговорив з Тарасом, готово, зайняло 35 хвилин."
    )
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "inbox"
    done = await life.confirm(1, 1)
    assert done.kind == LifecycleKind.DONE
    assert "35 хв" in done.text
    assert ASK_ACTUAL not in done.text
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "done"
        assert row.completed_at is not None
        assert row.actual_minutes == 35


async def test_done_task_disappears_from_today(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, BUDGET)
    other = await persist(
        repository,
        complete_draft(task_title=LETTER, people=(), project="IFC", estimated_minutes=20),
        user=1,
        chat=1,
    )
    life = _life(repository)
    await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    await life.confirm(1, 1)
    planning = DailyPlanningService(repository, InMemoryPlanningSessionStore(), _clock())
    planning.start(1, 1)
    plan = await planning.handle_user_text(1, 1, "3 години")
    assert plan.kind in {PlanKind.PLAN, PlanKind.EMPTY}
    assert BUDGET not in plan.text
    assert LETTER in plan.text or plan.kind == PlanKind.EMPTY
    open_ids = {task.id for task in await repository.list_open_tasks_for_telegram_user(1)}
    assert other in open_ids


async def test_done_task_produces_no_reminders(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(
            task_title=BUDGET,
            people=("Тарас",),
            deadline=date(2026, 9, 2),
            estimated_minutes=20,
        ),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TaskReminderRow(
                    task_id=task_id,
                    reminder_type=ReminderType.DUE_TODAY.value,
                    occurrence_date=date(2026, 9, 2),
                    scheduled_for=at_kyiv(date(2026, 9, 2), 9, 0),
                )
            )
    life = _life(repository)
    await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    await life.confirm(1, 1)
    notifier = RecordingNotifier()
    sent = await ReminderService(
        session_factory, FrozenClock(at_kyiv(date(2026, 9, 2), 9, 20)), notifier
    ).dispatch_due_reminders()
    assert sent == 0
    assert notifier.messages == []


async def test_voice_completion_uses_same_flow(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, ANDRIY, people=("Andriy",), project="Alpha")
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    life = _life(repository)
    voice = VoiceMessageService(
        FakeTranscriber(["done call Andriy"]),
        intake,
        lifecycle=life,
    )
    result = await voice.handle_voice(1, 1, b"ogg")
    assert result.error is None
    assert result.intake is not None
    assert result.intake.kind == LifecycleKind.ASK_CONFIRM
    assert ANDRIY in result.intake.text
    assert interpreter.calls == []


async def test_completion_never_falls_through_to_task_creation(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _task(repository, BUDGET)
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    life = _life(repository)
    result = await process_user_utterance(
        intake,
        1,
        1,
        "Познач задачу про бюджет як готову.",
        lifecycle=life,
    )
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert interpreter.calls == []
    async with session_factory() as session:
        from sqlalchemy import func, select

        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 1


async def test_unsupported_command_does_not_create_task() -> None:
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), FakeTaskRepository())
    life = TaskLifecycleService(FakeTaskRepository(), InMemoryLifecycleStore(), _clock())
    result = await process_user_utterance(
        intake, 1, 1, "відклади задачу про бюджет", lifecycle=life
    )
    assert result.kind == LifecycleKind.INFO
    assert interpreter.calls == []
