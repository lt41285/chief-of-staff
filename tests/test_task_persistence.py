from collections.abc import AsyncIterator
from datetime import date
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm import PersonRow, ProjectRow, TaskPersonRow, TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.task import Importance, TaskDraft, Urgency
from chief_of_staff.services.task_card import SAVE_FAILED, format_created_message
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_session import DraftPhase, InMemoryTaskSessionStore
from tests.fakes import FakeTaskRepository
from tests.test_task_validation import ScriptedInterpreter, complete_draft


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


async def _count(session: AsyncSession, model: type[object]) -> int:
    result = await session.scalar(select(func.count()).select_from(model))
    return int(result or 0)


async def persist(
    repository: SqlAlchemyTaskRepository,
    draft: TaskDraft,
    *,
    user: int = 10,
    chat: int = 20,
) -> UUID:
    if draft.project:
        existing = await repository.resolve_user_project(user, draft.project)
        if existing is None:
            await repository.create_user_project(
                user, draft.project, telegram_chat_id=chat
            )
    return await repository.save_validated_task(
        draft,
        telegram_user_id=user,
        telegram_chat_id=chat,
    )


async def test_save_requires_existing_project(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError, match="project_not_found"):
        await repository.save_validated_task(
            complete_draft(project="Ghost"),
            telegram_user_id=1,
            telegram_chat_id=1,
        )
    async with session_factory() as session:
        assert await _count(session, ProjectRow) == 0
        assert await _count(session, TaskRow) == 0
    draft = complete_draft()
    task_id = await persist(repository, draft)
    assert isinstance(task_id, UUID)

    async with session_factory() as session:
        task = await session.get(TaskRow, task_id)
        assert task is not None
        assert task.title == draft.task_title
        assert task.desired_outcome == draft.desired_outcome
        assert task.deadline == date(2026, 9, 2)
        assert task.importance == Importance.MEDIUM.value
        assert task.urgency == Urgency.NOT_URGENT.value
        assert task.estimated_minutes == 20
        assert task.status == "inbox"
        assert task.actual_minutes is None
        assert task.completed_at is None
        assert task.owner_user_id is not None
        assert await _count(session, ProjectRow) == 1
        assert await _count(session, PersonRow) == 1
        assert await _count(session, TaskRow) == 1
        assert await _count(session, TaskPersonRow) == 1


async def test_save_uses_legacy_defaults_when_importance_omitted(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(repository, complete_draft(importance=None, urgency=None))
    async with session_factory() as session:
        task = await session.get(TaskRow, task_id)
        assert task is not None
        assert task.importance == Importance.MEDIUM.value
        assert task.urgency == Urgency.NOT_URGENT.value


async def test_historical_importance_values_still_load(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(importance=Importance.HIGH, urgency=Urgency.URGENT),
    )
    loaded = await repository.list_user_tasks(10)
    assert len(loaded) == 1
    assert loaded[0].id == task_id
    assert loaded[0].importance == Importance.HIGH
    assert loaded[0].urgency == Urgency.URGENT
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.importance == "high"
        assert row.urgency == "urgent"


async def test_reuse_existing_project(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft())
    second = complete_draft(
        project="  unity   CENTER ",
        task_title="Send the signed budget pack to finance",
        people=(),
    )
    await persist(repository, second)

    async with session_factory() as session:
        assert await _count(session, ProjectRow) == 1
        assert await _count(session, TaskRow) == 2
        project = await session.scalar(select(ProjectRow))
        assert project is not None
        assert project.name == "Unity Center"


async def test_reuse_existing_person(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft())
    await persist(
        repository,
        complete_draft(
            task_title="Review the budget comments with Taras",
            people=("  TARAS ",),
        ),
    )

    async with session_factory() as session:
        assert await _count(session, PersonRow) == 1
        assert await _count(session, TaskRow) == 2
        assert await _count(session, TaskPersonRow) == 2
        person = await session.scalar(select(PersonRow))
        assert person is not None
        assert person.name == "Taras"


async def test_multiple_people_on_a_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(people=("Taras", "Oksana", "taras")),
    )

    async with session_factory() as session:
        assert await _count(session, PersonRow) == 2
        assert await _count(session, TaskPersonRow) == 2
        names = set((await session.scalars(select(PersonRow.name))).all())
        assert names == {"Taras", "Oksana"}


async def test_failure_rollback(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft())
    with (
        patch.object(
            SqlAlchemyTaskRepository,
            "_link_people",
            side_effect=RuntimeError("write failed"),
        ),
        pytest.raises(RuntimeError, match="write failed"),
    ):
        await persist(
            repository,
            complete_draft(task_title="Prepare the weekly status note for Unity"),
        )

    async with session_factory() as session:
        assert await _count(session, TaskRow) == 1
        assert await _count(session, ProjectRow) == 1
        assert await _count(session, PersonRow) == 1


async def test_confirm_persists_and_clears_draft() -> None:
    repo = FakeTaskRepository()
    interpreter = ScriptedInterpreter([complete_draft()])
    store = InMemoryTaskSessionStore()
    service = TaskIntakeService(interpreter, store, repo)
    await service.handle_user_text(1, "full task", chat_id=1)
    yes = await service.handle_user_text(1, "Yes", chat_id=1)
    assert yes.kind == IntakeKind.CREATED
    assert yes.text == format_created_message(complete_draft())
    assert "✅ Task created" in yes.text
    assert len(repo.saved) == 1
    assert store.get(1, 1) is None


async def test_confirm_failure_keeps_draft_for_retry() -> None:
    repo = FakeTaskRepository()
    interpreter = ScriptedInterpreter([complete_draft()])
    store = InMemoryTaskSessionStore()
    service = TaskIntakeService(interpreter, store, repo)
    await service.handle_user_text(1, "full task", chat_id=1)
    repo.fail = True
    failed = await service.handle_user_text(1, "Yes", chat_id=1)
    assert failed.kind == IntakeKind.SAVE_FAILED
    assert failed.text == SAVE_FAILED
    assert failed.show_confirm_buttons
    session = store.get(1, 1)
    assert session is not None
    assert session.phase == DraftPhase.CONFIRMING
    assert not repo.saved

    repo.fail = False
    retry = await service.handle_user_text(1, "Yes", chat_id=1)
    assert retry.kind == IntakeKind.CREATED
    assert len(repo.saved) == 1
    assert store.get(1, 1) is None
