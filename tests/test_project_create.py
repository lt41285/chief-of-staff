from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.orm.user import UserRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.project_command import ProjectIntentKind
from chief_of_staff.services.names import normalize_project_name
from chief_of_staff.services.project_intent import parse_project_intent_deterministic
from chief_of_staff.services.project_ops import ProjectKind, ProjectManagementService
from chief_of_staff.services.project_session import InMemoryProjectOpStore
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_session import DraftPhase, InMemoryTaskSessionStore
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


def _ops(repository: SqlAlchemyTaskRepository) -> ProjectManagementService:
    return ProjectManagementService(repository, InMemoryProjectOpStore())


def test_create_project_intents() -> None:
    for text in (
        "Створи новий проєкт BG.",
        "Додай проєкт MistoHub.",
        "Новий проєкт — Fundraising.",
        "Create project Angel Portfolio.",
        "/newproject",
    ):
        parsed = parse_project_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == ProjectIntentKind.CREATE_PROJECT, text
    bg = parse_project_intent_deterministic("Створи проєкт BG")
    assert bg is not None
    assert bg.new_name == "BG"
    task = parse_project_intent_deterministic("Створи задачу по BG")
    assert task is None
    add_task = parse_project_intent_deterministic("Додай задачу по Fundraising")
    assert add_task is None
    add_proj = parse_project_intent_deterministic("Додай проєкт Fundraising")
    assert add_proj is not None
    assert add_proj.kind == ProjectIntentKind.CREATE_PROJECT


async def test_create_requires_confirm_and_cancel_creates_nothing(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ops = _ops(repository)
    asked = await ops.handle_user_text(1, 1, "Створи проєкт BG")
    assert asked.kind == ProjectKind.ASK_CONFIRM
    assert "Створити проєкт" in asked.text
    assert "📁 BG" in asked.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 0
    cancelled = ops.cancel(1, 1)
    assert cancelled.kind == ProjectKind.CANCELLED
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 0


async def test_create_project_persists_owner_display_and_normalized(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ops = _ops(repository)
    await ops.handle_user_text(7, 70, "Create project Angel Portfolio.")
    done = await ops.confirm(7, 70)
    assert done.kind == ProjectKind.DONE
    assert "Angel Portfolio" in done.text
    async with session_factory() as session:
        project = await session.scalar(select(ProjectRow))
        user = await session.scalar(select(UserRow).where(UserRow.telegram_id == 7))
        assert project is not None and user is not None
        assert project.name == "Angel Portfolio"
        assert project.name_normalized == normalize_project_name("Angel Portfolio")
        assert project.owner_user_id == user.id


async def test_duplicate_canonical_and_acronym_rejected(
    repository: SqlAlchemyTaskRepository,
) -> None:
    ops = _ops(repository)
    await ops.handle_user_text(1, 1, "Створи проєкт BG")
    await ops.confirm(1, 1)
    for text in ("Створи проєкт BG", "Створи проєкт Bg", "Створи проєкт B G", "Створи проєкт B.G."):
        result = await ops.handle_user_text(1, 1, text)
        assert result.kind == ProjectKind.INFO, text
        assert "вже існує" in result.text, text
        assert "BG" in result.text
    assert len(await repository.list_projects_for_user(1)) == 1


async def test_alias_collision_rejected(repository: SqlAlchemyTaskRepository) -> None:
    ops = _ops(repository)
    await ops.handle_user_text(1, 1, "Створи проєкт BG")
    await ops.confirm(1, 1)
    await repository.add_alias(1, "BG", "проєкти BG")
    result = await ops.handle_user_text(1, 1, "Створи проєкт проєкти BG")
    assert result.kind == ProjectKind.INFO
    assert "Це вже існуючий проєкт «BG»." in result.text


async def test_other_user_same_name_does_not_block(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ops = _ops(repository)
    await ops.handle_user_text(1, 1, "Створи проєкт BG")
    await ops.confirm(1, 1)
    await ops.handle_user_text(2, 2, "Створи проєкт BG")
    done = await ops.confirm(2, 2)
    assert done.kind == ProjectKind.DONE
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 2


async def test_newproject_without_name_asks(
    repository: SqlAlchemyTaskRepository,
) -> None:
    ops = _ops(repository)
    asked = await ops.handle_user_text(1, 1, "/newproject")
    assert asked.text == "Як назвати новий проєкт?"
    preview = await ops.handle_user_text(1, 1, "Fundraising")
    assert preview.kind == ProjectKind.ASK_CONFIRM
    assert "Fundraising" in preview.text


async def test_voice_create_uses_same_flow(repository: SqlAlchemyTaskRepository) -> None:
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber(["Створи проєкт BG."]),
        intake,
        projects=_ops(repository),
    )
    result = await voice.handle_voice(1, 1, b"ogg")
    assert result.intake is not None
    assert result.intake.kind == ProjectKind.ASK_CONFIRM
    assert interpreter.calls == []


async def test_create_never_falls_through_to_intake(
    repository: SqlAlchemyTaskRepository,
) -> None:
    interpreter = ScriptedInterpreter([])
    result = await process_user_utterance(
        TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository),
        1,
        1,
        "Створи проєкт BG",
        projects=_ops(repository),
    )
    assert result.kind == ProjectKind.ASK_CONFIRM
    assert interpreter.calls == []


async def test_create_task_phrase_is_intake_not_project(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await repository.create_user_project(1, "BG", telegram_chat_id=1)
    interpreter = ScriptedInterpreter([complete_draft(project="BG")])
    result = await process_user_utterance(
        TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository),
        1,
        1,
        "Створи задачу по BG",
        projects=_ops(repository),
    )
    assert result.kind == IntakeKind.CONFIRMATION
    assert interpreter.calls[0]["user_text"] == "Створи задачу по BG"


async def test_unknown_project_does_not_create_project_or_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await repository.create_user_project(1, "BG", telegram_chat_id=1)
    interpreter = ScriptedInterpreter([complete_draft(project="Misto Hub")])
    store = InMemoryTaskSessionStore()
    intake = TaskIntakeService(interpreter, store, repository)
    with patch.object(
        repository, "create_user_project", wraps=repository.create_user_project
    ) as create:
        result = await process_user_utterance(intake, 1, 1, "new task", projects=_ops(repository))
        create.assert_not_called()
    assert result.kind == IntakeKind.ASK_PROJECT
    assert "Не знайшов проєкт «Misto Hub»." in result.text
    assert "BG" in result.text
    assert "Створи проєкт Misto Hub" in result.text
    session = store.get(1, 1)
    assert session is not None
    assert session.phase == DraftPhase.AWAITING_PROJECT
    async with session_factory() as session_db:
        assert await session_db.scalar(select(func.count()).select_from(TaskRow)) == 0
        names = set((await session_db.scalars(select(ProjectRow.name))).all())
        assert names == {"BG"}


async def test_existing_canonical_and_alias_attach(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await repository.add_alias(1, "BG", "проєкти BG")
    interpreter = ScriptedInterpreter(
        [complete_draft(project="проєкти BG", task_title="Send the weekly BG pack today", people=())]
    )
    store = InMemoryTaskSessionStore()
    intake = TaskIntakeService(interpreter, store, repository)
    result = await process_user_utterance(intake, 1, 1, "task")
    assert result.kind == IntakeKind.CONFIRMATION
    yes = await process_user_utterance(intake, 1, 1, "Yes")
    assert yes.kind == IntakeKind.CREATED
    async with session_factory() as session:
        tasks = (await session.scalars(select(TaskRow))).all()
        assert len(tasks) == 2
        assert {task.project_id for task in tasks} == {tasks[0].project_id}
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 1


async def test_select_existing_project_continues_draft(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await repository.create_user_project(1, "Unity Center", telegram_chat_id=1)
    await repository.create_user_project(1, "BG", telegram_chat_id=1)
    interpreter = ScriptedInterpreter([complete_draft(project="Misto Hub")])
    store = InMemoryTaskSessionStore()
    intake = TaskIntakeService(interpreter, store, repository)
    first = await process_user_utterance(intake, 1, 1, "new task")
    assert first.kind == IntakeKind.ASK_PROJECT
    second = await process_user_utterance(intake, 1, 1, "Unity Center")
    assert second.kind == IntakeKind.CONFIRMATION
    assert "Unity Center" in second.text
    assert store.get(1, 1) is not None
    yes = await process_user_utterance(intake, 1, 1, "Yes")
    assert yes.kind == IntakeKind.CREATED
    async with session_factory() as session:
        task = await session.scalar(select(TaskRow))
        project = await session.get(ProjectRow, task.project_id)
        assert project is not None
        assert project.name == "Unity Center"
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 2


async def test_fuzzy_suggestion_does_not_create(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await repository.create_user_project(1, "BG", telegram_chat_id=1)
    interpreter = ScriptedInterpreter([complete_draft(project="проєкти BG")])
    store = InMemoryTaskSessionStore()
    intake = TaskIntakeService(interpreter, store, repository)
    result = await process_user_utterance(intake, 1, 1, "task")
    assert result.kind == IntakeKind.CONFIRMATION
    assert "BG" in result.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 1
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 0
    yes = await process_user_utterance(intake, 1, 1, "Yes")
    assert yes.kind == IntakeKind.CREATED
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 1
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 1
