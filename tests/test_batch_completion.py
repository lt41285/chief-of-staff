"""One message may report several finished tasks. Production transcript included."""

from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.task_command import TaskIntentKind
from chief_of_staff.models.utterance_intent import RouterKind, UtteranceInterpretation
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.completion_statement import (
    CompletionPhrasing,
    classify_completion_phrasing,
)
from chief_of_staff.services.conversation_context import InMemoryConversationStore
from chief_of_staff.services.lifecycle_format import (
    ASK_ACTUAL,
    ASK_LIST_COMPLETED,
    ASK_WHICH_TIME,
    NEED_TASK_HINT,
    NO_MATCHED_COMPLETION,
    SKIPPED_ACTUAL_ALL,
)
from chief_of_staff.services.lifecycle_session import (
    InMemoryLifecycleStore,
    LifecyclePhase,
)
from chief_of_staff.services.task_command_intent import parse_task_intent_deterministic
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleKind, TaskLifecycleService
from chief_of_staff.services.task_query import TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from tests.test_ai_router import ScriptedRouter
from tests.test_reminders import FrozenClock
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft

USER = 1
CHAT = 1

WINTER = "Домовитися з Боровцем про зустріч про підготовку до зими"
STORAGE = "Обговорити з Андрієм Боровцем про сховище на Свєнціцького"
PANELS = "Обговорити з Боровцем сонячні панелі"
SHUTTLE = "Поговорити з Андрієм Боровцем про курсування бусика"

TRANSCRIPT = (
    "Ці завдання виконав:\n"
    "— Домовитися з Боровцем про зустріч про підготовку до зими, дедлайн 9 вересня\n"
    "— Обговорити з Андрієм Боровцем про сховище на Свєнціцького, дедлайн 6 вересня\n"
    "— Обговорити з Боровцем сонячні панелі, дедлайн 20 вересня\n"
    "— Поговорити з Андрієм Боровцем про курсування бусика, дедлайн 8 вересня"
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
    return FrozenClock(datetime(2026, 9, 17, 12, 0, tzinfo=KYIV))


def _life(repository: SqlAlchemyTaskRepository) -> TaskLifecycleService:
    return TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())


async def _seed(
    repository: SqlAlchemyTaskRepository,
    *titles: str,
    people: tuple[str, ...] = ("Андрій Боровець",),
) -> None:
    for title in titles:
        await persist(
            repository,
            complete_draft(
                task_title=title,
                people=people,
                project="Unity Center",
                estimated_minutes=20,
                deadline=date(2026, 9, 9),
            ),
            user=USER,
            chat=CHAT,
        )


async def _statuses(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, tuple[str, int | None]]:
    async with session_factory() as session:
        rows = (await session.execute(select(TaskRow))).scalars().all()
        return {row.title: (row.status, row.actual_minutes) for row in rows}


async def test_exact_production_transcript_offers_one_confirmation(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository, WINTER, STORAGE, PANELS, SHUTTLE)
    life = _life(repository)

    result = await life.handle_user_text(USER, CHAT, TRANSCRIPT)

    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert result.show_confirm_buttons
    for title in (WINTER, STORAGE, PANELS, SHUTTLE):
        assert title in result.text
    assert "Не зміг зіставити" not in result.text


async def test_exact_production_transcript_completes_all_four(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(repository, WINTER, STORAGE, PANELS, SHUTTLE)
    life = _life(repository)
    await life.handle_user_text(USER, CHAT, TRANSCRIPT)

    confirmed = await life.confirm(USER, CHAT)

    statuses = await _statuses(session_factory)
    assert [status for status, _minutes in statuses.values()] == ["done"] * 4
    assert confirmed.kind == LifecycleKind.ASK_ACTUAL
    assert "4 задачі" in confirmed.text
    assert WINTER in confirmed.text


async def test_clarification_is_never_repeated_verbatim(
    repository: SqlAlchemyTaskRepository,
) -> None:
    """The production loop: three identical «Уточни, яку саме задачу» replies."""
    await _seed(repository, WINTER, STORAGE, PANELS, SHUTTLE)
    life = _life(repository)

    first = await life.handle_user_text(USER, CHAT, "я виконав 4 задачі")
    second = await life.handle_user_text(USER, CHAT, "всі 4")
    third = await life.handle_user_text(USER, CHAT, TRANSCRIPT)

    assert first.text == ASK_LIST_COMPLETED
    assert second.text != first.text
    assert third.text != second.text
    assert third.kind == LifecycleKind.ASK_CONFIRM


async def test_count_only_statement_keeps_pending_state(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository, WINTER, STORAGE)
    store = InMemoryLifecycleStore()
    life = TaskLifecycleService(repository, store, _clock())

    await life.handle_user_text(USER, CHAT, "я виконав 4 задачі")

    pending = store.get(USER, CHAT)
    assert pending is not None
    assert pending.phase == LifecyclePhase.AWAITING_TASK_REFERENCE
    assert pending.original_text == "я виконав 4 задачі"


async def test_all_reference_after_choice_list_completes_every_candidate(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(repository, WINTER, STORAGE, PANELS)
    life = _life(repository)

    await life.handle_user_text(USER, CHAT, "я виконав 3 задачі")
    listed = await life.handle_user_text(USER, CHAT, "всі")
    preview = await life.handle_user_text(USER, CHAT, "всі")
    await life.confirm(USER, CHAT)

    assert listed.kind == LifecycleKind.ASK_WHICH
    assert listed.choice_count == 3
    assert preview.kind == LifecycleKind.ASK_CONFIRM
    statuses = await _statuses(session_factory)
    assert [status for status, _minutes in statuses.values()] == ["done"] * 3


async def test_unmatched_item_is_reported_not_silently_dropped(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(repository, WINTER, STORAGE, PANELS)
    life = _life(repository)
    message = (
        "Ці завдання виконав:\n"
        f"— {WINTER}\n"
        f"— {STORAGE}\n"
        "— Купити квитки до Рейкʼявіка"
    )

    result = await life.handle_user_text(USER, CHAT, message)
    await life.confirm(USER, CHAT)

    assert "Не зміг зіставити" in result.text
    assert "квитки" in result.text
    statuses = await _statuses(session_factory)
    assert statuses[WINTER][0] == "done"
    assert statuses[STORAGE][0] == "done"
    assert statuses[PANELS][0] != "done"


async def test_one_task_is_never_claimed_by_two_items(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository, WINTER, STORAGE)
    life = _life(repository)
    message = f"Виконав:\n— {WINTER}\n— {STORAGE}\n— {STORAGE}"

    result = await life.handle_user_text(USER, CHAT, message)

    assert result.text.count(f"✅ {STORAGE}") == 1
    assert "Не зміг зіставити" in result.text


async def test_actual_time_is_asked_for_every_task_in_turn(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(repository, WINTER, STORAGE, PANELS, SHUTTLE)
    life = _life(repository)
    await life.handle_user_text(USER, CHAT, TRANSCRIPT)
    first = await life.confirm(USER, CHAT)

    second = await life.handle_user_text(USER, CHAT, "30")
    third = await life.handle_user_text(USER, CHAT, "пів години")
    fourth = await life.handle_user_text(USER, CHAT, "пропустити")
    done = await life.handle_user_text(USER, CHAT, "1 год 20 хв")

    assert WINTER in first.text
    assert STORAGE in second.text
    assert PANELS in third.text
    assert SHUTTLE in fourth.text
    assert done.kind == LifecycleKind.DONE
    statuses = await _statuses(session_factory)
    assert statuses[WINTER] == ("done", 30)
    assert statuses[STORAGE] == ("done", 30)
    assert statuses[PANELS] == ("done", None)
    assert statuses[SHUTTLE] == ("done", 80)


async def test_skip_all_stops_the_whole_chain(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(repository, WINTER, STORAGE, PANELS)
    store = InMemoryLifecycleStore()
    life = TaskLifecycleService(repository, store, _clock())
    await life.handle_user_text(USER, CHAT, f"Виконав:\n— {WINTER}\n— {STORAGE}\n— {PANELS}")
    await life.confirm(USER, CHAT)

    stopped = await life.handle_user_text(USER, CHAT, "далі не питай")

    assert stopped.text == SKIPPED_ACTUAL_ALL
    assert store.get(USER, CHAT) is None
    statuses = await _statuses(session_factory)
    assert [status for status, _minutes in statuses.values()] == ["done"] * 3


async def test_unparsable_duration_reasks_once_then_moves_on(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository, WINTER, STORAGE)
    life = _life(repository)
    await life.handle_user_text(USER, CHAT, f"Виконав:\n— {WINTER}\n— {STORAGE}")
    await life.confirm(USER, CHAT)

    again = await life.handle_user_text(USER, CHAT, "ага добре")
    moved = await life.handle_user_text(USER, CHAT, "ну таке")

    assert again.text == ASK_WHICH_TIME
    assert STORAGE in moved.text


async def test_batch_duration_reply_never_touches_another_task(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: a bare number must stay on the task being asked about."""
    await _seed(repository, WINTER, STORAGE)
    await _seed(repository, "Виплатити премії лідерів", people=("Ліда Верещинська",))
    life = _life(repository)
    await life.handle_user_text(USER, CHAT, f"Виконав:\n— {WINTER}\n— {STORAGE}")
    await life.confirm(USER, CHAT)

    await life.handle_user_text(USER, CHAT, "10")

    statuses = await _statuses(session_factory)
    assert statuses[WINTER] == ("done", 10)
    assert statuses["Виплатити премії лідерів"] == ("inbox", None)


async def test_single_task_completion_keeps_the_old_flow(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository, WINTER, STORAGE)
    life = _life(repository)

    preview = await life.consider_completed_statement(
        USER, CHAT, "я домовився з Боровцем про зустріч про підготовку до зими"
    )
    assert preview is not None
    asked = await life.confirm(USER, CHAT)

    assert preview.kind == LifecycleKind.ASK_CONFIRM
    assert "Схоже, ти виконав цю задачу" in preview.text
    assert ASK_ACTUAL in asked.text


async def test_two_line_message_does_not_hijack_a_plain_sentence(
    repository: SqlAlchemyTaskRepository,
) -> None:
    """A single task named across two lines must not become a batch."""
    await _seed(repository, WINTER, STORAGE)
    life = _life(repository)

    result = await life.handle_user_text(
        USER, CHAT, f"я виконав\n{WINTER}"
    )

    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert STORAGE not in result.text


def test_vykonav_is_recognised_without_an_llm_call() -> None:
    for text in ("я виконав цю задачу", "виконала лист до IFC", "ми виконали бюджет"):
        assert classify_completion_phrasing(text) == CompletionPhrasing.COMPLETED, text
    parsed = parse_task_intent_deterministic("я виконав лист до IFC")
    assert parsed is not None
    assert parsed.kind == TaskIntentKind.COMPLETE_TASK


TAKOZH_DONE = "Також домовився з Боровцем про зустріч до підготовки до зима"


def test_takozh_winter_statement_is_completed_phrasing() -> None:
    assert classify_completion_phrasing(TAKOZH_DONE) == CompletionPhrasing.COMPLETED


async def test_takozh_winter_statement_matches_open_task(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository, WINTER, STORAGE, PANELS, SHUTTLE)
    life = _life(repository)
    result = await life.consider_completed_statement(USER, CHAT, TAKOZH_DONE)
    assert result is not None
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert WINTER in result.text


async def test_past_tense_is_not_silent_general_chat(
    repository: SqlAlchemyTaskRepository,
) -> None:
    """A completed-action report must not become «Прийнято, дякую за оновлення!»."""
    await _seed(repository, WINTER)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.GENERAL_CHAT,
                chat_reply="Прийнято, дякую за оновлення!",
            )
        ]
    )
    reply = await process_user_utterance(
        TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository),
        USER,
        CHAT,
        TAKOZH_DONE,
        lifecycle=_life(repository),
        queries=TaskQueryService(repository),
        router=router,
        context=InMemoryConversationStore(_clock()),
    )
    assert "Прийнято" not in reply.text
    assert "дякую за оновлення" not in reply.text.casefold()
    assert reply.kind == LifecycleKind.ASK_CONFIRM
    assert WINTER in reply.text
    assert router.calls == []


async def test_unmatched_completion_says_could_not_match(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _seed(repository, STORAGE)
    router = ScriptedRouter(
        [
            UtteranceInterpretation(
                kind=RouterKind.GENERAL_CHAT,
                chat_reply="Прийнято, дякую за оновлення!",
            )
        ]
    )
    reply = await process_user_utterance(
        TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository),
        USER,
        CHAT,
        TAKOZH_DONE,
        lifecycle=_life(repository),
        queries=TaskQueryService(repository),
        router=router,
        context=InMemoryConversationStore(_clock()),
    )
    assert reply.kind == LifecycleKind.ASK_WHICH
    assert NO_MATCHED_COMPLETION in reply.text
    assert "Прийнято" not in reply.text
    assert router.calls == []


async def test_impersonal_list_is_still_a_batch(
    repository: SqlAlchemyTaskRepository,
) -> None:
    """«виконано» is an explicit command, but a list under it is still a batch."""
    await _seed(repository, WINTER, STORAGE)
    life = _life(repository)

    result = await life.handle_user_text(USER, CHAT, f"Виконано:\n— {WINTER}\n— {STORAGE}")

    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert WINTER in result.text
    assert STORAGE in result.text


async def test_production_transcript_through_the_ai_router(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(repository, WINTER, STORAGE, PANELS, SHUTTLE)
    life = _life(repository)
    router = ScriptedRouter(
        [UtteranceInterpretation(kind=RouterKind.COMPLETE_STATEMENT, task_query=None)]
    )
    reply = await process_user_utterance(
        TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository),
        USER,
        CHAT,
        TRANSCRIPT,
        lifecycle=life,
        queries=TaskQueryService(repository),
        router=router,
        context=InMemoryConversationStore(_clock()),
    )
    await life.confirm(USER, CHAT)

    assert NEED_TASK_HINT not in reply.text
    assert WINTER in reply.text
    statuses = await _statuses(session_factory)
    assert [status for status, _minutes in statuses.values()] == ["done"] * 4
