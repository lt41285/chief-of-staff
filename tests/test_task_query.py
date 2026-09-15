from collections.abc import AsyncIterator
from datetime import date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task import Importance, Urgency
from chief_of_staff.models.task_command import TaskIntentKind
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.task_command_intent import parse_task_intent_deterministic
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_query import QueryKind, TaskQueryService
from chief_of_staff.services.task_query_format import MAX_LISTED_TASKS, format_all_tasks_grouped
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.fakes import FakeTaskRepository
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

LIST_PHRASES = (
    "список всіх завдань по проєкту BG",
    "Покажи всі завдання по проєкту BG",
    "Покажи всі таски по проєкту BG",
    "show all tasks for BG",
    "list tasks for BG",
    "Які задачі в Unity Center?",
    "Покажи мені всі таски по проектах",
    "Покажи всі задачі",
    "show all tasks",
)


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


def test_deterministic_list_intents() -> None:
    for text in (
        "список всіх завдань по проєкту BG",
        "Покажи всі завдання по проєкту BG",
        "Покажи всі таски по проєкту BG",
        "show all tasks for BG",
        "list tasks for BG",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.LIST_PROJECT_TASKS, text
        assert parsed.project_query is not None
        assert "bg" in parsed.project_query.casefold()
    unity = parse_task_intent_deterministic("Які задачі в Unity Center?")
    assert unity is not None
    assert unity.kind == TaskIntentKind.LIST_PROJECT_TASKS
    assert unity.project_query == "Unity Center"
    for text in (
        "Покажи мені всі таски по проектах",
        "Покажи всі задачі",
        "Покажи всі мої таски",
        "show all tasks",
        "show me all tasks",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.LIST_ALL_TASKS, text


ALL_PROJECTS_PHRASES = (
    "Покажи всі таски по всіх проєктах.",
    "Покажи всі задачі у всіх проєктах.",
    "Які в мене задачі по всіх проектах?",
    "Дай список завдань з усіх проєктів.",
    "Покажи всі мої таски.",
    "Show all tasks across all projects.",
    "Покажи всі таски по всім проєктам",
    "Покажи всі задачі в усіх проєктах",
    "Покажи всі таски по всіх моїх проєктах",
    "Покажи завдання за всіма проєктами",
    "Show all tasks from all projects.",
    "Show all tasks for all projects.",
)

SCOPED_PROJECT_PHRASES = (
    ("Покажи всі таски по проєкту BG.", "bg"),
    ("Які задачі в Unity Center?", "unity center"),
    ("Покажи завдання по проєкту Особисте.", "особисте"),
)


def test_all_projects_phrases_are_list_all_tasks() -> None:
    for text in ALL_PROJECTS_PHRASES:
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.LIST_ALL_TASKS, text
        assert parsed.project_query is None, text


def test_scoped_project_phrases_stay_list_project_tasks() -> None:
    for text, needle in SCOPED_PROJECT_PHRASES:
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.LIST_PROJECT_TASKS, text
        assert parsed.project_query is not None, text
        assert needle in parsed.project_query.casefold(), text


def test_generic_quantifiers_are_not_project_names() -> None:
    for text in (
        "Покажи всі таски по всіх проєктах",
        "list tasks for всіх проєктах",
        "Покажи задачі в усіх проєктах",
        "Покажи завдання по всім проєктам",
        "Show tasks for all projects",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.LIST_ALL_TASKS, text
        assert parsed.project_query is None, text


def test_add_task_is_not_a_list_query() -> None:
    assert parse_task_intent_deterministic("Додай задачу по проєктах BG") is None


async def _ask(
    repository: SqlAlchemyTaskRepository,
    text: str,
    *,
    user: int = 1,
    chat: int = 1,
) -> tuple[object, ScriptedInterpreter]:
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    result = await process_user_utterance(
        intake,
        user,
        chat,
        text,
        queries=TaskQueryService(repository),
    )
    return result, interpreter


async def test_list_phrases_never_reach_intake(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Review the Unity Center briefing note today",
            people=(),
        ),
        user=1,
        chat=1,
    )
    for text in LIST_PHRASES:
        result, interpreter = await _ask(repository, text)
        assert getattr(result, "kind") != IntakeKind.FOLLOW_UP, text
        assert getattr(result, "kind") != IntakeKind.CONFIRMATION, text
        assert interpreter.calls == [], text


async def test_project_task_list_output(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    for text in (
        "список всіх завдань по проєкту BG",
        "Покажи всі завдання по проєкту BG",
        "Покажи всі таски по проєкту BG",
        "show all tasks for BG",
    ):
        result, interpreter = await _ask(repository, text)
        assert result.kind == QueryKind.TASKS, text
        assert "📁 BG" in result.text
        assert "Agree the revised budget" in result.text
        assert "High · Urgent" not in result.text
        assert "High" not in result.text
        assert "Urgent" not in result.text
        assert interpreter.calls == []


async def test_unity_center_and_all_tasks(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Review the Unity Center briefing note today",
            people=(),
        ),
        user=1,
        chat=1,
    )
    one, _ = await _ask(repository, "Які задачі в Unity Center?")
    assert one.kind == QueryKind.TASKS
    assert "📁 Unity Center" in one.text
    assert "Review the Unity Center briefing note today" in one.text
    assert "Agree the revised budget" not in one.text
    grouped, _ = await _ask(repository, "Покажи мені всі таски по проектах")
    assert grouped.kind == QueryKind.TASKS
    assert "📁 BG" in grouped.text
    assert "📁 Unity Center" in grouped.text
    all_open, _ = await _ask(repository, "Покажи всі задачі")
    assert "📁 BG" in all_open.text
    assert "📁 Unity Center" in all_open.text


async def test_acronym_and_alias_resolution(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await repository.add_alias(1, "BG", "проєкти BG")
    for text in (
        "Покажи всі таски по проєкту Bg",
        "Покажи всі таски по проєкту B G",
        "Покажи всі таски по проєкту B.G.",
        "Покажи всі таски по проєкту проєкти BG",
    ):
        result, interpreter = await _ask(repository, text)
        assert result.kind == QueryKind.TASKS, text
        assert "📁 BG" in result.text
        assert interpreter.calls == []


async def test_unknown_project_does_not_create_project_or_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result, interpreter = await _ask(repository, "Покажи всі завдання по проєкту Missing")
    assert result.kind == QueryKind.INFO
    assert "Не знайшов проєкт «Missing»" in result.text
    assert "Покажи всі проєкти?" in result.text
    assert interpreter.calls == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 0
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 0


async def test_completed_cancelled_excluded_waiting_included(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    done_id = await persist(
        repository,
        complete_draft(project="BG", task_title="Finish the signed BG invoice pack", people=()),
        user=1,
        chat=1,
    )
    cancelled_id = await persist(
        repository,
        complete_draft(project="BG", task_title="Drop the unused BG vendor follow-up", people=()),
        user=1,
        chat=1,
    )
    waiting_id = await persist(
        repository,
        complete_draft(project="BG", task_title="Wait on finance for the BG copy", people=()),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            done = await session.get(TaskRow, done_id)
            cancelled = await session.get(TaskRow, cancelled_id)
            waiting = await session.get(TaskRow, waiting_id)
            assert done and cancelled and waiting
            done.status = "done"
            cancelled.status = "cancelled"
            waiting.status = "waiting"
    result, interpreter = await _ask(repository, "Покажи всі таски по проєкту BG")
    assert interpreter.calls == []
    assert "Agree the revised budget" in result.text
    assert "Wait on finance for the BG copy" in result.text
    assert "Finish the signed BG invoice pack" not in result.text
    assert "Drop the unused BG vendor follow-up" not in result.text


async def test_user_scoping(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG"), user=11, chat=110)
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Review someone else's BG weekly pack",
            people=(),
        ),
        user=12,
        chat=120,
    )
    mine, interpreter = await _ask(
        repository, "Покажи всі таски по проєкту BG", user=11, chat=110
    )
    assert interpreter.calls == []
    assert "Agree the revised budget" in mine.text
    assert "someone else's" not in mine.text


async def test_voice_all_projects_phrase_lists_grouped_tasks(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=8, chat=80)
    await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Review the Unity Center briefing note today",
            people=(),
        ),
        user=8,
        chat=80,
    )
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber(["Покажи всі таски по всіх проєктах."]),
        intake,
        queries=TaskQueryService(repository),
    )
    result = await voice.handle_voice(8, 80, b"ogg")
    assert result.error is None
    assert result.intake is not None
    assert result.intake.kind == QueryKind.TASKS
    assert "Не знайшов проєкт" not in result.intake.text
    assert "📁 BG" in result.intake.text
    assert "📁 Unity Center" in result.intake.text
    assert interpreter.calls == []
    listed, _ = await _ask(repository, "Покажи всі таски по всіх проєктах.", user=8, chat=80)
    assert listed.text == result.intake.text


async def test_voice_list_query_same_path(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG"), user=8, chat=80)
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber(["Покажи всі таски по проєкту BG"]),
        intake,
        queries=TaskQueryService(repository),
    )
    result = await voice.handle_voice(8, 80, b"ogg")
    assert result.error is None
    assert result.intake is not None
    assert result.intake.kind == QueryKind.TASKS
    assert "📁 BG" in result.intake.text
    assert interpreter.calls == []


async def test_empty_project_copy(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(
            project="EmptyNest",
            task_title="Park the unused EmptyNest note today",
            people=(),
        ),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, task_id)
            assert row is not None
            row.status = "done"
            row.completed_at = datetime.now(KYIV)
    result, interpreter = await _ask(repository, "Покажи всі задачі по проєкту EmptyNest")
    assert interpreter.calls == []
    assert "У проєкті EmptyNest немає відкритих задач." in result.text


def test_truncation_copy() -> None:
    def fake(index: int) -> PlanCandidate:
        return PlanCandidate(
            id=uuid4(),
            title=f"Task number {index:02d} weekly pack",
            project="BG",
            people=(),
            deadline=date(2026, 9, 2),
            importance=Importance.HIGH,
            urgency=Urgency.URGENT,
            estimated_minutes=20,
            status="inbox",
        )

    tasks = [fake(i) for i in range(37)]
    text = format_all_tasks_grouped([("BG", tasks)], total=37)
    assert f"Показано {MAX_LISTED_TASKS} із 37." in text
    assert text.count("Task number") == MAX_LISTED_TASKS


async def test_intake_without_open_tasks_still_does_not_create() -> None:
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), FakeTaskRepository())
    result = await process_user_utterance(
        intake, 1, 1, "Покажи всі задачі", queries=TaskQueryService(FakeTaskRepository())
    )
    assert result.kind == QueryKind.TASKS
    assert interpreter.calls == []
