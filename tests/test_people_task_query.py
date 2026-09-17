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
from chief_of_staff.models.task_command import TaskIntent, TaskIntentKind
from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.conversation_context import InMemoryConversationStore, PendingAmbiguity
from chief_of_staff.services.followup_intent import looks_like_listed_status_dispute
from chief_of_staff.services.people_query import parse_people_tasks_query
from chief_of_staff.services.person_ambiguity import parse_person_ambiguity_reply
from chief_of_staff.services.person_match import PersonHit, name_stems, resolve_person
from chief_of_staff.services.task_command_intent import (
    looks_like_status_list_query,
    parse_task_intent_deterministic,
)
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_query import QueryKind, TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.tool_runtime import execute_grounded_tools
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_ai_router import ScriptedRouter, _clock, _turn
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

PEOPLE_QUERY_PHRASES = (
    "Покажи, що у мене є по Тарасові Хомі",
    "Покажи все по Тарасові Хомі",
    "Що у мене є по Тарасові Хомі?",
    "Які задачі по Тарасові Хомі?",
    "Які в мене задачі по Тарасові Хомі?",
    "Що треба обговорити з Тарасом Хомою?",
    "Покажи задачі з Тарасом Хомою",
    "Які задачі пов'язані з Тарасом Хомою?",
    "Що я чекаю від Тараса Хоми?",
    "Покажи все, де є Тарас Хома",
    "Show me everything related to Taras Khoma",
    "What tasks do I have with Taras Khoma?",
    "What do I need to discuss with Taras Khoma?",
    "What am I waiting for from Taras Khoma?",
)

INTAKE_PHRASES = (
    "Поговорити з Тарасом Хомою",
    "Pоговорити з Тарасом Хомою",
    "Треба поговорити з Тарасом Хомою",
    "Домовитися з Тарасом Хомою",
    "Надіслати Тарасові Хомі матеріали",
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


def test_people_query_phrases_are_read_only() -> None:
    for text in PEOPLE_QUERY_PHRASES:
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.PEOPLE_TASKS_QUERY, text
        assert parsed.person_query, text
        assert parse_people_tasks_query(text) is not None, text


def test_waiting_and_discuss_filters() -> None:
    waiting = parse_task_intent_deterministic("Що я чекаю від Тараса Хоми?")
    assert waiting is not None
    assert waiting.status_filter == "waiting"
    assert waiting.task_query is None
    waiting_en = parse_task_intent_deterministic("What am I waiting for from Taras Khoma?")
    assert waiting_en is not None
    assert waiting_en.status_filter == "waiting"
    show_waiting = parse_task_intent_deterministic("Покажи waiting по Тарасові Хомі")
    assert show_waiting is not None
    assert show_waiting.status_filter == "waiting"
    discuss = parse_task_intent_deterministic("Що треба обговорити з Тарасом Хомою?")
    assert discuss is not None
    assert discuss.status_filter is None
    assert discuss.task_query == "discuss"


def test_done_tasks_by_person_are_a_people_query() -> None:
    for text in (
        "Чи є виконані задачі по Боровцю?",
        "Які виконані задачі по Боровцю?",
        "Покажи архівні задачі по Андрію Боровцю",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind == TaskIntentKind.PEOPLE_TASKS_QUERY, text
        assert parsed.status_filter == "done", text
        assert parsed.person_query, text
        assert "боров" in parsed.person_query.casefold(), text


def test_done_list_is_not_a_complete_command() -> None:
    parsed = parse_task_intent_deterministic("Чи є виконані задачі по Боровцю?")
    assert parsed is not None
    assert parsed.kind != TaskIntentKind.COMPLETE_TASK


def test_open_tasks_by_inflected_surname_are_a_people_query() -> None:
    parsed = parse_task_intent_deterministic("які відкриті задачі є по Боровцю?")
    assert parsed is not None
    assert parsed.kind == TaskIntentKind.PEOPLE_TASKS_QUERY
    assert parsed.status_filter is None
    assert parsed.person_query
    assert "боров" in parsed.person_query.casefold()


QUERY_KINDS = {
    TaskIntentKind.PEOPLE_TASKS_QUERY,
    TaskIntentKind.LIST_TASKS,
    TaskIntentKind.LIST_PROJECT_TASKS,
    TaskIntentKind.LIST_ALL_TASKS,
}


def test_status_list_markers_bypass_ai_classifier() -> None:
    for text in (
        "Чи є виконані задачі по Боровцю?",
        "які відкриті задачі є по Боровцю?",
        "Покажи waiting по Тарасові Хомі",
    ):
        assert looks_like_status_list_query(text), text
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind in QUERY_KINDS, text
    assert not looks_like_status_list_query("я виконав лист до IFC")
    assert not looks_like_status_list_query("Також домовився з Боровцем про зустріч")
    assert looks_like_listed_status_dispute(
        "я закрив ці задачі. Ти кажеш, що вони відкриті..."
    )
    assert not looks_like_listed_status_dispute("я закрив ці задачі")


def test_intake_phrases_are_not_people_queries() -> None:
    for text in INTAKE_PHRASES:
        parsed = parse_task_intent_deterministic(text)
        assert parsed is None or parsed.kind != TaskIntentKind.PEOPLE_TASKS_QUERY, text
        assert parse_people_tasks_query(text) is None, text


def test_project_lists_are_not_people_queries() -> None:
    for text in (
        "Які задачі по BG",
        "show all tasks for BG",
        "список всіх завдань по проєкту BG",
        "Дай список завдань з усіх проєктів.",
    ):
        parsed = parse_task_intent_deterministic(text)
        assert parsed is not None, text
        assert parsed.kind != TaskIntentKind.PEOPLE_TASKS_QUERY, text


def test_inflected_forms_share_stems() -> None:
    canonical = name_stems("Тарас Хома")
    for form in (
        "Тарас Хома",
        "Тараса Хоми",
        "Тарасові Хомі",
        "Тарасом Хомою",
        "з Тарасом Хомою",
    ):
        assert name_stems(form.replace("з ", "")) == canonical, form
    people = [PersonHit(name="Тарас Хома")]
    for form in ("Тарасові Хомі", "Тарасом Хомою", "Тараса Хоми", "Тарас Хома"):
        matched = resolve_person(form, people)
        assert matched.selected is not None, form
        assert matched.selected.name == "Тарас Хома"


async def _ask(
    repository: SqlAlchemyTaskRepository,
    text: str,
    *,
    user: int = 1,
    chat: int = 1,
    interpreter: ScriptedInterpreter | None = None,
) -> tuple[object, ScriptedInterpreter]:
    interpreter = interpreter or ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    result = await process_user_utterance(
        intake,
        user,
        chat,
        text,
        queries=TaskQueryService(repository),
    )
    return result, interpreter


async def _seed_taras(repository: SqlAlchemyTaskRepository, *, user: int = 1) -> tuple[object, object]:
    budget = await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Погодити бюджет із командою",
            people=("Тарас Хома",),
            deadline=date(2026, 9, 12),
            estimated_minutes=30,
            desired_outcome="погоджений бюджет",
        ),
        user=user,
        chat=user,
    )
    materials = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            task_title="Надіслати матеріали партнерам",
            people=("Тарас Хома",),
            deadline=date(2026, 9, 15),
            estimated_minutes=20,
            desired_outcome="матеріали надіслані",
        ),
        user=user,
        chat=user,
    )
    return budget, materials


async def test_show_taras_lists_open_tasks_and_does_not_create(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_taras(repository)
    async with session_factory() as session:
        people_before = int(await session.scalar(select(func.count()).select_from(PersonRow)) or 0)
        tasks_before = int(await session.scalar(select(func.count()).select_from(TaskRow)) or 0)
    for text in (
        "Покажи, що у мене є по Тарасові Хомі",
        "Що у мене є по Тарасові Хомі?",
        "Які задачі по Тарасові Хомі?",
    ):
        result, interpreter = await _ask(repository, text)
        assert result.kind == QueryKind.TASKS, text
        assert interpreter.calls == [], text
        assert "👤 Тарас Хома" in result.text
        assert "Погодити бюджет із командою" in result.text
        assert "Надіслати матеріали партнерам" in result.text
        assert "📁 BG" in result.text
        assert "📁 Unity Center" in result.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PersonRow)) == people_before
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == tasks_before


async def test_inflected_queries_resolve_same_person(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_taras(repository)
    texts = (
        "Покажи все по Тарасові Хомі",
        "Покажи задачі з Тарасом Хомою",
        "Що у мене є по Тараса Хоми?",
        "Покажи все, де є Тарас Хома",
    )
    outputs: list[str] = []
    for text in texts:
        result, interpreter = await _ask(repository, text)
        assert result.kind == QueryKind.TASKS, text
        assert interpreter.calls == []
        assert "Погодити бюджет із командою" in result.text
        outputs.append(result.text)
    assert all("👤 Тарас Хома" in text for text in outputs)


async def test_discuss_returns_open_related_tasks(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_taras(repository)
    result, interpreter = await _ask(repository, "Що треба обговорити з Тарасом Хомою?")
    assert interpreter.calls == []
    assert result.kind == QueryKind.TASKS
    assert "Погодити бюджет із командою" in result.text
    assert "Надіслати матеріали партнерам" in result.text


async def test_waiting_only_query(
    repository: SqlAlchemyTaskRepository,
) -> None:
    budget_id, materials_id = await _seed_taras(repository)
    await repository.set_user_task_waiting(1, materials_id, "Тарас Хома")
    result, interpreter = await _ask(repository, "Що я чекаю від Тараса Хоми?")
    assert interpreter.calls == []
    assert result.kind == QueryKind.TASKS
    assert "Надіслати матеріали партнерам" in result.text
    assert "Погодити бюджет із командою" not in result.text
    assert "⏳ Чекаю від: Тарас Хома" in result.text
    listed, _ = await _ask(repository, "Покажи, що у мене є по Тарасові Хомі")
    assert "Погодити бюджет із командою" in listed.text
    assert "Надіслати матеріали партнерам" in listed.text
    assert listed.text.count("Надіслати матеріали партнерам") == 1
    del budget_id


async def test_unknown_person_creates_nothing(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    result, interpreter = await _ask(repository, "Покажи, що у мене є по Тарасові Хомі")
    assert interpreter.calls == []
    assert result.kind == QueryKind.INFO
    assert "Не знайшов людину «Тарасові Хомі» серед людей у твоїх задачах." in result.text
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PersonRow)) == 0
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 0


async def test_ambiguous_partial_name_shows_picker(
    repository: SqlAlchemyTaskRepository,
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
    await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Підписати договір з Олегом",
            people=("Олег Хома",),
        ),
        user=1,
        chat=1,
    )
    result, interpreter = await _ask(repository, "Покажи все, де є Хома")
    assert interpreter.calls == []
    assert result.kind == QueryKind.INFO
    assert "Тарас Хома" in result.text
    assert "Олег Хома" in result.text
    assert "увазі" in result.text.casefold() or "уточни" in result.text.casefold()


async def test_done_and_cancelled_excluded(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    open_id, done_id = await _seed_taras(repository)
    cancelled_id = await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Скасована зустріч з партнером",
            people=("Тарас Хома",),
        ),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            done = await session.get(TaskRow, done_id)
            cancelled = await session.get(TaskRow, cancelled_id)
            assert done and cancelled
            done.status = "done"
            done.completed_at = datetime.now(KYIV)
            cancelled.status = "cancelled"
    result, interpreter = await _ask(repository, "Покажи, що у мене є по Тарасові Хомі")
    assert interpreter.calls == []
    assert "Погодити бюджет із командою" in result.text
    assert "Надіслати матеріали партнерам" not in result.text
    assert "Скасована зустріч" not in result.text
    del open_id


async def test_person_only_on_done_task_is_empty_not_unknown(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(project="BG", people=("Тарас Хома",), task_title="Закрита справа по бюджету"),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, task_id)
            assert row is not None
            row.status = "done"
            row.completed_at = datetime.now(KYIV)
    result, interpreter = await _ask(repository, "Покажи, що у мене є по Тарасові Хомі")
    assert interpreter.calls == []
    assert "немає відкритих задач" in result.text.casefold()
    assert "Не знайшов людину" not in result.text


async def test_done_person_query_lists_closed_tasks_not_as_open(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    title = "Домовитися з Боровцем про зустріч про підготовку до зими"
    task_id = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            people=("Андрій Боровець",),
            task_title=title,
        ),
        user=1,
        chat=1,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, task_id)
            assert row is not None
            row.status = "done"
            row.completed_at = datetime.now(KYIV)
    listed, interpreter = await _ask(repository, "Чи є виконані задачі по Боровцю?")
    assert interpreter.calls == []
    assert title in listed.text
    assert "відкрит" not in listed.text.casefold()
    opened, _ = await _ask(repository, "які відкриті задачі є по Боровцю?")
    assert title not in opened.text
    broader = await TaskQueryService(repository).search_people(
        1,
        TaskIntent(
            kind=TaskIntentKind.PEOPLE_TASKS_QUERY,
            person_query="Боровець",
            status_filter="done",
            broader_search=True,
        ),
    )
    assert title in broader.text
    assert "виконан" in broader.text.casefold()
    assert "відкрит" not in broader.text.casefold()


async def test_dual_link_shown_once(repository: SqlAlchemyTaskRepository) -> None:
    task_id = await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Надіслати матеріали партнерам",
            people=("Тарас Хома",),
        ),
        user=1,
        chat=1,
    )
    await repository.set_user_task_waiting(1, task_id, "Тарас Хома")
    result, interpreter = await _ask(repository, "Покажи, що у мене є по Тарасові Хомі")
    assert interpreter.calls == []
    assert result.text.count("Надіслати матеріали партнерам") == 1


async def test_infinitive_stays_task_intake(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(repository, complete_draft(project="BG", people=("Тарас Хома",)), user=1, chat=1)
    async with session_factory() as session:
        tasks_before = int(await session.scalar(select(func.count()).select_from(TaskRow)) or 0)
        people_before = int(await session.scalar(select(func.count()).select_from(PersonRow)) or 0)
    interpreter = ScriptedInterpreter([complete_draft(desired_outcome=None)])
    result, _ = await _ask(
        repository,
        "Поговорити з Тарасом Хомою",
        interpreter=interpreter,
    )
    assert result.kind in {IntakeKind.FOLLOW_UP, IntakeKind.CONFIRMATION, IntakeKind.ASK_PROJECT}
    assert interpreter.calls
    latin = ScriptedInterpreter([complete_draft(desired_outcome=None)])
    latin_result, _ = await _ask(
        repository,
        "Pоговорити з Тарасом Хомою",
        interpreter=latin,
    )
    assert latin.calls
    assert latin_result.kind != QueryKind.TASKS
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == tasks_before
        assert await session.scalar(select(func.count()).select_from(PersonRow)) == people_before


async def test_voice_uses_same_people_query_route(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed_taras(repository, user=8)
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(
        FakeTranscriber(["Покажи, що у мене є по Тарасові Хомі"]),
        intake,
        queries=TaskQueryService(repository),
    )
    result = await voice.handle_voice(8, 80, b"ogg")
    assert result.error is None
    assert result.intake is not None
    assert result.intake.kind == QueryKind.TASKS
    assert "👤 Тарас Хома" in result.intake.text
    assert "Погодити бюджет із командою" in result.intake.text
    assert interpreter.calls == []
    listed, _ = await _ask(
        repository, "Покажи, що у мене є по Тарасові Хомі", user=8, chat=80
    )
    assert listed.text == result.intake.text


WINTER = "Домовитися з Боровцем про зустріч про підготовку до зими"
SOLAR = "Обговорити з Боровцем сонячні панелі"
IVAN_OPEN = "Узгодити порядок денний з Іваном Боровцем"


def _hostile_router() -> ScriptedRouter:
    return ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.COMPLETE_TASK,
                task_query="виконані задачі по Боровцю",
            ),
            UtteranceInterpretation(
                kind=RouterKind.GENERAL_CHAT,
                chat_reply="Прийнято, дякую за оновлення!",
            ),
            UtteranceInterpretation(
                kind=RouterKind.GENERAL_CHAT,
                chat_reply="Всі задачі по Андрію Боровцю закриті. Відкритих задач немає.",
            ),
        ]
        * 4
    )


async def _mark_done(
    session_factory: async_sessionmaker[AsyncSession], task_id: object
) -> None:
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, task_id)
            assert row is not None
            row.status = "done"
            row.completed_at = datetime.now(KYIV)


async def _seed_two_borovets(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user: int = 1,
) -> None:
    winter_id = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            people=("Андрій Боровець",),
            task_title=WINTER,
        ),
        user=user,
        chat=user,
    )
    solar_id = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            people=("Андрій Боровець",),
            task_title=SOLAR,
        ),
        user=user,
        chat=user,
    )
    await persist(
        repository,
        complete_draft(
            project="BG",
            people=("Іван Боровець",),
            task_title=IVAN_OPEN,
        ),
        user=user,
        chat=user,
    )
    await _mark_done(session_factory, winter_id)
    await _mark_done(session_factory, solar_id)


def test_ambiguity_reply_picks_name_before_trailing_clause() -> None:
    pending = PendingAmbiguity(
        original_message="Чи є виконані задачі по Боровцю?",
        original_reference="Боровцю",
        candidates=("Андрій Боровець", "Іван Боровець"),
        intent_kind="people_tasks_query",
        person_query="Боровцю",
        status_filter="done",
    )
    action = parse_person_ambiguity_reply(
        "Андрій Боровець. Ти питався вже раніше", pending
    )
    assert action == {"action": "pick", "name": "Андрій Боровець"}


async def test_transcript_done_list_after_person_ambiguity(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_two_borovets(repository, session_factory)
    store = InMemoryConversationStore(_clock())
    router = _hostile_router()

    listed, _, store = await _turn(
        repository,
        "які відкриті задачі є по Боровцю?",
        router=router,
        context=store,
    )
    assert listed.is_person_ambiguity
    assert "Андрій Боровець" in listed.text
    assert "Іван Боровець" in listed.text

    picked, _, store = await _turn(
        repository,
        "Андрій Боровець. Ти питався вже раніше",
        router=router,
        context=store,
    )
    assert not getattr(picked, "is_person_ambiguity", False)
    assert WINTER not in picked.text
    assert SOLAR not in picked.text

    done, _, store = await _turn(
        repository,
        "Чи є виконані задачі по Боровцю?",
        router=router,
        context=store,
    )
    folded = done.text.casefold()
    assert WINTER in done.text
    assert SOLAR in done.text
    assert IVAN_OPEN not in done.text
    assert "виконан" in folded
    assert "відкрит" not in folded
    assert done.task_ids
    assert router.calls == []

    dispute, _, store = await _turn(
        repository,
        "я закрив ці задачі. Ти кажеш, що вони відкриті...",
        router=router,
        context=store,
    )
    dispute_folded = dispute.text.casefold()
    assert "перевірив" in dispute_folded
    assert "базі" in dispute_folded
    assert "виконан" in dispute_folded
    assert WINTER in dispute.text
    assert SOLAR in dispute.text
    assert "прийнято" not in dispute_folded
    assert router.calls == []


async def test_pick_after_done_query_keeps_done_filter(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_two_borovets(repository, session_factory)
    store = InMemoryConversationStore(_clock())
    router = _hostile_router()
    first, _, store = await _turn(
        repository,
        "Чи є виконані задачі по Боровцю?",
        router=router,
        context=store,
    )
    assert first.is_person_ambiguity
    picked, _, _ = await _turn(
        repository,
        "Андрій Боровець. Ти питався вже раніше",
        router=router,
        context=store,
    )
    folded = picked.text.casefold()
    assert WINTER in picked.text
    assert "виконан" in folded
    assert "відкрит" not in folded
    assert picked.task_ids
    assert router.calls == []


async def test_grounded_tools_keep_done_tasks_when_status_filter_is_done(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(
            project="Unity Center",
            people=("Андрій Боровець",),
            task_title=WINTER,
        ),
        user=1,
        chat=1,
    )
    await _mark_done(session_factory, task_id)
    queries = TaskQueryService(repository)
    dropped = await queries.tasks_by_ids(1, (task_id,), include_closed=False)
    assert dropped == []
    kept = await queries.tasks_by_ids(1, (task_id,), include_closed=True)
    assert [task.id for task in kept] == [task_id]

    result = await execute_grounded_tools(
        queries,
        1,
        UtteranceInterpretation(
            kind=RouterKind.PEOPLE_TASKS_QUERY,
            person_query="Андрій Боровець",
            status_filter="done",
        ),
        None,
        today=date(2026, 9, 17),
    )
    assert WINTER in result.text
    assert result.task_ids == (task_id,)
    folded = result.text.casefold()
    assert "виконан" in folded
    assert "відкрит" not in folded

