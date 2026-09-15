from collections.abc import AsyncIterator
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.person import PersonRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.task_command import TaskIntentKind
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.daily_planning import DailyPlanningService, PlanKind
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.planning_session import InMemoryPlanningSessionStore
from chief_of_staff.services.reminder_policy import at_kyiv
from chief_of_staff.services.task_command_intent import parse_task_intent_deterministic
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleKind, TaskLifecycleService
from chief_of_staff.services.task_query import QueryKind, TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_reminders import FrozenClock
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

BUDGET = "Поговорити з Тарасом про бюджет"
VISA = "Поговорити з Наталею про британську візу"
IFC = "Підготувати лист до IFC"
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
) -> object:
    return await persist(
        repository,
        complete_draft(
            task_title=title,
            people=people,
            project=project,
            estimated_minutes=20,
            deadline=date(2026, 9, 6),
        ),
        user=user,
        chat=chat,
    )


def test_parses_waiting_and_resume_intents() -> None:
    for text in (
        "Постав задачу про бюджет у waiting.",
        "По візі чекаю відповідь від Наталі.",
        "Чекаю від Тараса бюджет.",
        "Задача з Андрієм зараз waiting.",
        "Mark the IFC task as waiting.",
        "Waiting for Andriy.",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.WAITING_TASK, text
    for text in (
        "Наталя відповіла, поверни задачу про візу в роботу.",
        "Більше не waiting по задачі BG.",
        "Resume the IFC task.",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.RESUME_TASK, text


async def test_waiting_does_not_enter_intake(repository: SqlAlchemyTaskRepository) -> None:
    await _task(repository, VISA, people=("Наталя Тарновська",), project="Особисте")
    interpreter = ScriptedInterpreter([])
    result = await process_user_utterance(
        TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository),
        1,
        1,
        "Чекаю відповідь від Наталі",
        lifecycle=_life(repository),
    )
    assert interpreter.calls == []
    assert result.kind == LifecycleKind.ASK_CONFIRM


async def test_waiting_resolution_and_confirm(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(
        repository, VISA, people=("Наталя Тарновська",), project="Особисте"
    )
    life = _life(repository)
    asked = await life.handle_user_text(1, 1, "По візі чекаю відповідь від Наталі.")
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    assert "Перевести у Waiting" in asked.text
    assert VISA in asked.text
    assert "Чекаємо від: Наталя Тарновська" in asked.text
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "inbox"
        assert row.waiting_for_person_id is None
    done = await life.confirm(1, 1)
    assert done.kind == LifecycleKind.DONE
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "waiting"
        assert row.waiting_for_person_id is not None
        person = await session.get(PersonRow, row.waiting_for_person_id)
        assert person is not None
        assert person.name == "Наталя Тарновська"


async def test_waiting_without_person_is_null(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET)
    life = _life(repository)
    asked = await life.handle_user_text(1, 1, "Постав задачу про бюджет у waiting.")
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    assert "Чекаємо від: —" in asked.text
    await life.confirm(1, 1)
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "waiting"
        assert row.waiting_for_person_id is None


async def test_waiting_ambiguity(repository: SqlAlchemyTaskRepository) -> None:
    await _task(repository, BUDGET)
    await _task(repository, "Узгодити бюджет з фінансами", people=(), project="BG")
    result = await _life(repository).handle_user_text(1, 1, "Постав задачу про бюджет у waiting.")
    assert result.kind == LifecycleKind.ASK_WHICH


async def test_waiting_new_person_created(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, IFC, people=(), project="BG")
    life = _life(repository)
    await life.handle_user_text(1, 1, "Mark the IFC task as waiting.")
    await life.confirm(1, 1)
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "waiting"
        assert row.waiting_for_person_id is None


async def test_waiting_excluded_from_today_but_listed(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _task(repository, BUDGET)
    await _task(repository, IFC, people=(), project="BG")
    life = _life(repository)
    await life.handle_user_text(1, 1, "Постав задачу про бюджет у waiting.")
    await life.confirm(1, 1)
    open_today = await repository.list_open_tasks_for_telegram_user(1)
    listed = await repository.list_user_tasks(1)
    assert not any(task.title == BUDGET for task in open_today)
    assert any(task.title == BUDGET and task.status == "waiting" for task in listed)
    assert any(task.title == IFC for task in listed)
    interpreter = ScriptedInterpreter([])
    shown = await process_user_utterance(
        TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository),
        1,
        1,
        "Покажи всі задачі",
        queries=TaskQueryService(repository),
    )
    assert shown.kind == QueryKind.TASKS
    assert BUDGET in shown.text
    assert "📌 Статус: waiting" in shown.text
    planning = DailyPlanningService(repository, InMemoryPlanningSessionStore(), _clock())
    planning.start(1, 1)
    plan = await planning.handle_user_text(1, 1, "3 години")
    assert plan.kind in {PlanKind.PLAN, PlanKind.EMPTY}
    assert BUDGET not in plan.text


async def test_other_user_cannot_set_waiting(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(repository, BUDGET, user=11, chat=110)
    result = await _life(repository).handle_user_text(12, 120, "Постав задачу про бюджет у waiting.")
    assert result.kind == LifecycleKind.INFO
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "inbox"


async def test_resume_waiting_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _task(
        repository, VISA, people=("Наталя Тарновська",), project="Особисте"
    )
    life = _life(repository)
    await life.handle_user_text(1, 1, "По візі чекаю відповідь від Наталі.")
    await life.confirm(1, 1)
    asked = await life.handle_user_text(
        1, 1, "Наталя відповіла, поверни задачу про візу в роботу."
    )
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    assert "Повернути в роботу" in asked.text
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "waiting"
    done = await life.confirm(1, 1)
    assert done.kind == LifecycleKind.DONE
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "next"
        assert row.waiting_for_person_id is None
        assert row.completed_at is None


async def test_voice_waiting_and_resume(repository: SqlAlchemyTaskRepository) -> None:
    await _task(repository, IFC, people=("Andriy",), project="BG")
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    life = _life(repository)
    wait_voice = VoiceMessageService(
        FakeTranscriber(["Mark the IFC task as waiting."]),
        intake,
        lifecycle=life,
    )
    waited = await wait_voice.handle_voice(1, 1, b"ogg")
    assert waited.intake is not None
    assert waited.intake.kind == LifecycleKind.ASK_CONFIRM
    await life.confirm(1, 1)
    resume_voice = VoiceMessageService(
        FakeTranscriber(["Resume the IFC task."]),
        intake,
        lifecycle=life,
    )
    resumed = await resume_voice.handle_voice(1, 1, b"ogg")
    assert resumed.intake is not None
    assert resumed.intake.kind == LifecycleKind.ASK_CONFIRM
    assert interpreter.calls == []
