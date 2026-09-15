from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.reminder import TaskReminderRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.reminder import ReminderType
from chief_of_staff.models.task_command import TaskIntentKind
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.lifecycle_format import ASK_DEADLINE, CANCELLED, SEPARATE_ACTIONS
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.reminder_policy import at_kyiv
from chief_of_staff.services.reminders import ReminderService
from chief_of_staff.services.task_command_intent import parse_task_intent_deterministic
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleKind, TaskLifecycleService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_reminders import FrozenClock, RecordingNotifier
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

BUDGET = "Поговорити з Тарасом про бюджет"
LETTER = "Підготувати лист до IFC"
VISA = "Поговорити з Наталею про британську візу"


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


def _clock() -> Clock:
    return FrozenClock(at_kyiv(date(2026, 9, 2), 12, 0))


def _life(repository: SqlAlchemyTaskRepository) -> TaskLifecycleService:
    return TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())


async def _task(
    repository: SqlAlchemyTaskRepository,
    title: str,
    *,
    user: int = 1,
    chat: int = 1,
    people: tuple[str, ...] = ("Тарас",),
    project: str = "Unity Center",
    deadline: date = date(2026, 9, 6),
) -> object:
    return await persist(
        repository,
        complete_draft(
            task_title=title,
            people=people,
            project=project,
            estimated_minutes=20,
            deadline=deadline,
        ),
        user=user,
        chat=chat,
    )


def test_parses_postpone_intents() -> None:
    for text in (
        "Перенеси задачу про бюджет на п'ятницю.",
        "Перенеси дедлайн розмови з Тарасом на 5 вересня.",
        "Відклади задачу про візу до наступного понеділка.",
        "Зміни дедлайн задачі про BG на завтра.",
        "Postpone the visa task until September 6.",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.POSTPONE_TASK, text


def test_combined_postpone_and_waiting_is_not_half_applied() -> None:
    parsed = parse_task_intent_deterministic(
        "Перенеси задачу про бюджет на 5 вересня і постав waiting."
    )
    assert parsed is not None
    assert parsed.kind == TaskIntentKind.UPDATE_TASK


async def test_exact_postpone_asks_confirmation(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    life = _life(repository)
    result = await life.handle_user_text(1, 1, "Перенеси задачу про бюджет на 5 вересня.")
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert "Змінити дедлайн" in result.text
    assert BUDGET in result.text
    assert "Було: 6 вересня" in result.text
    assert "Буде: 5 вересня" in result.text
    assert result.show_confirm_buttons
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.deadline == date(2026, 9, 6)
        assert row.status == "inbox"


async def test_ambiguous_postpone_asks_which(repository: SqlAlchemyTaskRepository) -> None:
    await _task(repository, BUDGET)
    await _task(
        repository,
        "Узгодити бюджет з фінансами",
        people=(),
        project="BG",
    )
    result = await _life(repository).handle_user_text(1, 1, "Перенеси задачу про бюджет на п'ятницю.")
    assert result.kind == LifecycleKind.ASK_WHICH
    assert result.show_choice_buttons


async def test_postpone_choice_then_confirm(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _task(repository, BUDGET)
    second = await _task(repository, "Узгодити бюджет з фінансами", people=(), project="BG")
    life = _life(repository)
    asked = await life.handle_user_text(1, 1, "Перенеси задачу про бюджет на 10 вересня.")
    assert asked.kind == LifecycleKind.ASK_WHICH
    preview = await life.choose(1, 1, 0)
    assert preview.kind == LifecycleKind.ASK_CONFIRM
    done = await life.confirm(1, 1)
    assert done.kind == LifecycleKind.DONE
    async with session_factory() as session:
        rows = [
            await session.get(TaskRow, first),
            await session.get(TaskRow, second),
        ]
        deadlines = {row.deadline for row in rows if row is not None}
        assert date(2026, 9, 10) in deadlines
        assert date(2026, 9, 6) in deadlines


async def test_cancel_postpone_changes_nothing(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    life = _life(repository)
    await life.handle_user_text(1, 1, "Перенеси задачу про бюджет на 10 вересня.")
    cancelled = life.cancel(1, 1)
    assert cancelled.kind == LifecycleKind.CANCELLED
    assert cancelled.text == CANCELLED
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.deadline == date(2026, 9, 6)


async def test_relative_friday_in_kyiv(repository: SqlAlchemyTaskRepository) -> None:
    await _task(repository, BUDGET)
    result = await _life(repository).handle_user_text(
        1, 1, "Перенеси задачу про бюджет на п'ятницю."
    )
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert "Буде: 4 вересня" in result.text


async def test_missing_deadline_asks_follow_up(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    life = _life(repository)
    asked = await life.handle_user_text(1, 1, "Перенеси задачу про бюджет.")
    assert asked.kind == LifecycleKind.ASK_DEADLINE
    assert asked.text == ASK_DEADLINE
    preview = await life.handle_user_text(1, 1, "на 10 вересня")
    assert preview.kind == LifecycleKind.ASK_CONFIRM
    assert "Буде: 10 вересня" in preview.text
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.deadline == date(2026, 9, 6)
    done = await life.confirm(1, 1)
    assert done.kind == LifecycleKind.DONE
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.deadline == date(2026, 9, 10)


async def test_voice_follow_up_date(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, VISA, people=("Наталя",), project="Особисте")
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    life = _life(repository)
    voice = VoiceMessageService(
        FakeTranscriber(["Перенеси задачу про візу.", "на 10 вересня"]),
        intake,
        lifecycle=life,
    )
    first = await voice.handle_voice(1, 1, b"ogg")
    assert first.intake is not None
    assert first.intake.kind == LifecycleKind.ASK_DEADLINE
    second = await voice.handle_voice(1, 1, b"ogg")
    assert second.intake is not None
    assert second.intake.kind == LifecycleKind.ASK_CONFIRM
    await life.confirm(1, 1)
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.deadline == date(2026, 9, 10)
    assert interpreter.calls == []


async def test_voice_postpone_same_flow(repository: SqlAlchemyTaskRepository) -> None:
    await _task(repository, VISA, people=("Наталя",), project="Особисте")
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber(["Перенеси задачу про візу на 10 вересня."]),
        intake,
        lifecycle=_life(repository),
    )
    result = await voice.handle_voice(1, 1, b"ogg")
    assert result.intake is not None
    assert result.intake.kind == LifecycleKind.ASK_CONFIRM
    assert interpreter.calls == []


async def test_old_unsent_reminder_does_not_fire_and_new_deadline_is_scheduled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FrozenClock(at_kyiv(date(2026, 9, 2), 12, 0))
    repository = SqlAlchemyTaskRepository(session_factory, clock)
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
                    reminder_type=ReminderType.DAY_BEFORE.value,
                    occurrence_date=date(2026, 9, 1),
                    scheduled_for=at_kyiv(date(2026, 9, 1), 18, 0),
                )
            )
    life = TaskLifecycleService(repository, InMemoryLifecycleStore(), clock)
    await life.handle_user_text(1, 1, "Перенеси задачу про бюджет на 10 вересня.")
    await life.confirm(1, 1)
    notifier = RecordingNotifier()
    old_window = await ReminderService(
        session_factory, FrozenClock(at_kyiv(date(2026, 9, 1), 18, 30)), notifier
    ).dispatch_due_reminders()
    assert old_window == 0
    assert notifier.messages == []
    first = await ReminderService(
        session_factory, FrozenClock(at_kyiv(date(2026, 9, 9), 18, 30)), notifier
    ).dispatch_due_reminders()
    assert first == 1
    second = await ReminderService(
        session_factory, FrozenClock(at_kyiv(date(2026, 9, 9), 18, 40)), notifier
    ).dispatch_due_reminders()
    assert second == 0
    async with session_factory() as session:
        sent = await session.scalar(
            select(func.count())
            .select_from(TaskReminderRow)
            .where(
                TaskReminderRow.task_id == task_id,
                TaskReminderRow.sent_at.is_not(None),
            )
        )
        unsent = await session.scalar(
            select(func.count())
            .select_from(TaskReminderRow)
            .where(
                TaskReminderRow.task_id == task_id,
                TaskReminderRow.sent_at.is_(None),
            )
        )
        assert sent == 1
        assert unsent == 0


async def test_postpone_does_not_enter_intake(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, BUDGET)
    interpreter = ScriptedInterpreter([])
    result = await process_user_utterance(
        TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository),
        1,
        1,
        "Відклади задачу про бюджет на завтра.",
        lifecycle=_life(repository),
    )
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert interpreter.calls == []


async def test_combined_command_does_not_change_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    interpreter = ScriptedInterpreter([])
    result = await process_user_utterance(
        TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository),
        1,
        1,
        "Перенеси задачу про бюджет на 5 вересня і постав waiting.",
        lifecycle=_life(repository),
    )
    assert result.kind == LifecycleKind.INFO
    assert result.text == SEPARATE_ACTIONS
    assert interpreter.calls == []
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.deadline == date(2026, 9, 6)
        assert row.status == "inbox"


async def test_other_user_cannot_postpone(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET, user=11, chat=110)
    result = await _life(repository).handle_user_text(
        12, 120, "Перенеси задачу про бюджет на 10 вересня."
    )
    assert result.kind == LifecycleKind.INFO
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.deadline == date(2026, 9, 6)
