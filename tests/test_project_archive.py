from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.project_command import ProjectIntentKind
from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.conversation_context import InMemoryConversationStore
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.project_format import (
    BTN_ARCHIVE,
    DELETE_FOREVER,
    REFUSE_PERMANENT_DELETE,
    format_archive_prompt,
    format_archived,
    format_restored,
)
from chief_of_staff.services.project_intent import parse_project_intent_deterministic
from chief_of_staff.services.project_ops import ProjectKind, ProjectManagementService
from chief_of_staff.services.project_session import InMemoryProjectOpStore
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_lifecycle import TaskLifecycleService
from chief_of_staff.services.task_query import TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_ai_router import ScriptedRouter
from tests.test_reminders import FrozenClock
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

REAL_PHRASE = (
    "Один із проєктів називається Test Project. Я хотів би його видалити зі списку."
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


def _clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 9, 8, 12, 0, tzinfo=KYIV))


def _ops(repository: SqlAlchemyTaskRepository) -> ProjectManagementService:
    return ProjectManagementService(repository, InMemoryProjectOpStore())


async def _create_named(
    repository: SqlAlchemyTaskRepository, user: int, chat: int, name: str
) -> None:
    ops = _ops(repository)
    await ops.handle_user_text(user, chat, f"Створи проєкт {name}")
    done = await ops.confirm(user, chat)
    assert done.kind == ProjectKind.DONE


async def _project_status(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> str | None:
    async with session_factory() as session:
        row = await session.scalar(select(ProjectRow).where(ProjectRow.name == name))
        return None if row is None else row.status


def test_archive_phrases_are_not_permanent_delete() -> None:
    parsed = parse_project_intent_deterministic(REAL_PHRASE)
    assert parsed is not None
    assert parsed.kind == ProjectIntentKind.ARCHIVE_PROJECT
    assert parsed.project_query == "Test Project"
    for text in (
        "видали Test Project зі списку",
        "прибери Test Project зі списку",
        "архівуй Test Project",
        "сховай Test Project",
        "remove Test Project from my projects",
    ):
        hit = parse_project_intent_deterministic(text)
        assert hit is not None, text
        assert hit.kind == ProjectIntentKind.ARCHIVE_PROJECT, text
        assert hit.project_query == "Test Project", text
    forever = parse_project_intent_deterministic("видали Test Project назавжди")
    assert forever is not None
    assert forever.kind == ProjectIntentKind.DELETE_PROJECT_PERMANENTLY
    assert forever.project_query == "Test Project"


async def test_real_phrase_archives_and_preserves_tasks(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(
            project="Test Project",
            task_title="Підготувати тестовий звіт для ради",
            people=(),
        ),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    asked = await ops.handle_user_text(1, 1, REAL_PHRASE)
    assert asked.kind == ProjectKind.ASK_CONFIRM
    assert asked.confirm_label == BTN_ARCHIVE
    assert format_archive_prompt("Test Project") == asked.text
    names = {name for name, _c in await repository.list_projects_for_user(1)}
    assert "Test Project" in names
    done = await ops.confirm(1, 1)
    assert done.text == format_archived("Test Project")
    assert await _project_status(session_factory, "Test Project") == "archived"
    names = {name for name, _c in await repository.list_projects_for_user(1)}
    assert "Test Project" not in names
    async with session_factory() as session:
        task = await session.get(TaskRow, task_id)
        assert task is not None
        assert task.status == "inbox"
    listed = await repository.list_open_tasks_for_telegram_user(1)
    assert listed == []
    explicit = await repository.list_open_tasks_for_project_name(1, "Test Project")
    assert explicit is not None
    assert explicit[0] == "Test Project"
    assert len(explicit[1]) == 1


async def test_archived_list_and_restore(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="Test Project",
            task_title="Підготувати тестовий звіт для ради",
            people=(),
        ),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    await ops.handle_user_text(1, 1, "архівуй Test Project")
    await ops.confirm(1, 1)
    archived = await ops.handle_user_text(1, 1, "покажи архівні проєкти")
    assert archived.kind == ProjectKind.LIST
    assert "Test Project" in archived.text
    assert "📦 Архівні проєкти" in archived.text
    restore = await ops.handle_user_text(1, 1, "віднови Test Project")
    assert restore.kind == ProjectKind.ASK_CONFIRM
    done = await ops.confirm(1, 1)
    assert done.text == format_restored("Test Project")
    assert await _project_status(session_factory, "Test Project") == "active"
    names = {name for name, _c in await repository.list_projects_for_user(1)}
    assert "Test Project" in names


async def test_permanent_delete_with_tasks_refuses(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="Test Project",
            task_title="Підготувати тестовий звіт для ради",
            people=(),
        ),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    result = await ops.handle_user_text(1, 1, "видали Test Project назавжди")
    assert result.kind == ProjectKind.INFO
    assert result.text == REFUSE_PERMANENT_DELETE
    assert await _project_status(session_factory, "Test Project") == "active"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 1


async def test_empty_project_permanent_delete_needs_strong_confirm(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_named(repository, 1, 1, "Empty Lab")
    ops = _ops(repository)
    asked = await ops.handle_user_text(1, 1, "видали Empty Lab назавжди")
    assert asked.kind == ProjectKind.ASK_CONFIRM
    assert asked.confirm_label == DELETE_FOREVER
    weak = await ops.handle_user_text(1, 1, "так")
    assert weak.kind == ProjectKind.ASK_CONFIRM
    assert await _project_status(session_factory, "Empty Lab") == "active"
    done = await ops.handle_user_text(1, 1, DELETE_FOREVER)
    assert done.kind == ProjectKind.DONE
    assert await _project_status(session_factory, "Empty Lab") is None


async def test_cancel_archive_zero_db_changes(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="Test Project",
            task_title="Підготувати тестовий звіт для ради",
            people=(),
        ),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    await ops.handle_user_text(1, 1, "архівуй Test Project")
    cancelled = ops.cancel(1, 1)
    assert cancelled.kind == ProjectKind.CANCELLED
    assert await _project_status(session_factory, "Test Project") == "active"
    names = {name for name, _c in await repository.list_projects_for_user(1)}
    assert "Test Project" in names


async def test_voice_archive_same_path(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="Test Project",
            task_title="Підготувати тестовий звіт для ради",
            people=(),
        ),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    intake = TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber([REAL_PHRASE]),
        intake,
        projects=ops,
        queries=TaskQueryService(repository),
        lifecycle=TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock()),
        router=ScriptedRouter(
            [
                UtteranceInterpretation(
                    kind=RouterKind.ARCHIVE_PROJECT,
                    project_query="Test Project",
                )
            ]
        ),
        context=InMemoryConversationStore(_clock()),
    )
    voiced = await voice.handle_voice(1, 1, b"ogg")
    assert voiced.intake is not None
    assert voiced.intake.kind == ProjectKind.ASK_CONFIRM
    done = await ops.confirm(1, 1)
    assert done.text == format_archived("Test Project")
    assert await _project_status(session_factory, "Test Project") == "archived"


async def test_archived_alias_resolves_for_restore(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="Building Group",
            task_title="Погодити наступний крок по будівлі",
            people=(),
        ),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    await repository.add_alias(1, "Building Group", "BG")
    await ops.handle_user_text(1, 1, "архівуй Building Group")
    await ops.confirm(1, 1)
    assert await _project_status(session_factory, "Building Group") == "archived"
    asked = await ops.handle_user_text(1, 1, "віднови BG")
    assert asked.kind == ProjectKind.ASK_CONFIRM
    await ops.confirm(1, 1)
    assert await _project_status(session_factory, "Building Group") == "active"


async def test_archive_is_owner_scoped(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="Test Project",
            task_title="Підготувати тестовий звіт для ради",
            people=(),
        ),
        user=11,
        chat=110,
    )
    await persist(
        repository,
        complete_draft(
            project="Test Project",
            task_title="Підготувати інший тестовий звіт для ради",
            people=(),
        ),
        user=12,
        chat=120,
    )
    ops = _ops(repository)
    await ops.handle_user_text(11, 110, "архівуй Test Project")
    await ops.confirm(11, 110)
    mine = {name for name, _c in await repository.list_projects_for_user(11)}
    theirs = {name for name, _c in await repository.list_projects_for_user(12)}
    assert "Test Project" not in mine
    assert "Test Project" in theirs
    async with session_factory() as session:
        rows = (await session.scalars(select(ProjectRow))).all()
        statuses = {row.owner_user_id: row.status for row in rows}
        assert "archived" in statuses.values()
        assert "active" in statuses.values()


async def test_ai_first_cancel_does_not_archive(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="Test Project",
            task_title="Підготувати тестовий звіт для ради",
            people=(),
        ),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    await ops.handle_user_text(1, 1, "архівуй Test Project")
    result = await process_user_utterance(
        TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository),
        1,
        1,
        "ні, я не хочу цього архівувати",
        projects=ops,
        queries=TaskQueryService(repository),
        router=ScriptedRouter(
            [
                UtteranceInterpretation(
                    kind=RouterKind.CANCEL_PENDING,
                    pending_action="cancel_pending_action",
                )
            ]
        ),
        context=InMemoryConversationStore(_clock()),
    )
    assert result.kind == ProjectKind.CANCELLED
    assert await _project_status(session_factory, "Test Project") == "active"
