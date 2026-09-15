from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task import Importance, Urgency
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_query import QueryKind, TaskQueryService
from chief_of_staff.services.task_query_format import (
    format_all_tasks_grouped,
    format_project_task_list,
    format_task_list_block,
)
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft


TODAY = date(2026, 9, 2)


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


def _task(**overrides: Any) -> PlanCandidate:
    base = PlanCandidate(
        id=uuid4(),
        title="Скласти 10 способів казати людям «ні»",
        project="Особисте",
        people=(),
        deadline=date(2026, 9, 15),
        importance=Importance.HIGH,
        urgency=Urgency.NOT_URGENT,
        estimated_minutes=60,
        status="inbox",
        desired_outcome="Є список з 10 формулювань",
        actual_minutes=None,
    )
    return replace(base, **overrides)


def test_lists_do_not_show_importance_or_urgency() -> None:
    block = "\n".join(format_task_list_block(1, _task(), today=TODAY))
    assert "High" not in block
    assert "Urgent" not in block
    assert "Low" not in block
    assert "Medium" not in block
    assert "importance" not in block.casefold()
    assert "urgency" not in block.casefold()


def test_people_shown_or_dash() -> None:
    none = "\n".join(format_task_list_block(1, _task(), today=TODAY))
    assert "👤 Люди: —" in none
    some = "\n".join(
        format_task_list_block(
            1,
            _task(people=("Наталя Тарновська", "Андрій Боровець")),
            today=TODAY,
        )
    )
    assert "👤 Люди: Наталя Тарновська, Андрій Боровець" in some


def test_desired_outcome_shown_or_dash() -> None:
    filled = "\n".join(format_task_list_block(1, _task(), today=TODAY))
    assert "🎯 Результат: Є список з 10 формулювань" in filled
    missing = "\n".join(format_task_list_block(1, _task(desired_outcome="  "), today=TODAY))
    assert "🎯 Результат: —" in missing


def test_waiting_for_line_only_when_waiting() -> None:
    waiting = "\n".join(
        format_task_list_block(
            1,
            _task(status="waiting", waiting_for="Наталя Тарновська"),
            today=TODAY,
        )
    )
    assert "📌 Статус: waiting" in waiting
    assert "⏳ Чекаю від: Наталя Тарновська" in waiting
    inbox = "\n".join(format_task_list_block(1, _task(status="inbox"), today=TODAY))
    assert "⏳ Чекаю від:" not in inbox
    waiting = "\n".join(format_task_list_block(1, _task(status="waiting"), today=TODAY))
    assert "📌 Статус: waiting" in waiting
    assert "⏱ Оцінка: 1 год" in waiting
    assert "Факт:" not in waiting
    with_actual = "\n".join(
        format_task_list_block(1, _task(estimated_minutes=20, actual_minutes=35), today=TODAY)
    )
    assert "⏱ Оцінка: 20 хв · Факт: 35 хв" in with_actual


def test_deadline_signals() -> None:
    overdue = "\n".join(format_task_list_block(1, _task(deadline=date(2026, 8, 30)), today=TODAY))
    assert "⚠️ Прострочено" in overdue
    due_today = "\n".join(format_task_list_block(1, _task(deadline=TODAY), today=TODAY))
    assert "📅 Сьогодні" in due_today
    later = "\n".join(format_task_list_block(1, _task(deadline=date(2026, 9, 15)), today=TODAY))
    assert "📅 15 вересня" in later


def test_project_and_grouped_lists_use_same_block() -> None:
    task = _task(people=("Наталя Тарновська",), actual_minutes=35, estimated_minutes=20)
    project = format_project_task_list("Особисте", [task], today=TODAY)
    grouped = format_all_tasks_grouped([("Особисте", [task])], total=1, today=TODAY)
    block = "\n".join(format_task_list_block(1, task, today=TODAY))
    assert block in project
    assert block in grouped
    assert "👤 Люди: Наталя Тарновська" in project
    assert "📌 Статус: inbox" in grouped


async def test_list_output_includes_details_and_stays_user_scoped(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(
            project="Особисте",
            task_title="Скласти 10 способів казати людям ні сьогодні",
            people=("Наталя Тарновська",),
            desired_outcome="Є список з 10 формулювань",
            estimated_minutes=60,
        ),
        user=1,
        chat=1,
    )
    other = await persist(
        repository,
        complete_draft(
            project="Особисте",
            task_title="Review someone else's private refusal list today",
            people=("Andriy",),
            desired_outcome="Other user outcome must not leak",
        ),
        user=2,
        chat=2,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, other)
            assert row is not None
            row.actual_minutes = 99
    interpreter = ScriptedInterpreter([])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    queries = TaskQueryService(repository)
    project = await process_user_utterance(
        intake, 1, 1, "Які задачі в Особисте?", queries=queries
    )
    grouped = await process_user_utterance(
        intake, 1, 1, "Покажи всі задачі", queries=queries
    )
    assert project.kind == QueryKind.TASKS
    assert grouped.kind == QueryKind.TASKS
    for text in (project.text, grouped.text):
        assert "👤 Люди: Наталя Тарновська" in text
        assert "🎯 Результат: Є список з 10 формулювань" in text
        assert "📌 Статус: inbox" in text
        assert "⏱ Оцінка: 1 год" in text
        assert "someone else's" not in text
        assert "Other user outcome" not in text
        assert "Факт: 99" not in text
    assert interpreter.calls == []


async def test_missing_outcome_and_actual_time_from_repository(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id = await persist(
        repository,
        complete_draft(
            project="BG",
            task_title="Send the BG weekly status pack today",
            people=(),
            desired_outcome="ok",
        ),
        user=3,
        chat=3,
    )
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, task_id)
            assert row is not None
            row.desired_outcome = ""
            row.actual_minutes = 35
            row.status = "waiting"
    interpreter = ScriptedInterpreter([])
    result = await process_user_utterance(
        TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository),
        3,
        3,
        "Покажи всі таски по проєкту BG",
        queries=TaskQueryService(repository),
    )
    assert "👤 Люди: —" in result.text
    assert "🎯 Результат: —" in result.text
    assert "📌 Статус: waiting" in result.text
    assert "⏳ Чекаю від: —" in result.text
    assert "⏱ Оцінка: 20 хв · Факт: 35 хв" in result.text
    assert interpreter.calls == []
