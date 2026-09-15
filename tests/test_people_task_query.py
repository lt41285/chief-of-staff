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
from chief_of_staff.models.task_command import TaskIntentKind
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.people_query import parse_people_tasks_query
from chief_of_staff.services.person_match import PersonHit, name_stems, resolve_person
from chief_of_staff.services.task_command_intent import parse_task_intent_deterministic
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_query import QueryKind, TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
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
