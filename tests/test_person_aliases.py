from collections.abc import AsyncIterator
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.person import PersonRow
from chief_of_staff.infrastructure.database.orm.person_alias import PersonAliasRow
from chief_of_staff.infrastructure.database.orm.task import TaskPersonRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.conversation_context import InMemoryConversationStore
from chief_of_staff.services.person_match import PersonHit
from chief_of_staff.services.person_resolution import PersonAliasHit, resolve_person_reference
from tests.test_ai_router import ScriptedRouter, _clock, _turn
from tests.test_task_persistence import persist
from tests.test_task_validation import complete_draft


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


async def _seed_duplicate_borovets(repository: SqlAlchemyTaskRepository, *, user: int = 1) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Узгодити порядок денний з Андрієм Боровцем",
            people=("Андрій Боровець",),
            deadline=date(2026, 9, 16),
        ),
        user=user,
        chat=user,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Надіслати короткий статус Боровцю",
            people=("Боровець",),
            deadline=date(2026, 9, 18),
        ),
        user=user,
        chat=user,
    )


def test_inflections_match_alias_not_other_person() -> None:
    andriy = PersonHit(name="Андрій Боровець", person_id=uuid4())
    aliases = (
        PersonAliasHit(alias="Боровець", person_id=andriy.person_id, person_name=andriy.name),
    )
    for query in ("Боровець", "Боровцю", "Боровцем", "боровцю"):
        resolved = resolve_person_reference(query, [andriy], aliases)
        assert resolved.status == "resolved"
        assert resolved.person is not None
        assert resolved.person.name == "Андрій Боровець"


async def test_same_person_persists_and_continues(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_duplicate_borovets(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Боровцю"),
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Боровцю"),
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Боровцю"),
        ]
    )
    first, _, store = await _turn(
        repository, "Що в мене по боровцю?", router=router, context=store
    )
    assert first.is_person_ambiguity
    assert "Андрій Боровець" in first.text
    assert "Боровець" in first.text

    second, _, store = await _turn(
        repository, "це одне і те ж саме", router=router, context=store
    )
    assert "враховую" not in second.text.casefold()
    assert "Андрій Боровець" in second.text
    assert "Узгодити порядок" in second.text
    assert "Надіслати короткий статус" in second.text
    assert "Надалі" in second.text
    async with session_factory() as session:
        aliases = (await session.scalars(select(PersonAliasRow))).all()
        assert aliases
        people = (await session.scalars(select(PersonRow))).all()
        names = {row.name for row in people}
        assert "Андрій Боровець" in names

    fresh = InMemoryConversationStore(_clock())
    again, _, _ = await _turn(
        repository, "Що в мене по Боровцю?", router=router, context=fresh
    )
    assert not again.is_person_ambiguity
    assert "чи «" not in again.text
    assert "Узгодити порядок" in again.text
    assert "Надіслати короткий статус" in again.text


async def test_inflected_query_after_alias(repository: SqlAlchemyTaskRepository) -> None:
    await _seed_duplicate_borovets(repository)
    await repository.confirm_person_equivalence(1, "Андрій Боровець", "Боровець")
    for text in ("що в мене по Боровцю?", "що по Боровцем?"):
        store = InMemoryConversationStore(_clock())
        router = ScriptedRouter(
            [UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Боровцю")]
        )
        result, _, _ = await _turn(repository, text, router=router, context=store)
        assert not result.is_person_ambiguity
        assert "чи «" not in result.text


async def test_alias_conflict_does_not_overwrite(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(project="BG", task_title="Підготувати матеріали для Марії Коваль", people=("Марія Коваль",)),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Погодити наступний крок з Андрієм Боровцем",
            people=("Андрій Боровець",),
        ),
        user=1,
        chat=1,
    )
    first = await repository.confirm_person_equivalence(1, "Марія Коваль", "Боровець")
    assert first.status == "ok"
    conflict = await repository.confirm_person_equivalence(1, "Андрій Боровець", "Боровець")
    assert conflict.status == "conflict"
    assert conflict.conflict_with == "Марія Коваль"


async def test_merge_dedupes_task_people(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Узгодити подвійний зв'язок по Боровцю",
            people=("Андрій Боровець", "Боровець"),
        ),
        user=1,
        chat=1,
    )
    outcome = await repository.confirm_person_equivalence(1, "Андрій Боровець", "Боровець")
    assert outcome.status == "ok"
    async with session_factory() as session:
        links = (await session.scalars(select(TaskPersonRow))).all()
        assert len(links) == 1


async def test_merge_moves_waiting_for(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Дочекатися відповіді від Боровця по датах",
            people=("Андрій Боровець",),
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Окремий запис людини Боровець у реєстрі",
            people=("Боровець",),
        ),
        user=1,
        chat=1,
    )
    tasks = await repository.list_user_tasks(1)
    waiting = next(task for task in tasks if "Дочекатися" in task.title)
    await repository.set_user_task_waiting(1, waiting.id, "Боровець")
    await repository.confirm_person_equivalence(1, "Андрій Боровець", "Боровець")
    refreshed = await repository.get_user_task(1, waiting.id)
    assert refreshed is not None
    assert refreshed.waiting_for == "Андрій Боровець"


async def test_alias_is_per_user(repository: SqlAlchemyTaskRepository) -> None:
    await _seed_duplicate_borovets(repository, user=1)
    await _seed_duplicate_borovets(repository, user=2)
    await repository.confirm_person_equivalence(1, "Андрій Боровець", "Боровець")
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Боровцю")]
    )
    other, _, _ = await _turn(
        repository, "Що в мене по Боровцю?", user=2, chat=2, router=router, context=store
    )
    assert other.is_person_ambiguity
    self_store = InMemoryConversationStore(_clock())
    self_router = ScriptedRouter(
        [UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Боровцю")]
    )
    mine, _, _ = await _turn(
        repository, "Що в мене по Боровцю?", user=1, chat=1, router=self_router, context=self_store
    )
    assert not mine.is_person_ambiguity
