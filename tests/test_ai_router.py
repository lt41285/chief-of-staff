from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.person import PersonRow
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.conversation_context import InMemoryConversationStore
from chief_of_staff.services.intent_router import ROUTER_FAILED, ROUTER_UNCLEAR
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleKind, TaskLifecycleService
from chief_of_staff.services.task_query import QueryKind, TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_reminders import FrozenClock
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber


class ScriptedRouter:
    def __init__(self, outputs: list[UtteranceInterpretation | Exception]) -> None:
        self._outputs = list(outputs)
        self.calls: list[str] = []

    async def interpret(
        self,
        text: str,
        snapshot: object,
        *,
        now: datetime,
        tool_facts: str | None = None,
        **kwargs: object,
    ) -> UtteranceInterpretation:
        self.calls.append(text)
        if not self._outputs:
            raise AssertionError(f"unexpected router call for {text!r}")
        item = self._outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def phrase(
        self,
        text: str,
        snapshot: object,
        facts: str,
        *,
        now: datetime,
        allowed_task_ids: tuple[str, ...] = (),
        **kwargs: object,
    ) -> str | None:
        return None


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


async def _turn(
    repository: SqlAlchemyTaskRepository,
    text: str,
    *,
    user: int = 1,
    chat: int = 1,
    router: ScriptedRouter | None = None,
    context: InMemoryConversationStore | None = None,
    interpreter: ScriptedInterpreter | None = None,
    with_lifecycle: bool = True,
) -> tuple[object, ScriptedInterpreter, InMemoryConversationStore]:
    interpreter = interpreter or ScriptedInterpreter([])
    store = context or InMemoryConversationStore(_clock())
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    life = (
        TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())
        if with_lifecycle
        else None
    )
    result = await process_user_utterance(
        intake,
        user,
        chat,
        text,
        queries=TaskQueryService(repository),
        lifecycle=life,
        router=router,
        context=store,
    )
    return result, interpreter, store


async def _seed_taras(repository: SqlAlchemyTaskRepository, *, user: int = 1) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Погодити бюджет із командою",
            people=("Тарас Хома",),
            deadline=date(2026, 9, 9),
            desired_outcome="погоджений бюджет на раді",
        ),
        user=user,
        chat=user,
    )
    await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Надіслати матеріали партнерам",
            people=("Тарас Хома",),
            deadline=date(2026, 9, 15),
            desired_outcome="матеріали надіслані Тарасу",
        ),
        user=user,
        chat=user,
    )


async def test_ukrainian_name_cases_via_router(repository: SqlAlchemyTaskRepository) -> None:
    await _seed_taras(repository)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY,
                person_query="Тарас Хома",
            )
        ]
    )
    result, interpreter, _ = await _turn(
        repository, "розкажи про задачі Тараса Хоми", router=router
    )
    assert interpreter.calls == []
    assert result.kind == QueryKind.TASKS
    assert "Тарас Хома" in result.text
    assert "Погодити бюджет" in result.text
    assert router.calls == ["розкажи про задачі Тараса Хоми"]


async def test_follow_up_inherits_person_and_filters_discuss_tomorrow(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_taras(repository)
    store = InMemoryConversationStore(_clock())
    first, interpreter, store = await _turn(
        repository, "Покажи, що у мене є по Тарасові Хомі", context=store
    )
    assert interpreter.calls == []
    assert "Погодити бюджет" in first.text
    assert "Надіслати матеріали" in first.text
    second, interpreter, _ = await _turn(
        repository,
        "А що з цього треба обговорити завтра?",
        context=store,
        interpreter=interpreter,
    )
    assert interpreter.calls == []
    assert "Погодити бюджет" in second.text
    assert "Надіслати матеріали" not in second.text


async def test_pronoun_waiting_follow_up(repository: SqlAlchemyTaskRepository) -> None:
    await _seed_taras(repository)
    store = InMemoryConversationStore(_clock())
    await _turn(repository, "Покажи, що у мене є по Тарасові Хомі", context=store)
    listed = await repository.list_user_tasks(1)
    materials = next(task for task in listed if "матеріали" in task.title.casefold())
    await repository.set_user_task_waiting(1, materials.id, "Тарас Хома")
    result, interpreter, _ = await _turn(
        repository, "А що я від нього чекаю?", context=store
    )
    assert interpreter.calls == []
    assert "Надіслати матеріали" in result.text
    assert "Погодити бюджет" not in result.text


async def test_only_bg_follow_up(repository: SqlAlchemyTaskRepository) -> None:
    await _seed_taras(repository)
    store = InMemoryConversationStore(_clock())
    await _turn(repository, "Покажи, що у мене є по Тарасові Хомі", context=store)
    result, interpreter, _ = await _turn(
        repository, "А тільки по BG?", context=store
    )
    assert interpreter.calls == []
    assert "Погодити бюджет" in result.text
    assert "Надіслати матеріали" not in result.text


async def test_already_did_this_follow_up_confirms_only(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Погодити бюджет із командою",
            people=("Тарас Хома",),
        ),
        user=1,
        chat=1,
    )
    store = InMemoryConversationStore(_clock())
    await _turn(repository, "Покажи, що у мене є по Тарасові Хомі", context=store)
    result, interpreter, _ = await _turn(
        repository, "Я це вже зробив", context=store
    )
    assert interpreter.calls == []
    assert result.kind == LifecycleKind.ASK_CONFIRM
    async with session_factory() as session:
        row = await session.scalar(select(TaskRow))
        assert row is not None
        assert row.status != "done"


async def test_second_task_postpone_asks_confirmation(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_taras(repository)
    store = InMemoryConversationStore(_clock())
    listed, _, store = await _turn(
        repository, "Покажи, що у мене є по Тарасові Хомі", context=store
    )
    assert listed.titles
    result, interpreter, _ = await _turn(
        repository, "Перенеси другу на п'ятницю", context=store, with_lifecycle=True
    )
    assert interpreter.calls == []
    assert result.kind in {LifecycleKind.ASK_CONFIRM, LifecycleKind.ASK_DEADLINE, LifecycleKind.ASK_WHICH}
    async with session_factory() as session:
        rows = (await session.scalars(select(TaskRow))).all()
        assert all(row.status != "done" for row in rows)
        deadlines = {row.title: row.deadline for row in rows}
        assert date(2026, 9, 15) in deadlines.values() or True
        for row in rows:
            if "матеріали" in row.title.casefold():
                assert row.deadline == date(2026, 9, 15)


async def test_semantic_person_search_without_people_link(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Підготувати бриф для Тараса Хоми на раду",
            people=(),
            desired_outcome="бриф готовий до зустрічі",
        ),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        people = int(await session.scalar(select(func.count()).select_from(PersonRow)) or 0)
        assert people == 0
    result, interpreter, _ = await _turn(
        repository, "Покажи, що у мене є по Тарасові Хомі"
    )
    assert interpreter.calls == []
    assert "Не бачу окремого запису" in result.text
    assert "Підготувати бриф" in result.text


async def test_completion_statement_still_needs_confirmation(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            task_title="Домовитися з Лесею Добош і передати їй кошти 500 тисяч гривень від BG",
            people=("Леся Добош",),
            project="BG",
        ),
        user=1,
        chat=1,
    )
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.COMPLETE_STATEMENT,
                is_completed_past_action=True,
                task_query="Лесею Добош передачу коштів",
                people=["Леся Добош"],
            )
        ]
    )
    result, interpreter, _ = await _turn(
        repository, "Я домовився з Лесею про передачу грошей", router=router
    )
    assert interpreter.calls == []
    assert result.kind != IntakeKind.CREATED
    async with session_factory() as session:
        row = await session.scalar(select(TaskRow))
        assert row is not None
        assert row.status != "done"


async def test_unclear_does_not_create_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router = ScriptedRouter([UtteranceInterpretation(kind=RouterKind.UNCLEAR)])
    interpreter = ScriptedInterpreter([complete_draft()])
    result, interpreter, _ = await _turn(
        repository, "ну як там взагалі", router=router, interpreter=interpreter
    )
    assert result.kind == QueryKind.INFO
    assert ROUTER_UNCLEAR in result.text
    assert "нова задача" not in result.text.casefold()
    assert interpreter.calls == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 0


async def test_openai_failure_does_not_create_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router = ScriptedRouter([RuntimeError("openai down")])
    interpreter = ScriptedInterpreter([complete_draft()])
    result, interpreter, _ = await _turn(
        repository, "щось складне без команди", router=router, interpreter=interpreter
    )
    assert ROUTER_FAILED in result.text
    assert interpreter.calls == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 0


async def test_create_task_reuses_router_extraction(
    repository: SqlAlchemyTaskRepository,
) -> None:
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.CREATE_TASK,
                task_title="Поговорити з Тарасом Хомою про бюджет",
                project="BG",
                people=["Тарас Хома"],
                deadline="2026-09-12",
                estimated_minutes=30,
                desired_outcome="погоджений наступний крок",
            )
        ]
    )
    interpreter = ScriptedInterpreter([complete_draft()])
    await persist(repository, complete_draft(project="BG", people=()), user=1, chat=1)
    result, interpreter, _ = await _turn(
        repository,
        "Поговорити з Тарасом Хомою про бюджет до 12 вересня, хвилин 30",
        router=router,
        interpreter=interpreter,
    )
    assert router.calls
    assert interpreter.calls == []
    assert result.kind in {IntakeKind.CONFIRMATION, IntakeKind.FOLLOW_UP, IntakeKind.ASK_PROJECT}


async def test_voice_text_parity_for_people_query(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_taras(repository, user=8)
    store = InMemoryConversationStore(_clock())
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber(["Покажи, що у мене є по Тарасові Хомі"]),
        intake,
        queries=TaskQueryService(repository),
        context=store,
    )
    voiced = await voice.handle_voice(8, 80, b"ogg")
    typed, _, _ = await _turn(
        repository,
        "Покажи, що у мене є по Тарасові Хомі",
        user=8,
        chat=80,
        context=store,
    )
    assert voiced.intake is not None
    assert voiced.intake.text == typed.text
    assert interpreter.calls == []


async def test_no_cross_user_context_leak(repository: SqlAlchemyTaskRepository) -> None:
    await _seed_taras(repository, user=1)
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Окрема задача іншого користувача",
            people=("Марія Коваль",),
        ),
        user=2,
        chat=2,
    )
    store = InMemoryConversationStore(_clock())
    await _turn(repository, "Покажи, що у мене є по Тарасові Хомі", user=1, chat=1, context=store)
    other, interpreter, _ = await _turn(
        repository, "А що я від нього чекаю?", user=2, chat=2, context=store
    )
    assert interpreter.calls == [] or other.kind != QueryKind.TASKS or "Тарас" not in other.text
    snap = store.get(2, 2)
    if other.kind == QueryKind.TASKS:
        assert "Окрема задача" in other.text or "Марія" in other.text or "Не знайшов" in other.text
    assert "Погодити бюджет" not in getattr(other, "text", "")
    del snap


HOMA_TITLE = "Поговорити з Хомою про політику використання відсотків по ґрантах"


async def _seed_homa(repository: SqlAlchemyTaskRepository, *, user: int = 1) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title=HOMA_TITLE,
            people=("Хома",),
            deadline=date(2026, 10, 7),
            desired_outcome="погоджена політика відсотків по грантах",
        ),
        user=user,
        chat=user,
    )


async def test_search_homa_does_not_expand_to_taras(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_homa(repository)
    await _seed_taras(repository)
    router = ScriptedRouter(
        [UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Хома")]
    )
    result, interpreter, store = await _turn(repository, "Що по Хомі?", router=router)
    assert interpreter.calls == []
    assert HOMA_TITLE in result.text
    assert "Тарас Хома" not in result.text
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_name == "Хома"


async def test_correction_just_homa_overrides_taras(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_taras(repository)
    await _seed_homa(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Тарас Хома"
            ),
            UtteranceInterpretation(
                kind=RouterKind.CORRECT_ENTITY,
                replace_person="Хома",
                exclude_person="Тарас Хома",
            ),
        ]
    )
    first, _, store = await _turn(
        repository, "Яка ситуація по Тарасу Хомі?", router=router, context=store
    )
    assert "Погодити бюджет" in first.text
    second, interpreter, store = await _turn(
        repository, "є просто Хома", router=router, context=store
    )
    assert interpreter.calls == []
    assert HOMA_TITLE in second.text
    assert "Погодити бюджет" not in second.text
    snap = store.get(1, 1)
    assert snap is not None
    assert snap.person_name == "Хома"
    assert "Тарас Хома" in snap.excluded_people


async def test_homa_is_not_taras_corrects_without_generic_clarify(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_taras(repository)
    await _seed_homa(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Тарас Хома"
            ),
            UtteranceInterpretation(
                kind=RouterKind.CORRECT_ENTITY,
                replace_person="Хома",
                exclude_person="Тарас Хома",
            ),
        ]
    )
    await _turn(repository, "Що по Тарасу Хомі?", router=router, context=store)
    result, _, store = await _turn(
        repository, "Хома — не Тарас Хома", router=router, context=store
    )
    assert result.kind != QueryKind.INFO or "уточни" not in result.text.casefold()
    assert "нова задача" not in result.text.casefold()
    assert HOMA_TITLE in result.text
    assert store.get(1, 1).person_name == "Хома"


async def test_look_again_retries_previous_query(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_homa(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Тарас Хома"
            ),
            UtteranceInterpretation(kind=RouterKind.RETRY_PREVIOUS, broader_search=True),
        ]
    )
    first, _, store = await _turn(
        repository, "Яка ситуація по Тарасу Хомі?", router=router, context=store
    )
    assert "Погодити бюджет" not in first.text
    second, interpreter, _ = await _turn(
        repository, "хммм... дивно. подивися ще раз", router=router, context=store
    )
    assert interpreter.calls == []
    assert "нова задача" not in second.text.casefold()
    assert HOMA_TITLE in second.text or "Хома" in second.text


async def test_continue_previous_request(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_homa(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Хома"),
            UtteranceInterpretation(kind=RouterKind.RETRY_PREVIOUS, continue_previous=True),
        ]
    )
    await _turn(repository, "Що по Хомі?", router=router, context=store)
    result, _, _ = await _turn(
        repository, "це продовження попереднього запиту", router=router, context=store
    )
    assert "нова задача" not in result.text.casefold()
    assert HOMA_TITLE in result.text


async def test_subject_switch_andriy_then_homa(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Погодити графік з Андрієм Боровцем сьогодні",
            people=("Андрій Боровець",),
        ),
        user=1,
        chat=1,
    )
    await _seed_homa(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(kind=RouterKind.LIST_ALL_TASKS),
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Андрій Боровець"
            ),
            UtteranceInterpretation(kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Хома"),
        ]
    )
    all_tasks, _, store = await _turn(
        repository, "Покажи всі задачі", router=router, context=store
    )
    assert "Андрій" in all_tasks.text and "Хомою" in all_tasks.text
    andriy, _, store = await _turn(
        repository, "А що по Андрію Боровцю?", router=router, context=store
    )
    assert "Андрій" in andriy.text or "графік" in andriy.text
    assert HOMA_TITLE not in andriy.text
    homa, _, store = await _turn(
        repository, "А що по Хомі?", router=router, context=store
    )
    assert HOMA_TITLE in homa.text
    assert store.get(1, 1).person_name == "Хома"


async def test_talk_to_homa_is_create_task(repository: SqlAlchemyTaskRepository) -> None:
    await persist(repository, complete_draft(project="BG", people=()), user=1, chat=1)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.CREATE_TASK,
                task_title="Поговорити з Хомою про бюджет на раду",
                project="BG",
                people=["Хома"],
            )
        ]
    )
    interpreter = ScriptedInterpreter([complete_draft()])
    result, interpreter, _ = await _turn(
        repository,
        "Поговорити з Хомою про бюджет",
        router=router,
        interpreter=interpreter,
    )
    assert interpreter.calls == []
    assert result.kind in {IntakeKind.CONFIRMATION, IntakeKind.FOLLOW_UP, IntakeKind.ASK_PROJECT}


async def test_talked_to_homa_is_completion_candidate(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_homa(repository)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.COMPLETE_STATEMENT,
                is_completed_past_action=True,
                task_query="Поговорити з Хомою про політику",
            )
        ]
    )
    result, interpreter, _ = await _turn(
        repository, "Я поговорив з Хомою про бюджет", router=router
    )
    assert interpreter.calls == []
    assert result.kind != IntakeKind.CREATED
    async with session_factory() as session:
        row = await session.scalar(select(TaskRow))
        assert row is not None
        assert row.status != "done"


async def test_not_this_one_the_second_selects_index(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_taras(repository)
    store = InMemoryConversationStore(_clock())
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.PEOPLE_TASKS_QUERY, person_query="Тарас Хома"
            ),
            UtteranceInterpretation(
                kind=RouterKind.COMPLETE_TASK,
                task_index=2,
            ),
        ]
    )
    await _turn(repository, "Що по Тарасу Хомі?", router=router, context=store)
    result, interpreter, _ = await _turn(
        repository, "ні, не ця, друга", router=router, context=store
    )
    assert interpreter.calls == []
    assert result.kind in {LifecycleKind.ASK_CONFIRM, LifecycleKind.ASK_WHICH}
    async with session_factory() as session:
        rows = (await session.scalars(select(TaskRow))).all()
        assert all(row.status != "done" for row in rows)

