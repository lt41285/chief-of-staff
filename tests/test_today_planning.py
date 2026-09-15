from collections.abc import AsyncIterator
from datetime import date, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.task import TaskRow
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.plan import PlanConstraints
from chief_of_staff.models.task import Importance, Urgency
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.daily_planning import DailyPlanningService, PlanKind
from chief_of_staff.services.plan_format import ASK_FREE_TIME, ASK_REPLAN, OVERFLOW_HEADER, PLAN_CANCELLED
from chief_of_staff.services.planning_session import InMemoryPlanningSessionStore, PlanningPhase
from chief_of_staff.services.reminder_policy import at_kyiv
from chief_of_staff.services.task_intake import IntakeKind, TaskIntakeService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService
from tests.fakes import FakeTaskRepository
from tests.test_task_persistence import persist
from tests.test_task_validation import ScriptedInterpreter, complete_draft
from tests.test_voice_intake import FakeTranscriber


class FrozenClock(Clock):
    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


class ScriptedReplan:
    def __init__(self, constraints: list[PlanConstraints]) -> None:
        self._constraints = list(constraints)
        self.calls: list[str] = []

    async def interpret(self, text: str) -> PlanConstraints:
        self.calls.append(text)
        if not self._constraints:
            raise AssertionError(f"unexpected replan for {text!r}")
        return self._constraints.pop(0)


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


def _planning(
    repository: SqlAlchemyTaskRepository | FakeTaskRepository,
    now: datetime,
    *,
    store: InMemoryPlanningSessionStore | None = None,
    replan: ScriptedReplan | None = None,
) -> tuple[DailyPlanningService, InMemoryPlanningSessionStore]:
    store = store or InMemoryPlanningSessionStore()
    return DailyPlanningService(repository, store, FrozenClock(now), replan), store


async def _set_status(
    session_factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
    status: str,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(TaskRow, task_id)
            assert row is not None
            row.status = status


def test_today_starts_planning_flow() -> None:
    service, store = _planning(FakeTaskRepository(), at_kyiv(date(2026, 9, 2), 9, 0))
    result = service.start(1, 10)
    assert result.kind == PlanKind.ASK_TIME
    assert result.text == ASK_FREE_TIME
    session = store.get(1, 10)
    assert session is not None
    assert session.phase == PlanningPhase.AWAITING_TIME


async def test_voice_answer_uses_same_planning_flow(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(deadline=date(2026, 9, 2), estimated_minutes=20),
        user=8,
        chat=80,
    )
    planning, _store = _planning(repository, at_kyiv(date(2026, 9, 2), 9, 20))
    planning.start(8, 80)
    intake = TaskIntakeService(ScriptedInterpreter([]), InMemoryTaskSessionStore(), repository)
    voice = VoiceMessageService(FakeTranscriber(["три години"]), intake, planning)
    result = await voice.handle_voice(8, 80, b"ogg")
    assert result.error is None
    assert result.intake is not None
    assert result.intake.kind == PlanKind.PLAN
    assert "🗓 План на сьогодні" in result.intake.text
    assert "Agree the revised budget" in result.intake.text
    assert "High" not in result.intake.text
    assert "Urgent" not in result.intake.text


async def test_today_ranking_ignores_legacy_importance(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    low_id = await persist(
        repository,
        complete_draft(
            task_title="Low not urgent budget check with Taras",
            deadline=date(2026, 9, 2),
            importance=Importance.LOW,
            urgency=Urgency.NOT_URGENT,
        ),
        user=11,
        chat=11,
    )
    high_id = await persist(
        repository,
        complete_draft(
            task_title="High urgent budget war room with Taras",
            deadline=date(2026, 9, 2),
            importance=Importance.HIGH,
            urgency=Urgency.URGENT,
        ),
        user=11,
        chat=11,
    )
    async with session_factory() as session:
        async with session.begin():
            low = await session.get(TaskRow, low_id)
            high = await session.get(TaskRow, high_id)
            assert low and high
            low.created_at = at_kyiv(date(2026, 8, 1), 10, 0)
            high.created_at = at_kyiv(date(2026, 8, 20), 10, 0)
    service, _store = _planning(repository, at_kyiv(date(2026, 9, 2), 9, 20))
    service.start(11, 11)
    plan = await service.handle_user_text(11, 11, "3 години")
    low_at = plan.text.index("Low not urgent budget check with Taras")
    high_at = plan.text.index("High urgent budget war room with Taras")
    assert low_at < high_at
    assert "High ·" not in plan.text
    assert " · Urgent" not in plan.text


async def test_excludes_done_cancelled_and_waiting(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    open_id = await persist(
        repository,
        complete_draft(task_title="Open the weekly Unity status pack"),
        user=1,
        chat=1,
    )
    done_id = await persist(
        repository,
        complete_draft(task_title="Finish the signed budget pack today", people=()),
        user=1,
        chat=1,
    )
    cancelled_id = await persist(
        repository,
        complete_draft(task_title="Drop the unused vendor follow-up", people=()),
        user=1,
        chat=1,
    )
    waiting_id = await persist(
        repository,
        complete_draft(task_title="Wait on finance for the invoice copy", people=()),
        user=1,
        chat=1,
    )
    await _set_status(session_factory, done_id, "done")
    await _set_status(session_factory, cancelled_id, "cancelled")
    await _set_status(session_factory, waiting_id, "waiting")
    loaded = await repository.list_open_tasks_for_telegram_user(1)
    titles = {task.title for task in loaded}
    assert "Open the weekly Unity status pack" in titles
    assert "Finish the signed budget pack today" not in titles
    assert "Drop the unused vendor follow-up" not in titles
    assert "Wait on finance for the invoice copy" not in titles
    assert {task.id for task in loaded} == {open_id}


async def test_accept_sets_selected_tasks_to_today(
    repository: SqlAlchemyTaskRepository,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await persist(
        repository,
        complete_draft(deadline=date(2026, 9, 2), estimated_minutes=20),
        user=2,
        chat=2,
    )
    service, store = _planning(repository, at_kyiv(date(2026, 9, 2), 9, 20))
    service.start(2, 2)
    proposed = await service.handle_user_text(2, 2, "3 години")
    assert proposed.kind == PlanKind.PLAN
    session = store.get(2, 2)
    assert session is not None
    selected = session.selected_ids
    assert selected
    accepted = await service.accept(2, 2)
    assert accepted.kind == PlanKind.ACCEPTED
    assert store.get(2, 2) is None
    async with session_factory() as session:
        stored = await session.get(TaskRow, selected[0])
        assert stored is not None
        assert stored.status == "today"
        assert stored.completed_at is None


async def test_replan_respects_project_exclusion(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(
            deadline=date(2026, 9, 2),
            task_title="Agree the Unity Center budget with Taras",
            project="Unity Center",
            estimated_minutes=20,
        ),
        user=3,
        chat=3,
    )
    await persist(
        repository,
        complete_draft(
            deadline=date(2026, 9, 2),
            task_title="Write the Alpha weekly status note",
            project="Alpha",
            people=(),
            estimated_minutes=20,
        ),
        user=3,
        chat=3,
    )
    replan = ScriptedReplan([PlanConstraints(exclude_projects=("Unity Center",))])
    service, _store = _planning(
        repository, at_kyiv(date(2026, 9, 2), 9, 20), replan=replan
    )
    service.start(3, 3)
    first = await service.handle_user_text(3, 3, "4 год")
    assert "Agree the Unity Center budget with Taras" in first.text
    assert service.begin_replan(3, 3).text == ASK_REPLAN
    second = await service.handle_user_text(3, 3, "Не став нічого по Unity Center")
    assert replan.calls == ["Не став нічого по Unity Center"]
    assert "Agree the Unity Center budget with Taras" not in second.text
    assert "Write the Alpha weekly status note" in second.text


def test_cancel_clears_planning_session() -> None:
    service, store = _planning(FakeTaskRepository(), at_kyiv(date(2026, 9, 2), 9, 0))
    service.start(4, 4)
    result = service.cancel(4, 4)
    assert result.text == PLAN_CANCELLED
    assert store.get(4, 4) is None


async def test_planning_does_not_steal_normal_task_intake() -> None:
    repo = FakeTaskRepository()
    interpreter = ScriptedInterpreter([complete_draft(desired_outcome=None)])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repo)
    planning, _store = _planning(repo, at_kyiv(date(2026, 9, 2), 9, 0))
    planning.start(5, 5)
    proposed = await process_user_utterance(
        intake, 5, 5, "Поговорити з Тарасом про бюджет", planning=planning
    )
    assert proposed.kind == PlanKind.CLARIFY
    assert interpreter.calls == []
    planning.cancel(5, 5)
    intake_result = await process_user_utterance(
        intake, 5, 5, "Поговорити з Тарасом про бюджет", planning=planning
    )
    assert intake_result.kind == IntakeKind.FOLLOW_UP
    assert interpreter.calls


async def test_proposing_plan_does_not_block_new_task_message(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(repository, complete_draft(deadline=date(2026, 9, 2)), user=6, chat=6)
    interpreter = ScriptedInterpreter([complete_draft(desired_outcome=None)])
    intake = TaskIntakeService(interpreter, InMemoryTaskSessionStore(), repository)
    planning, store = _planning(repository, at_kyiv(date(2026, 9, 2), 9, 20))
    planning.start(6, 6)
    await planning.handle_user_text(6, 6, "3 години")
    assert store.get(6, 6) is not None
    assert store.get(6, 6).phase == PlanningPhase.PROPOSING
    result = await process_user_utterance(
        intake, 6, 6, "Нова задача по Alpha", planning=planning
    )
    assert result.kind == IntakeKind.FOLLOW_UP
    assert interpreter.calls
    assert store.get(6, 6) is not None


async def test_only_owner_tasks_are_planned(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(task_title="Review my own Unity budget pack", deadline=date(2026, 9, 2)),
        user=11,
        chat=110,
    )
    await persist(
        repository,
        complete_draft(task_title="Review someone else's Unity budget pack", deadline=date(2026, 9, 2), people=()),
        user=12,
        chat=120,
    )
    service, _store = _planning(repository, at_kyiv(date(2026, 9, 2), 9, 20))
    service.start(11, 110)
    plan = await service.handle_user_text(11, 110, "3 години")
    assert "Review my own Unity budget pack" in plan.text
    assert "Review someone else's Unity budget pack" not in plan.text


async def test_kyiv_calendar_date_is_used_for_due_today(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(task_title="Due today in Kyiv", deadline=date(2026, 9, 2)),
        user=7,
        chat=7,
    )
    early_kyiv = at_kyiv(date(2026, 9, 2), 0, 30)
    service, _store = _planning(repository, early_kyiv)
    service.start(7, 7)
    plan = await service.handle_user_text(7, 7, "2 години")
    assert "Дедлайн: сьогодні" in plan.text
    assert "прострочено" not in plan.text


async def test_oversized_critical_copy(
    repository: SqlAlchemyTaskRepository,
) -> None:
    await persist(
        repository,
        complete_draft(
            task_title="Rewrite the full budget pack",
            deadline=date(2026, 9, 2),
            estimated_minutes=240,
            importance=Importance.HIGH,
            urgency=Urgency.URGENT,
        ),
        user=9,
        chat=9,
    )
    service, _store = _planning(repository, at_kyiv(date(2026, 9, 2), 9, 20))
    service.start(9, 9)
    plan = await service.handle_user_text(9, 9, "1 година")
    assert OVERFLOW_HEADER in plan.text
    assert "4 год" in plan.text
    assert "High" not in plan.text
    assert "Urgent" not in plan.text
