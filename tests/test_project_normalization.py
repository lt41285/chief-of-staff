from collections.abc import AsyncIterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.project_alias import ProjectAliasRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.services.names import normalize_project_name
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_session import DraftPhase, InMemoryTaskSessionStore, TaskSession
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft

BG_VARIANTS = ("BG", "Bg", "bG", "bg", "B G", "B.G.", "B-G")
UCU_VARIANTS = ("UCU", "Ucu", "ucu", "U C U")


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


def test_acronym_variants_share_one_key() -> None:
    keys = {normalize_project_name(raw) for raw in BG_VARIANTS}
    assert keys == {"bg"}


def test_ucu_acronym_variants_share_one_key() -> None:
    keys = {normalize_project_name(raw) for raw in UCU_VARIANTS}
    assert keys == {"ucu"}


def test_multiword_project_is_not_over_normalized() -> None:
    assert normalize_project_name("Unity Center") == "unity center"
    assert normalize_project_name("  unity   CENTER ") == "unity center"
    assert normalize_project_name("Unity Center") != normalize_project_name("BG")
    assert normalize_project_name("Go Pro") == "go pro"
    assert normalize_project_name("Go Pro") != normalize_project_name("GOPRO")
    assert normalize_project_name("AB Corp") == "ab corp"


async def test_acronym_variants_resolve_to_one_canonical_project(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    canonical = await repository.resolve_user_project(1, "BG")
    assert canonical is not None
    assert canonical.name == "BG"
    for raw in BG_VARIANTS:
        found = await repository.resolve_user_project(1, raw)
        assert found is not None
        assert found.id == canonical.id
        assert found.name == "BG"
        await persist(
            repository,
            complete_draft(
                project=raw,
                task_title=f"Send the BG weekly pack for {raw.replace(' ', '')}",
                people=(),
            ),
            user=1,
            chat=1,
        )
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(ProjectRow))
        project = await session.get(ProjectRow, canonical.id)
        assert count == 1
        assert project is not None
        assert project.name == "BG"
        assert project.name_normalized == "bg"


async def test_ucu_variants_reuse_canonical_display_name(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="UCU"), user=2, chat=2)
    for raw in UCU_VARIANTS:
        found = await repository.resolve_user_project(2, raw)
        assert found is not None
        assert found.name == "UCU"
        await persist(
            repository,
            complete_draft(
                project=raw,
                task_title=f"Review the UCU briefing note {raw.replace(' ', '')}",
                people=(),
            ),
            user=2,
            chat=2,
        )
    listed = await repository.list_projects_for_user(2)
    assert [name for name, _count in listed] == ["UCU"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 1


async def test_normalization_is_user_scoped(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG"), user=11, chat=110)
    await persist(
        repository,
        complete_draft(project="bg", task_title="File the other user BG status pack", people=()),
        user=12,
        chat=120,
    )
    one = await repository.resolve_user_project(11, "B G")
    two = await repository.resolve_user_project(12, "B.G.")
    assert one is not None and two is not None
    assert one.id != two.id
    assert one.name == "BG"
    assert two.name == "bg"


async def test_multiword_names_still_reuse_by_fold(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="Unity Center"), user=3, chat=3)
    await persist(
        repository,
        complete_draft(
            project="  unity   CENTER ",
            task_title="Send the Unity Center budget pack today",
            people=(),
        ),
        user=3,
        chat=3,
    )
    found = await repository.resolve_user_project(3, "UNITY center")
    assert found is not None
    assert found.name == "Unity Center"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 1


async def test_alias_uses_same_normalization(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="Unity Center"), user=4, chat=4)
    await repository.add_alias(4, "Unity Center", "B G")
    found = await repository.resolve_user_project(4, "B.G.")
    assert found is not None
    assert found.name == "Unity Center"
    async with session_factory() as session:
        keys = set(
            (await session.scalars(select(ProjectAliasRow.normalized_alias))).all()
        )
        assert keys == {"bg"}


async def test_acronym_alias_of_canonical_is_not_stored(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG"), user=5, chat=5)
    await repository.add_alias(5, "BG", "B.G.")
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectAliasRow)) == 0
    found = await repository.resolve_user_project(5, "bg")
    assert found is not None
    assert found.name == "BG"


async def test_intake_reuses_canonical_for_transcribed_acronym(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG"), user=6, chat=6)
    store = InMemoryTaskSessionStore()
    store.put(
        6,
        6,
        TaskSession(
            draft=complete_draft(
                project="Bg",
                task_title="Send the BG weekly status pack today",
                people=(),
            ),
            phase=DraftPhase.CONFIRMING,
        ),
    )
    intake = TaskIntakeService(ScriptedInterpreter([]), store, repository)
    result = await intake.confirm(6, 6)
    assert result.kind == IntakeKind.CREATED
    assert store.get(6, 6) is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ProjectRow)) == 1
        row = (await session.scalars(select(ProjectRow))).one()
        assert row.name == "BG"
    listed = await repository.list_projects_for_user(6)
    assert listed == [("BG", 2)]
