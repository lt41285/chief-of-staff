from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.project_alias import ProjectAliasRow
from chief_of_staff.infrastructure.database.orm.reminder import TaskReminderRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.project_command import ProjectIntentKind
from chief_of_staff.models.reminder import ReminderType
from chief_of_staff.services.names import projects_look_similar
from chief_of_staff.services.project_intent import parse_project_intent_deterministic
from chief_of_staff.services.project_ops import ProjectKind, ProjectManagementService
from chief_of_staff.services.project_session import InMemoryProjectOpStore
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.fakes import FakeTaskRepository
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


async def _project_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(ProjectRow)) or 0)


async def _alias_names(
    session_factory: async_sessionmaker[AsyncSession], project_id: UUID
) -> set[str]:
    async with session_factory() as session:
        rows = (await session.scalars(select(ProjectAliasRow).where(ProjectAliasRow.project_id == project_id))).all()
        return {row.normalized_alias for row in rows}


def test_parses_list_rename_merge_and_tasks_intents() -> None:
    assert parse_project_intent_deterministic("Покажи всі мої проєкти.").kind == ProjectIntentKind.LIST_PROJECTS
    for phrase in (
        "Покажи тепер список проєктів.",
        "покажи проєкти",
        "які в мене проєкти",
        "список проєктів",
        "покажи загальний список проєктів",
    ):
        parsed = parse_project_intent_deterministic(phrase)
        assert parsed is not None, phrase
        assert parsed.kind == ProjectIntentKind.LIST_PROJECTS, phrase
    rename = parse_project_intent_deterministic("Перейменуй проєкт BG на BG Projects.")
    assert rename is not None
    assert rename.kind == ProjectIntentKind.RENAME_PROJECT
    assert rename.old_name == "BG"
    assert rename.new_name == "BG Projects"
    merge = parse_project_intent_deterministic("Об'єднай BG і проєкти BG. Залиш назву BG.")
    assert merge is not None
    assert merge.kind == ProjectIntentKind.MERGE_PROJECTS
    assert merge.keep_name == "BG"
    assert merge.merge_names == ["проєкти BG"]
    same = parse_project_intent_deterministic("Проєкти BG — це той самий проєкт, що BG.")
    assert same is not None
    assert same.kind == ProjectIntentKind.LINK_ALIAS
    tasks = parse_project_intent_deterministic("Які задачі є в проєкті BG?")
    assert tasks is not None
    assert tasks.kind == ProjectIntentKind.PROJECT_TASKS
    assert tasks.project_query == "BG"
    assert parse_project_intent_deterministic("Додай задачу по проєктах BG") is None


def test_fuzzy_similarity_does_not_equal_identity() -> None:
    assert projects_look_similar("BG", "проєкти BG")
    assert not projects_look_similar("BG", "Unity Center")


async def test_canonical_and_alias_lookup(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    canonical = await repository.resolve_user_project(1, "BG")
    assert canonical is not None
    assert canonical.name == "BG"
    await repository.add_alias(1, "BG", "проєкти BG")
    aliased = await repository.resolve_user_project(1, "проєкти BG")
    assert aliased is not None
    assert aliased.id == canonical.id
    assert aliased.name == "BG"


async def test_task_created_using_alias_attaches_to_canonical(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await repository.add_alias(1, "BG", "проєкти BG")
    second = await persist(
        repository,
        complete_draft(
            project="проєкти BG",
            task_title="Send the weekly BG status pack today",
            people=(),
        ),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        a = await session.get(TaskRow, first)
        b = await session.get(TaskRow, second)
        assert a is not None and b is not None
        assert a.project_id == b.project_id
        assert await _project_count(session_factory) == 1


async def test_alias_does_not_create_duplicate_project(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await repository.add_alias(1, "BG", "проєкти BG")
    found = await repository.resolve_user_project(1, "проєкти BG")
    assert found is not None
    assert await repository.list_projects_for_user(1) == [("BG", 1)]


async def test_rename_keeps_id_and_old_name_becomes_alias(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    before = await repository.resolve_user_project(1, "BG")
    assert before is not None
    ops = _ops(repository)
    asked = await ops.handle_user_text(1, 1, "Перейменуй проєкт BG на Business Group.")
    assert asked.kind == ProjectKind.ASK_CONFIRM
    assert asked.show_confirm_buttons
    async with session_factory() as session:
        row = await session.get(ProjectRow, before.id)
        assert row is not None
        assert row.name == "BG"
    done = await ops.confirm(1, 1)
    assert done.kind == ProjectKind.DONE
    after = await repository.resolve_user_project(1, "Business Group")
    assert after is not None
    assert after.id == before.id
    via_old = await repository.resolve_user_project(1, "BG")
    assert via_old is not None
    assert via_old.id == before.id
    async with session_factory() as session:
        task = await session.get(TaskRow, task_id)
        assert task is not None
        assert task.project_id == before.id
    assert "bg" in await _alias_names(session_factory, before.id)


async def test_merge_moves_tasks_reminders_and_old_name_alias(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    keep_task = await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    drop_task = await persist(
        repository,
        complete_draft(
            project="проєкти BG",
            task_title="Prepare the BG weekly finance pack",
            people=(),
        ),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(
                TaskReminderRow(
                    task_id=drop_task,
                    reminder_type=ReminderType.DUE_TODAY.value,
                    occurrence_date=date(2026, 9, 2),
                    scheduled_for=datetime(2026, 9, 2, 6, 0, tzinfo=timezone.utc),
                )
            )
    keep = await repository.resolve_user_project(1, "BG")
    drop = await repository.resolve_user_project(1, "проєкти BG")
    assert keep is not None and drop is not None
    assert keep.id != drop.id
    ops = _ops(repository)
    prompt = await ops.handle_user_text(1, 1, "Об'єднай BG і проєкти BG. Залиш назву BG.")
    assert prompt.show_confirm_buttons
    assert "Задач буде перенесено: 1" in prompt.text
    result = await ops.confirm(1, 1)
    assert result.kind == ProjectKind.DONE
    assert await repository.resolve_user_project(1, "проєкти BG") is not None
    async with session_factory() as session:
        moved = await session.get(TaskRow, drop_task)
        kept = await session.get(TaskRow, keep_task)
        assert moved is not None and kept is not None
        assert moved.project_id == keep.id
        assert kept.project_id == keep.id
        assert await session.get(ProjectRow, drop.id) is None
        reminder = await session.scalar(select(TaskReminderRow).where(TaskReminderRow.task_id == drop_task))
        assert reminder is not None
        assert reminder.task_id == drop_task
    assert await _project_count(session_factory) == 1
    names = {name for name, _count in await repository.list_projects_for_user(1)}
    assert names == {"BG"}


async def test_merge_cancellation_changes_nothing(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await persist(
        repository,
        complete_draft(project="проєкти BG", task_title="Write the BG board update note", people=()),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    await ops.handle_user_text(1, 1, "Об'єднай BG і проєкти BG. Залиш назву BG.")
    cancelled = ops.cancel(1, 1)
    assert cancelled.kind == ProjectKind.CANCELLED
    assert await _project_count(session_factory) == 2


async def test_merge_is_transactional(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    drop_task = await persist(
        repository,
        complete_draft(project="проєкти BG", task_title="Draft the BG hiring plan note", people=()),
        user=1,
        chat=1,
    )
    drop = await repository.resolve_user_project(1, "проєкти BG")
    assert drop is not None

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("forced failure")

    monkeypatch.setattr(SqlAlchemyTaskRepository, "_ensure_alias", boom)
    with pytest.raises(RuntimeError, match="forced failure"):
        await repository.merge_projects(1, "BG", ["проєкти BG"])
    async with session_factory() as session:
        task = await session.get(TaskRow, drop_task)
        assert task is not None
        assert task.project_id == drop.id
        assert await session.get(ProjectRow, drop.id) is not None


async def test_project_listing_counts_open_tasks(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    done = await persist(
        repository,
        complete_draft(project="BG", task_title="Finish the BG invoice pack today", people=()),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, done)
            assert row is not None
            row.status = "done"
    listed = await repository.list_projects_for_user(1)
    assert listed == [("BG", 1)]


async def test_lookup_is_user_scoped(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG"), user=11, chat=110)
    await persist(
        repository,
        complete_draft(project="BG", task_title="Review the other owner's BG pack", people=()),
        user=12,
        chat=120,
    )
    mine = await repository.list_projects_for_user(11)
    theirs = await repository.list_projects_for_user(12)
    assert mine == [("BG", 1)]
    assert theirs == [("BG", 1)]
    one = await repository.resolve_user_project(11, "BG")
    two = await repository.resolve_user_project(12, "BG")
    assert one is not None and two is not None
    assert one.id != two.id


async def test_fuzzy_never_automatically_merges(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    similar = await repository.suggest_similar_project(1, "проєкти BG")
    assert similar is not None
    assert similar.name == "BG"
    assert await repository.resolve_user_project(1, "проєкти BG") is None
    await persist(
        repository,
        complete_draft(project="проєкти BG", task_title="File the BG archive copy today", people=()),
        user=1,
        chat=1,
    )
    names = {name for name, _c in await repository.list_projects_for_user(1)}
    assert names == {"BG", "проєкти BG"}


async def test_project_management_does_not_enter_task_intake() -> None:
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), FakeTaskRepository())
    ops = ProjectManagementService(FakeTaskRepository(), InMemoryProjectOpStore())
    result = await process_user_utterance(
        intake, 1, 1, "Покажи всі мої проєкти.", projects=ops
    )
    assert getattr(result, "kind") == ProjectKind.LIST
    assert interpreter.calls == []


async def test_voice_project_command_uses_same_flow(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=8, chat=80)
    ops = _ops(repository)
    intake = TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber(["Покажи всі мої проєкти."]),
        intake,
        projects=ops,
    )
    result = await voice.handle_voice(8, 80, b"ogg")
    assert result.error is None
    assert result.intake is not None
    assert result.intake.kind == ProjectKind.LIST
    assert "BG" in result.intake.text


async def test_same_phrase_adds_alias_without_second_project(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    ops = _ops(repository)
    result = await ops.handle_user_text(
        1, 1, "Проєкти BG — це той самий проєкт, що BG."
    )
    assert result.kind == ProjectKind.DONE
    aliased = await repository.resolve_user_project(1, "проєкти BG")
    assert aliased is not None
    assert aliased.name == "BG"
    assert len(await repository.list_projects_for_user(1)) == 1


async def test_project_tasks_resolve_alias(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG"), user=1, chat=1)
    await repository.add_alias(1, "BG", "проєкти BG")
    ops = _ops(repository)
    result = await ops.handle_user_text(1, 1, "Покажи задачі по BG.")
    assert result.kind == ProjectKind.TASKS
    assert "Agree the revised budget" in result.text
    assert "📁 BG" in result.text
