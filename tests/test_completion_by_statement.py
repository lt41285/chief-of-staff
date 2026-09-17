from collections.abc import AsyncIterator
from datetime import date, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.completion_statement import (
    CompletionPhrasing,
    classify_completion_phrasing,
    extract_amounts,
)
from chief_of_staff.services.lifecycle_format import NO_MATCHED_COMPLETION
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.reminder_policy import at_kyiv
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_lifecycle import LifecycleKind, TaskLifecycleService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.test_reminders import FrozenClock
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber

LESIA_TITLE = (
    "Домовитися з Лесею Добош і передати їй кошти 500 тисяч гривень від BG"
)
LESIA_DONE = "Я домовився з Лесею Добош про передачу 500 тис грн"


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


def _clock(moment: datetime | None = None) -> Clock:
    return FrozenClock(moment or at_kyiv(date(2026, 9, 2), 12, 0))


def _life(repository: SqlAlchemyTaskRepository) -> TaskLifecycleService:
    return TaskLifecycleService(repository, InMemoryLifecycleStore(), _clock())


def _intake(
    repository: SqlAlchemyTaskRepository, drafts: list | None = None
) -> tuple[TaskIntakeService, ScriptedInterpreter]:
    interpreter = ScriptedInterpreter(drafts or [complete_draft()])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    return intake, interpreter


async def _lesia(repository: SqlAlchemyTaskRepository) -> UUID:
    return await persist(
        repository,
        complete_draft(
            task_title=LESIA_TITLE,
            people=("Леся Добош",),
            project="BG",
            desired_outcome="Кошти 500 тисяч гривень передані Лесі Добош",
        ),
        user=1,
        chat=1,
    )


def test_numbers_normalize_to_the_same_amount() -> None:
    expected = frozenset({500_000})
    assert extract_amounts("500000") == expected
    assert extract_amounts("500 000") == expected
    assert extract_amounts("500 тис") == expected
    assert extract_amounts("500 тисяч") == expected
    assert extract_amounts("500 тис грн") == expected
    assert extract_amounts("500 тисяч гривень") == expected


def test_phrasing_gates() -> None:
    assert classify_completion_phrasing(LESIA_DONE) == CompletionPhrasing.COMPLETED
    assert classify_completion_phrasing("Я передав Лесі 500 тисяч") == (
        CompletionPhrasing.COMPLETED
    )
    assert classify_completion_phrasing("З Лесею домовився") == CompletionPhrasing.COMPLETED
    assert classify_completion_phrasing("Все погодили з Лесею") == (
        CompletionPhrasing.COMPLETED
    )
    assert classify_completion_phrasing("Я вже це зробив") == CompletionPhrasing.COMPLETED
    assert classify_completion_phrasing("Треба домовитися з Лесею Добош") == (
        CompletionPhrasing.PROSPECTIVE
    )
    assert classify_completion_phrasing("Домовитися з Лесею Добош") == (
        CompletionPhrasing.PROSPECTIVE
    )
    assert classify_completion_phrasing("Потрібно передати 500 тис грн") == (
        CompletionPhrasing.PROSPECTIVE
    )
    assert classify_completion_phrasing("Нагадайте поговорити з Лесею") == (
        CompletionPhrasing.PROSPECTIVE
    )


async def test_lesia_statement_asks_completion_not_intake(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _lesia(repository)
    intake, interpreter = _intake(repository, drafts=[])
    result = await process_user_utterance(
        intake, 1, 1, LESIA_DONE, lifecycle=_life(repository)
    )
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert result.show_statement_buttons
    assert LESIA_TITLE in result.text
    assert "Позначити виконаною?" in result.text
    assert interpreter.calls == []


async def test_transferred_money_statement_matches_same_task(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _lesia(repository)
    intake, interpreter = _intake(repository, drafts=[])
    result = await process_user_utterance(
        intake, 1, 1, "Я передав Лесі 500 тисяч", lifecycle=_life(repository)
    )
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert LESIA_TITLE in result.text
    assert interpreter.calls == []


async def test_short_lesia_statement_matches_when_unambiguous(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _lesia(repository)
    intake, interpreter = _intake(repository, drafts=[])
    result = await process_user_utterance(
        intake, 1, 1, "З Лесею домовився", lifecycle=_life(repository)
    )
    assert result.kind == LifecycleKind.ASK_CONFIRM
    assert LESIA_TITLE in result.text
    assert interpreter.calls == []


async def test_need_to_agree_is_new_task_intake(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _lesia(repository)
    intake, interpreter = _intake(repository)
    result = await process_user_utterance(
        intake,
        1,
        1,
        "Треба домовитися з Лесею Добош",
        lifecycle=_life(repository),
    )
    assert result.kind != LifecycleKind.ASK_CONFIRM
    assert interpreter.calls
    assert interpreter.calls[0]["user_text"] == "Треба домовитися з Лесею Добош"


async def test_infinitive_title_is_new_task_intake(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _lesia(repository)
    intake, interpreter = _intake(repository)
    result = await process_user_utterance(
        intake, 1, 1, "Домовитися з Лесею Добош", lifecycle=_life(repository)
    )
    assert result.kind in {IntakeKind.CONFIRMATION, IntakeKind.FOLLOW_UP, IntakeKind.ASK_PROJECT}
    assert interpreter.calls
    assert interpreter.calls[0]["user_text"] == "Домовитися з Лесею Добош"


async def test_other_meeting_does_not_force_match(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _lesia(repository)
    intake, interpreter = _intake(repository)
    result = await process_user_utterance(
        intake,
        1,
        1,
        "Я домовився з Лесею про іншу зустріч",
        lifecycle=_life(repository),
    )
    assert result.kind == LifecycleKind.ASK_WHICH
    assert NO_MATCHED_COMPLETION in result.text
    assert interpreter.calls == []


async def test_two_similar_lesia_tasks_ask_which(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(
            task_title="Домовитися з Лесею про бюджет",
            people=("Леся Добош",),
            project="BG",
        ),
        user=1,
        chat=1,
    )
    await persist(
        repository,
        complete_draft(
            task_title="Домовитися з Лесею про візу",
            people=("Леся Добош",),
            project="Alpha",
        ),
        user=1,
        chat=1,
    )
    intake, interpreter = _intake(repository, drafts=[])
    result = await process_user_utterance(
        intake, 1, 1, "З Лесею домовився", lifecycle=_life(repository)
    )
    assert result.kind == LifecycleKind.ASK_WHICH
    assert result.show_choice_buttons
    assert result.choice_count == 2
    assert "бюджет" in result.text
    assert "візу" in result.text
    assert interpreter.calls == []


async def test_new_task_override_keeps_original_utterance(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _lesia(repository)
    intake, interpreter = _intake(repository)
    life = _life(repository)
    first = await process_user_utterance(intake, 1, 1, LESIA_DONE, lifecycle=life)
    assert first.kind == LifecycleKind.ASK_CONFIRM
    second = await process_user_utterance(
        intake, 1, 1, "Це нова задача", lifecycle=life
    )
    assert second.kind != LifecycleKind.ASK_CONFIRM
    assert interpreter.calls
    assert interpreter.calls[0]["user_text"] == LESIA_DONE


async def test_confirm_yes_marks_existing_done_without_new_row(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _lesia(repository)
    intake, interpreter = _intake(repository, drafts=[])
    life = _life(repository)
    asked = await process_user_utterance(intake, 1, 1, LESIA_DONE, lifecycle=life)
    assert asked.kind == LifecycleKind.ASK_CONFIRM
    done = await process_user_utterance(intake, 1, 1, "так", lifecycle=life)
    assert done.kind == LifecycleKind.ASK_ACTUAL
    assert interpreter.calls == []
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 1
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "done"
        assert row.completed_at is not None


async def test_confirm_cancel_leaves_task_open(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await _lesia(repository)
    intake, interpreter = _intake(repository, drafts=[])
    life = _life(repository)
    await process_user_utterance(intake, 1, 1, LESIA_DONE, lifecycle=life)
    cancelled = await process_user_utterance(
        intake, 1, 1, "скасувати", lifecycle=life
    )
    assert cancelled.kind == LifecycleKind.CANCELLED
    assert interpreter.calls == []
    async with session_factory() as session:
        row = await session.get(TaskRow, task_id)
        assert row is not None
        assert row.status == "inbox"
        assert row.completed_at is None


async def test_voice_uses_the_same_completion_statement_flow(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await _lesia(repository)
    intake, interpreter = _intake(repository, drafts=[])
    life = _life(repository)
    voice = VoiceMessageService(
        FakeTranscriber([LESIA_DONE]),
        intake,
        lifecycle=life,
    )
    result = await voice.handle_voice(1, 1, b"ogg")
    assert result.error is None
    assert result.intake is not None
    assert result.intake.kind == LifecycleKind.ASK_CONFIRM
    assert LESIA_TITLE in result.intake.text
    assert interpreter.calls == []
