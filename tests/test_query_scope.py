from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import INFORMAL_ADDRESS_ACK, process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.utterance_intent import (
    GroundedReply,
    QueryRelation,
    RouterKind,
    UtteranceInterpretation,
)
from chief_of_staff.prompts.conversation import phrase_instructions_for
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.conversation_context import (
    ConversationSnapshot,
    InMemoryConversationStore,
)
from chief_of_staff.services.daily_planning import DailyPlanningService
from chief_of_staff.services.followup_intent import parse_context_followup
from chief_of_staff.services.intent_router import IntentRouter, apply_context
from chief_of_staff.services.planning_session import InMemoryPlanningSessionStore
from chief_of_staff.services.project_ops import ProjectManagementService
from chief_of_staff.services.project_session import InMemoryProjectOpStore
from chief_of_staff.services.query_scope import looks_like_drop_person_filter
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_query import TaskQueryService
from chief_of_staff.services.task_query_format import ASK_WHICH_PERSON, format_unknown_person
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_ai_router import ScriptedRouter, _clock
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

DOBKO_LONG = "Підготувати досьє по Добко"
OTHER_SHORT = "Написати тижневий апдейт по BG"
DOBKO_OVERDUE = "Прострочений дзвінок Добку"
OTHER_OVERDUE = "Прострочений звіт для ради"
HOUR = "Буду мати завтра вільну годину. Що можу встигнути за цей час?"


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


async def _seed(repository: SqlAlchemyTaskRepository) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title=DOBKO_LONG,
            people=("Добко",),
            deadline=date(2026, 9, 20),
            estimated_minutes=90,
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title=OTHER_SHORT,
            people=("Марія",),
            deadline=date(2026, 9, 9),
            estimated_minutes=20,
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title=DOBKO_OVERDUE,
            people=("Добко",),
            deadline=date(2026, 9, 1),
            estimated_minutes=15,
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title=OTHER_OVERDUE,
            people=(),
            deadline=date(2026, 9, 1),
            estimated_minutes=30,
        ),
        user=1,
        chat=1,
    )


async def _turn(
    repository: SqlAlchemyTaskRepository,
    text: str,
    *,
    router: ScriptedRouter,
    context: InMemoryConversationStore,
    planning: DailyPlanningService | None = None,
    projects: ProjectManagementService | None = None,
) -> object:
    intake = TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository)
    return await process_user_utterance(
        intake,
        1,
        1,
        text,
        planning=planning,
        projects=projects,
        queries=TaskQueryService(repository),
        router=router,
        context=context,
    )


def _planning(repository: SqlAlchemyTaskRepository) -> DailyPlanningService:
    return DailyPlanningService(repository, InMemoryPlanningSessionStore(), _clock())


def test_drop_person_filter_phrases() -> None:
    assert looks_like_drop_person_filter("Взагалі не тільки для Добка.")
    assert looks_like_drop_person_filter("не тільки по ньому")
    assert looks_like_drop_person_filter("без фільтра по людині")


def test_planning_hour_does_not_inherit_person() -> None:
    snap_person = type(
        "S",
        (),
        {
            "person_query": "Добко",
            "person_name": "Добко",
            "last_query_kind": "people_tasks_query",
            "last_period": None,
            "last_available_minutes": None,
            "project_name": None,
            "status_filter": None,
            "task_ids": (),
            "titles": (),
        },
    )()
    interp = UtteranceInterpretation(
        kind=RouterKind.PEOPLE_TASKS_QUERY,
        person_query="Добко",
        inherit_context=True,
        available_minutes=60,
    )
    scoped = apply_context(interp, snap_person, HOUR)  # type: ignore[arg-type]
    assert scoped.query_relation == QueryRelation.NEW_INTENT
    assert scoped.kind == RouterKind.FIT_MINUTES
    assert scoped.person_query is None


async def test_dubok_correction_then_planning_is_global(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    planning = _planning(repository)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Дубок"),
            UtteranceInterpretation(kind=RouterKind.CORRECT_ENTITY, replace_person="Добко"),
            UtteranceInterpretation(kind=RouterKind.RETRY_PREVIOUS, continue_previous=True),
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY,
                person_query="Добко",
                inherit_context=True,
                available_minutes=60,
            ),
        ]
    )
    await _turn(repository, "Що є по Дубку?", router=router, context=store)
    await _turn(repository, "Добко.", router=router, context=store)
    snap = store.get(1, 1)
    assert snap is not None
    assert (snap.person_query or snap.person_name or "").casefold().startswith("добк")
    assert "Дубок" not in (snap.person_query or "")
    broadened = await _turn(repository, "Розшир пошук.", router=router, context=store)
    assert DOBKO_LONG in broadened.text or "Добко" in broadened.text
    planned = await _turn(
        repository, HOUR, router=router, context=store, planning=planning
    )
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query is None
    assert snap.person_name is None
    assert OTHER_OVERDUE in planned.text
    assert "по Добко" not in planned.text
    assert "питань по Добко" not in planned.text


async def test_overdue_refine_keeps_dobko(repository: SqlAlchemyTaskRepository) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Добко"),
            UtteranceInterpretation(kind=RouterKind.REFINE_PREVIOUS, include_overdue=True),
        ]
    )
    await _turn(repository, "Що є по Добко?", router=router, context=store)
    overdue = await _turn(repository, "А прострочені?", router=router, context=store)
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query == "Добко" or snap.person_name == "Добко"
    assert DOBKO_OVERDUE in overdue.text
    assert OTHER_OVERDUE not in overdue.text
    assert OTHER_SHORT not in overdue.text


async def test_not_only_dobko_reruns_globally(repository: SqlAlchemyTaskRepository) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Добко"),
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY,
                person_query="Добко",
                inherit_context=True,
            ),
        ]
    )
    await _turn(repository, "Що є по Добко?", router=router, context=store)
    result = await _turn(
        repository, "Взагалі не тільки для Добка.", router=router, context=store
    )
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query is None
    assert OTHER_SHORT in result.text
    assert OTHER_OVERDUE in result.text or "Unity" in result.text


async def test_list_projects_is_global(repository: SqlAlchemyTaskRepository) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    projects = ProjectManagementService(repository, InMemoryProjectOpStore())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Добко"),
            UtteranceInterpretation(kind=RouterKind.LIST_PROJECTS),
        ]
    )
    await _turn(repository, "Що є по Добко?", router=router, context=store)
    listed = await _turn(
        repository,
        "Покажи всі проєкти.",
        router=router,
        context=store,
        projects=projects,
    )
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query is None
    assert "Добко" not in listed.text
    assert "BG" in listed.text or "проєкт" in listed.text.casefold()


async def test_global_overdue_query_does_not_keep_person(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Добко"),
            UtteranceInterpretation(kind=RouterKind.LIST_ALL_TASKS, include_overdue=True),
        ]
    )
    await _turn(repository, "Що є по Добко?", router=router, context=store)
    overdue = await _turn(
        repository, "Які задачі прострочені?", router=router, context=store
    )
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query is None
    assert OTHER_OVERDUE in overdue.text
    assert DOBKO_OVERDUE in overdue.text


async def test_voice_planning_resets_person_filter(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    planning = _planning(repository)
    typed = ScriptedRouter(
        [UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Добко")]
    )
    await _turn(repository, "Що є по Добко?", router=typed, context=store)
    intake = TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber([HOUR]),
        intake,
        planning=planning,
        queries=TaskQueryService(repository),
        router=ScriptedRouter(
            [
                UtteranceInterpretation(
                    kind=RouterKind.PEOPLE_TASKS_QUERY,
                    person_query="Добко",
                    inherit_context=True,
                    available_minutes=60,
                )
            ]
        ),
        context=store,
    )
    voiced = await voice.handle_voice(1, 1, b"ogg")
    assert voiced.intake is not None
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query is None
    assert OTHER_OVERDUE in voiced.intake.text
    assert "по Добко" not in voiced.intake.text


async def test_correction_updates_query_context(repository: SqlAlchemyTaskRepository) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Дубок"),
            UtteranceInterpretation(kind=RouterKind.CORRECT_ENTITY, replace_person="Добко"),
        ]
    )
    await _turn(repository, "Що є по Дубку?", router=router, context=store)
    result = await _turn(repository, "Добко.", router=router, context=store)
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query == "Добко" or snap.person_name == "Добко"
    assert DOBKO_LONG in result.text


async def test_broader_search_keeps_subject(repository: SqlAlchemyTaskRepository) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Добко"),
            UtteranceInterpretation(kind=RouterKind.RETRY_PREVIOUS, continue_previous=True),
        ]
    )
    await _turn(repository, "Що є по Добко?", router=router, context=store)
    result = await _turn(repository, "Розшир пошук.", router=router, context=store)
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query == "Добко" or snap.person_name == "Добко"
    assert DOBKO_LONG in result.text


async def test_not_only_him_removes_person_filter(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Добко"),
            UtteranceInterpretation(kind=RouterKind.REFINE_PREVIOUS, inherit_context=True),
        ]
    )
    await _turn(repository, "Що є по Добко?", router=router, context=store)
    result = await _turn(repository, "не тільки по ньому", router=router, context=store)
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_query is None
    assert OTHER_SHORT in result.text


async def test_informal_address_is_persisted(repository: SqlAlchemyTaskRepository) -> None:
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.GENERAL_CHAT, address_form="informal"),
            UtteranceInterpretation(kind=RouterKind.LIST_ALL_TASKS),
        ]
    )
    first = await _turn(
        repository,
        "Можеш говорити до мене ти, а не ви.",
        router=router,
        context=store,
    )
    assert INFORMAL_ADDRESS_ACK in first.text
    form = await repository.get_address_form(1)
    assert form == "informal"
    instructions = phrase_instructions_for(form)
    assert "ти / тебе" in instructions
    assert "Never use ви" in instructions

    class RecordingClient:
        def __init__(self) -> None:
            self.instructions: str | None = None

        async def parse_structured(
            self, user_input: str, instructions: str, text_format: object
        ) -> GroundedReply:
            self.instructions = instructions
            return GroundedReply(message="Ок.", referenced_task_ids=[], stated_count=0)

    client = RecordingClient()
    spoken = await IntentRouter(client).phrase(
        "які задачі?",
        store.get(1, 1),
        "Немає відкритих задач.",
        now=datetime(2026, 9, 8),
        address_form="informal",
    )
    assert spoken is not None
    assert client.instructions is not None
    assert "ти / тебе" in client.instructions
    assert "ви / вас" in client.instructions


def test_unknown_person_never_renders_placeholder() -> None:
    assert format_unknown_person("") == ASK_WHICH_PERSON
    assert format_unknown_person("—") == ASK_WHICH_PERSON
    assert format_unknown_person("–") == ASK_WHICH_PERSON
    assert "—" not in format_unknown_person("—")
    assert "Боровець" in format_unknown_person("Боровець")


def test_status_followup_inherits_person_from_snapshot() -> None:
    snap = ConversationSnapshot(person_name="Боровець", person_query="Боровець")
    for text, status in (
        ("А архівні задачі?", "done"),
        ("А виконані задачі?", "done"),
        ("А відкриті задачі?", None),
    ):
        follow = parse_context_followup(text, snap, today=date(2026, 9, 17))
        assert follow is not None, text
        assert follow.person_query == "Боровець", text
        assert follow.status_filter == status, text


def test_ai_dash_person_on_status_followup_keeps_snapshot_person() -> None:
    snap = ConversationSnapshot(person_name="Боровець", person_query="Боровець")
    interp = UtteranceInterpretation(
        kind=RouterKind.PEOPLE_TASKS_QUERY,
        person_query="—",
        query_relation=QueryRelation.NEW_QUERY,
        status_filter="done",
    )
    scoped = apply_context(interp, snap, "А виконані задачі?")
    assert scoped.query_relation == QueryRelation.REFINE_QUERY
    assert scoped.person_query == "Боровець"
    assert scoped.status_filter == "done"
    assert scoped.kind == RouterKind.PEOPLE_TASKS_QUERY


async def test_done_followup_keeps_person_not_placeholder(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(repository)
    async with session_factory() as session:
        async with session.begin():
            row = await session.scalar(
                select(TaskRow).where(TaskRow.title == DOBKO_LONG)
            )
            assert row is not None
            row.status = "done"
            row.completed_at = datetime(2026, 9, 16, 12, 0, tzinfo=KYIV)
    store = InMemoryConversationStore(_clock())
    hostile = UtteranceInterpretation(
        kind=RouterKind.PEOPLE_TASKS_QUERY,
        person_query="—",
        query_relation=QueryRelation.NEW_QUERY,
        status_filter="done",
    )
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Добко"),
            hostile,
            hostile,
        ]
    )
    opened = await _turn(repository, "Покажи відкриті задачі по Добко", router=router, context=store)
    assert "«—»" not in opened.text
    assert DOBKO_OVERDUE in opened.text
    assert DOBKO_LONG not in opened.text
    archived = await _turn(repository, "А архівні задачі?", router=router, context=store)
    assert "«—»" not in archived.text
    assert ASK_WHICH_PERSON not in archived.text
    assert "не знайшов людину" not in archived.text.casefold()
    assert DOBKO_LONG in archived.text
    done = await _turn(repository, "А виконані задачі?", router=router, context=store)
    assert "«—»" not in done.text
    assert ASK_WHICH_PERSON not in done.text
    assert "не знайшов людину" not in done.text.casefold()
    assert DOBKO_LONG in done.text
    snap = store.get(1, 1)
    assert snap is not None
    assert (snap.person_query or snap.person_name or "").casefold().startswith("добк")
    assert snap.status_filter == "done"
    named = await _turn(
        repository,
        "А виконані задачі по Добко?",
        router=ScriptedRouter(
            [
                UtteranceInterpretation(
                    kind=RouterKind.PEOPLE_TASKS_QUERY,
                    person_query="Добко",
                    status_filter="done",
                )
            ]
        ),
        context=store,
    )
    assert DOBKO_LONG in named.text
    assert "«—»" not in named.text
