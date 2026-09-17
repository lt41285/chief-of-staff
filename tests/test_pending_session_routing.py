from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.project_command import ProjectIntent, ProjectIntentKind
from chief_of_staff.models.task_command import TaskIntent, TaskIntentKind
from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.conversation_context import (
    InMemoryConversationStore,
    PendingAmbiguity,
)
from chief_of_staff.services.lifecycle_format import ASK_WHICH_TIME
from chief_of_staff.services.lifecycle_format import CANCELLED as LIFE_CANCELLED
from chief_of_staff.services.lifecycle_format import COMPLETED
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.pending_session import PENDING_INTERPRET_FAILED
from chief_of_staff.services.project_format import ASK_CREATE_NAME, CANCELLED, format_create_prompt
from chief_of_staff.services.project_ops import ProjectKind, ProjectManagementService
from chief_of_staff.services.project_session import InMemoryProjectOpStore
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleKind, TaskLifecycleService
from chief_of_staff.services.task_query import QueryKind, TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_ai_router import ScriptedRouter
from tests.test_reminders import FrozenClock
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

CORRECTION = "Дивися, я просив тільки список проєктів, а ти показав цілий список завдань."
SHOW_PROJECTS = "Покажи тепер список проєктів."
NOT_CREATING = (
    "Я не створюю новий проєкт, я просто хочу подивитися на загальний список."
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


async def _project_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(ProjectRow)) or 0)


async def _turn(
    repository: SqlAlchemyTaskRepository,
    text: str,
    *,
    user: int = 1,
    chat: int = 1,
    router: ScriptedRouter | None = None,
    context: InMemoryConversationStore | None = None,
    projects: ProjectManagementService | None = None,
    lifecycle: TaskLifecycleService | None = None,
) -> object:
    store = context or InMemoryConversationStore(_clock())
    intake = TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository)
    ops = projects if projects is not None else _ops(repository)
    life = lifecycle or TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
    return await process_user_utterance(
        intake,
        user,
        chat,
        text,
        queries=TaskQueryService(repository),
        lifecycle=life,
        projects=ops,
        router=router,
        context=store,
    )


async def test_correction_and_show_projects_never_start_create(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(project="BG", task_title="Підготувати бюджет ради", people=()),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.GENERAL_CHAT, chat_reply="Дякую за уточнення!"),
            UtteranceInterpretation(kind=RouterKind.CREATE_PROJECT, project="список проєктів"),
        ]
    )
    first = await _turn(
        repository,
        CORRECTION,
        router=router,
        context=store,
        projects=ops,
    )
    assert first.kind == ProjectKind.LIST
    assert "📁 Проєкти" in first.text
    assert ASK_CREATE_NAME not in first.text
    assert ops.describe_pending(1, 1) is None
    second = await _turn(
        repository,
        SHOW_PROJECTS,
        router=router,
        context=store,
        projects=ops,
    )
    assert second.kind == ProjectKind.LIST
    assert ASK_CREATE_NAME not in second.text
    assert "Створити проєкт" not in second.text
    assert ops.describe_pending(1, 1) is None


async def test_pending_create_rejection_lists_projects_with_zero_writes(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ops = _ops(repository)
    asked = await ops.handle_intent(
        1, 1, ProjectIntent(kind=ProjectIntentKind.CREATE_PROJECT, new_name=None)
    )
    assert asked.text == ASK_CREATE_NAME
    before = await _project_count(session_factory)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.PROVIDE_PENDING_VALUE,
                pending_action="provide_requested_value",
                provided_value=NOT_CREATING,
            )
        ]
    )
    result = await _turn(
        repository,
        NOT_CREATING,
        router=router,
        projects=ops,
    )
    assert result.kind == ProjectKind.LIST
    assert format_create_prompt(NOT_CREATING) not in result.text
    assert NOT_CREATING not in result.text
    assert ops.describe_pending(1, 1) is None
    assert await _project_count(session_factory) == before


async def test_pending_create_cancel_skasuy(repository: SqlAlchemyTaskRepository) -> None:
    ops = _ops(repository)
    await ops.handle_intent(1, 1, ProjectIntent(kind=ProjectIntentKind.CREATE_PROJECT))
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.CANCEL_PENDING,
                pending_action="cancel_pending_action",
            )
        ]
    )
    result = await _turn(repository, "скасуй", router=router, projects=ops)
    assert result.kind == ProjectKind.CANCELLED
    assert result.text == CANCELLED
    assert ops.describe_pending(1, 1) is None


async def test_pending_create_switch_to_list_tasks(repository: SqlAlchemyTaskRepository) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Підготувати бюджет ради директорів",
            people=(),
        ),
        user=1,
        chat=1,
    )
    ops = _ops(repository)
    await ops.handle_intent(1, 1, ProjectIntent(kind=ProjectIntentKind.CREATE_PROJECT))
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.LIST_ALL_TASKS,
                pending_action="switch_intent",
            )
        ]
    )
    result = await _turn(repository, "ні, покажи задачі", router=router, projects=ops)
    assert result.kind == QueryKind.TASKS
    assert "Підготувати бюджет ради директорів" in result.text
    assert ops.describe_pending(1, 1) is None


async def test_pending_postpone_negation_does_not_write(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Поговорити з Тарасом про бюджет",
            people=(),
            deadline=date(2026, 9, 6),
        ),
        user=1,
        chat=1,
    )
    life = TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
    asked = await life.handle_intent(
        1,
        1,
        TaskIntent(kind=TaskIntentKind.POSTPONE_TASK, task_query="бюджет"),
        raw_text="перенеси задачу про бюджет",
    )
    assert asked.kind == LifecycleKind.ASK_DEADLINE
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.CANCEL_PENDING,
                pending_action="cancel_pending_action",
            )
        ]
    )
    result = await _turn(
        repository,
        "не перенось",
        router=router,
        lifecycle=life,
        projects=_ops(repository),
    )
    assert result.kind == LifecycleKind.CANCELLED
    assert result.text == LIFE_CANCELLED
    assert life.describe_pending(1, 1) is None
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.deadline == date(2026, 9, 6)


async def test_pending_actual_time_skip_completes_with_null(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Поговорити з Тарасом про бюджет",
            people=(),
        ),
        user=1,
        chat=1,
    )
    life = TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
    await life.handle_user_text(1, 1, "виконано поговорити з Тарасом про бюджет")
    done = await life.confirm(1, 1)
    assert done.kind == LifecycleKind.ASK_ACTUAL
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.CONTINUE_PENDING,
                pending_action="continue_pending_action",
                skip_actual_minutes=True,
            )
        ]
    )
    result = await _turn(
        repository,
        "не пам'ятаю, залиш без часу",
        router=router,
        lifecycle=life,
        projects=_ops(repository),
    )
    assert result.kind == LifecycleKind.DONE
    assert COMPLETED in result.text
    assert "Фактично" not in result.text
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "done"
        assert row.actual_minutes is None


async def test_pending_person_ambiguity_switch_lists_all(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(project="BG", task_title="Задача Андрія по статусу", people=("Андрій Боровець",)),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(project="BG", task_title="Задача Івана по статусу", people=("Іван Боровець",)),
        user=1,
        chat=1,
    )
    store = InMemoryConversationStore(_clock())
    store.remember_list(
        1,
        1,
        task_ids=(),
        titles=(),
        pending_ambiguity=PendingAmbiguity(
            original_message="покажи задачі Боровця",
            original_reference="Боровець",
            candidates=("Андрій Боровець", "Іван Боровець"),
            intent_kind="people_tasks_query",
            person_query="Боровець",
        ),
    )
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.LIST_ALL_TASKS,
                pending_action="switch_intent",
            )
        ]
    )
    result = await _turn(
        repository,
        "це не той, покажи всіх",
        router=router,
        context=store,
    )
    assert result.kind == QueryKind.TASKS
    assert "Задача Андрія по статусу" in result.text
    assert "Задача Івана по статусу" in result.text
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.pending_ambiguity is None


async def test_voice_pending_create_uses_same_ai_path(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ops = _ops(repository)
    await ops.handle_intent(1, 1, ProjectIntent(kind=ProjectIntentKind.CREATE_PROJECT))
    before = await _project_count(session_factory)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.LIST_PROJECTS,
                pending_action="switch_intent",
            )
        ]
    )
    intake = TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber([NOT_CREATING]),
        intake,
        projects=ops,
        queries=TaskQueryService(repository),
        lifecycle=TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock()),
        router=router,
        context=InMemoryConversationStore(_clock()),
    )
    voiced = await voice.handle_voice(1, 1, b"ogg")
    assert voiced.intake is not None
    assert voiced.intake.kind == ProjectKind.LIST
    assert ops.describe_pending(1, 1) is None
    assert await _project_count(session_factory) == before
    assert NOT_CREATING not in voiced.intake.text


async def test_openai_failure_while_pending_does_not_consume_text(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ops = _ops(repository)
    await ops.handle_intent(1, 1, ProjectIntent(kind=ProjectIntentKind.CREATE_PROJECT))
    before = await _project_count(session_factory)
    router = ScriptedRouter([RuntimeError("openai down")])
    result = await _turn(
        repository,
        NOT_CREATING,
        router=router,
        projects=ops,
    )
    assert result.kind == QueryKind.INFO
    assert result.text == PENDING_INTERPRET_FAILED
    assert ops.describe_pending(1, 1) is not None
    assert ops.describe_pending(1, 1)["awaiting"] == "project_name"
    assert await _project_count(session_factory) == before
    assert "Створити проєкт" not in result.text


STRATEGIC = "Поговорити про можливе ведення обліку стратегічних проектів"
BONUSES = "Виплатити премії лідерів Ліді Верещинській і Ірині Спасській"


async def test_bare_number_records_actual_minutes_on_completed_task_not_another(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    done_id = await persist(
        repository,
        complete_draft(project="BG", task_title=STRATEGIC, people=()),
        user=1,
        chat=1,
    )
    other_id = await persist(
        repository,
        complete_draft(project="BG", task_title=BONUSES, people=()),
        user=1,
        chat=1,
    )
    life = TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
    asked = await life.handle_user_text(1, 1, f"виконано {STRATEGIC}")
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    done = await life.confirm(1, 1)
    assert done.kind == LifecycleKind.ASK_ACTUAL
    pending = life.describe_pending(1, 1)
    assert pending is not None
    assert pending["pending_action"] == "record_actual_minutes"
    assert pending["awaiting"] == "actual_minutes"
    assert pending["task_id"] == str(done_id)

    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.COMPLETE_TASK,
                pending_action="switch_intent",
                task_query=BONUSES,
                task_index=10,
            )
        ]
    )
    result = await _turn(
        repository,
        "10",
        router=router,
        lifecycle=life,
        projects=_ops(repository),
    )
    assert router.calls == []
    assert result.kind == LifecycleKind.DONE
    assert result.text == "✅ Записав: 10 хв."
    assert BONUSES not in result.text
    assert LIFE_CANCELLED not in result.text
    assert life.describe_pending(1, 1) is None
    async with session_factory() as session:
        completed = await session.get(TaskRow, done_id)
        other = await session.get(TaskRow, other_id)
        assert completed is not None and other is not None
        assert completed.status == "done"
        assert completed.actual_minutes == 10
        assert other.status == "inbox"
        assert other.actual_minutes is None
        assert other.completed_at is None


async def test_unparsed_actual_time_reasks_without_selecting_another_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    done_id = await persist(
        repository,
        complete_draft(project="BG", task_title=STRATEGIC, people=()),
        user=1,
        chat=1,
    )
    other_id = await persist(
        repository,
        complete_draft(project="BG", task_title=BONUSES, people=()),
        user=1,
        chat=1,
    )
    life = TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
    await life.handle_user_text(1, 1, f"виконано {STRATEGIC}")
    await life.confirm(1, 1)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.COMPLETE_TASK,
                pending_action="switch_intent",
                task_query=BONUSES,
            )
        ]
    )
    result = await _turn(
        repository,
        "не зрозумів питання",
        router=router,
        lifecycle=life,
        projects=_ops(repository),
    )
    assert result.kind == LifecycleKind.ASK_ACTUAL
    assert ASK_WHICH_TIME in result.text
    assert BONUSES not in result.text
    assert life.describe_pending(1, 1) is not None
    async with session_factory() as session:
        completed = await session.get(TaskRow, done_id)
        other = await session.get(TaskRow, other_id)
        assert completed is not None and other is not None
        assert completed.actual_minutes is None
        assert other.status == "inbox"


async def test_natural_duration_replies_save_on_same_completed_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cases = (
        ("15", 15, "Talk with the finance team about ledger alpha"),
        ("10 хв", 10, "Talk with the finance team about ledger beta"),
        ("пів години", 30, "Talk with the finance team about ledger gamma"),
        ("1 година", 60, "Talk with the finance team about ledger delta"),
        ("1 год 20 хв", 80, "Talk with the finance team about ledger epsilon"),
        ("десь 45 хвилин", 45, "Talk with the finance team about ledger zeta"),
    )
    life = TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
    for text, minutes, title in cases:
        task_id = await persist(
            repository,
            complete_draft(project="BG", task_title=title, people=()),
            user=1,
            chat=1,
        )
        asked = await life.handle_user_text(1, 1, f"виконано {title}")
        assert asked.kind == LifecycleKind.ASK_CONFIRM, asked.text
        await life.confirm(1, 1)
        result = await _turn(
            repository,
            text,
            router=ScriptedRouter(
                [
                    UtteranceInterpretation(
                        kind=RouterKind.COMPLETE_TASK,
                        pending_action="switch_intent",
                        task_query=BONUSES,
                    )
                ]
            ),
            lifecycle=life,
            projects=_ops(repository),
        )
        assert result.kind == LifecycleKind.DONE
        assert f"{minutes} хв" in result.text or (
            minutes >= 60 and "год" in result.text
        )
        async with session_factory() as session:
            row = await session.get(TaskRow, task_id)
            assert row is not None
            assert row.actual_minutes == minutes
            assert row.status == "done"


async def test_actual_time_pending_can_switch_to_list_projects(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(project="BG", task_title=STRATEGIC, people=()),
        user=1,
        chat=1,
    )
    life = TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
    await life.handle_user_text(1, 1, f"виконано {STRATEGIC}")
    await life.confirm(1, 1)
    assert life.describe_pending(1, 1) is not None
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.LIST_PROJECTS,
                pending_action="switch_intent",
            )
        ]
    )
    result = await _turn(
        repository,
        "Покажи тепер список проєктів.",
        router=router,
        lifecycle=life,
        projects=_ops(repository),
    )
    assert result.kind.value == "list" or "📁 Проєкти" in result.text
    assert life.describe_pending(1, 1) is None
