from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.project_command import ProjectIntentKind
from chief_of_staff.models.task_command import TaskIntentKind
from chief_of_staff.services.project_intent import parse_project_intent_deterministic
from chief_of_staff.services.task_command_intent import parse_task_intent_deterministic
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_query import QueryKind, TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber


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


async def _set_status(
    session_factory: async_sessionmaker[AsyncSession], task_id: UUID, status: str
) -> None:
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, task_id)
            assert row is not None
            row.status = status


async def test_open_count_matches_project_task_list_for_every_status(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    inbox = await persist(repository, complete_draft(project="Unity Center"), user=1, chat=1)
    nxt = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Next the Unity weekly pack today",
            people=(),
        ),
        user=1,
        chat=1,
    )
    today = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Today the Unity status note pack",
            people=(),
        ),
        user=1,
        chat=1,
    )
    waiting = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Wait on finance for Unity copy",
            people=(),
        ),
        user=1,
        chat=1,
    )
    done = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Finish the signed Unity invoice pack",
            people=(),
        ),
        user=1,
        chat=1,
    )
    cancelled = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Drop the unused Unity vendor follow-up",
            people=(),
        ),
        user=1,
        chat=1,
    )
    await _set_status(session_factory, nxt, "next")
    await _set_status(session_factory, today, "today")
    await _set_status(session_factory, waiting, "waiting")
    await _set_status(session_factory, done, "done")
    await _set_status(session_factory, cancelled, "cancelled")

    listed = dict(await repository.list_projects_for_user(1))
    resolved = await repository.resolve_user_project(1, "Unity Center")
    assert resolved is not None
    open_tasks = await repository.list_open_tasks_for_project(1, resolved.id)
    assert listed["Unity Center"] == len(open_tasks) == 4
    ids = {task.id for task in open_tasks}
    assert ids == {inbox, nxt, today, waiting}
    assert done not in ids
    assert cancelled not in ids
    by_name = await repository.list_open_tasks_for_project_name(1, "Unity Center")
    assert by_name is not None
    assert {task.id for task in by_name[1]} == ids


async def test_null_owner_legacy_tasks_count_and_list_together(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owned = await persist(repository, complete_draft(project="Unity Center"), user=1, chat=1)
    orphan = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Поговорити з Тарасом про бюджет по Unity Center",
            people=(),
        ),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, orphan)
            assert row is not None
            row.owner_user_id = None
    listed = dict(await repository.list_projects_for_user(1))
    resolved = await repository.resolve_user_project(1, "Unity Center")
    assert resolved is not None
    open_tasks = await repository.list_open_tasks_for_project(1, resolved.id)
    assert listed["Unity Center"] == len(open_tasks) == 2
    assert {task.id for task in open_tasks} == {owned, orphan}


async def test_aliases_do_not_multiply_counts(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await repository.add_alias(1, "BG", "проєкти BG")
    await repository.add_alias(1, "BG", "BG Projects")
    listed = dict(await repository.list_projects_for_user(1))
    resolved = await repository.resolve_user_project(1, "BG")
    assert resolved is not None
    assert listed["BG"] == 1
    assert len(await repository.list_open_tasks_for_project(1, resolved.id)) == 1


async def test_other_user_tasks_on_same_project_id_do_not_leak(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    mine = await persist(repository, complete_draft(project="Unity Center"), user=1, chat=1)
    theirs = await persist(
        repository,
        complete_draft(
            project="Other",
            task_title="Review someone else's Unity budget pack",
            people=(),
        ),
        user=2,
        chat=2,
    )
    resolved = await repository.resolve_user_project(1, "Unity Center")
    assert resolved is not None
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, theirs)
            assert row is not None
            row.project_id = resolved.id
    listed = dict(await repository.list_projects_for_user(1))
    open_tasks = await repository.list_open_tasks_for_project(1, resolved.id)
    assert listed["Unity Center"] == len(open_tasks) == 1
    assert open_tasks[0].id == mine


async def test_people_join_does_not_duplicate_counts(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(project="BG", people=("Taras", "Andriy", "Lesya")),
        user=1,
        chat=1,
    )
    listed = dict(await repository.list_projects_for_user(1))
    resolved = await repository.resolve_user_project(1, "BG")
    assert resolved is not None
    assert listed["BG"] == 1
    assert len(await repository.list_open_tasks_for_project(1, resolved.id)) == 1


async def test_list_all_and_project_counts_share_open_definition(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
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
    waiting = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Wait on finance for Unity copy",
            people=(),
        ),
        user=1,
        chat=1,
    )
    done = await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Finish the signed BG invoice pack today",
            people=(),
        ),
        user=1,
        chat=1,
    )
    await _set_status(session_factory, waiting, "waiting")
    await _set_status(session_factory, done, "done")
    listed = dict(await repository.list_projects_for_user(1))
    all_open = await repository.list_user_tasks(1)
    by_project: dict[str, int] = {}
    for task in all_open:
        by_project[task.project] = by_project.get(task.project, 0) + 1
    assert listed["BG"] == by_project["BG"] == 1
    assert listed["Unity Center"] == by_project["Unity Center"] == 2
    assert sum(listed.values()) == len(all_open)


async def test_user_scoping_of_counts(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG"), user=11, chat=110)
    await persist(
        repository,
        complete_draft(project="BG", task_title="Review someone else's BG weekly pack", people=()),
        user=12,
        chat=120,
    )
    assert dict(await repository.list_projects_for_user(11)) == {"BG": 1}
    assert dict(await repository.list_projects_for_user(12)) == {"BG": 1}


def test_grouped_task_names_phrase_is_list_all_not_projects() -> None:
    parsed = parse_task_intent_deterministic(
        "всі задачі, щоб було видно їхні назви по проєктах"
    )
    assert parsed is not None
    assert parsed.kind == TaskIntentKind.LIST_ALL_TASKS
    projects = parse_project_intent_deterministic("покажи всі проєкти")
    assert projects is not None
    assert projects.kind == ProjectIntentKind.LIST_PROJECTS
    assert (
        parse_project_intent_deterministic(
            "всі задачі, щоб було видно їхні назви по проєктах"
        )
        is None
    )
    assert parse_task_intent_deterministic("список завдань за проєктами").kind == (
        TaskIntentKind.LIST_ALL_TASKS
    )
    open_uc = parse_task_intent_deterministic(
        "Які відкриті задачі по проєкту Unity Center?"
    )
    assert open_uc is not None
    assert open_uc.kind == TaskIntentKind.LIST_PROJECT_TASKS
    assert open_uc.project_query == "Unity Center"


async def test_voice_grouped_names_phrase_lists_tasks(
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
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    queries = TaskQueryService(repository)
    voice = VoiceMessageService(
        FakeTranscriber(["всі задачі, щоб було видно їхні назви по проєктах"]),
        intake,
        queries=queries,
    )
    result = await voice.handle_voice(1, 1, b"ogg")
    assert result.intake is not None
    assert result.intake.kind == QueryKind.TASKS
    assert "📁 BG" in result.intake.text
    assert "📁 Unity Center" in result.intake.text
    assert interpreter.calls == []
    listed = await process_user_utterance(
        intake,
        1,
        1,
        "Які відкриті задачі по проєкту Unity Center?",
        queries=queries,
    )
    assert listed.kind == QueryKind.TASKS
    assert "Review the Unity Center briefing note today" in listed.text
    assert "Agree the revised budget" not in listed.text
    assert interpreter.calls == []
