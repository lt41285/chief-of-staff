""" /today use-case: ask for time, propose a plan, accept / replan / cancel. """

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from loguru import logger

from chief_of_staff.models.plan import PlanCandidate, PlanConstraints
from chief_of_staff.services.available_time import parse_available_time
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.daily_plan import build_daily_plan
from chief_of_staff.services.plan_format import (
    ASK_FREE_TIME,
    ASK_REPLAN,
    NO_ACTIVE_PLAN,
    NO_OPEN_TASKS,
    PLAN_ACCEPTED,
    PLAN_CANCELLED,
    format_plan_message,
)
from chief_of_staff.services.planning_session import (
    InMemoryPlanningSessionStore,
    PlanningPhase,
    PlanningSession,
)
from chief_of_staff.services.replan_interpreter import merge_constraints


class PlanningTaskSource(Protocol):
    async def list_open_tasks_for_telegram_user(
        self, telegram_user_id: int
    ) -> list[PlanCandidate]: ...

    async def mark_tasks_today(
        self, telegram_user_id: int, task_ids: tuple[UUID, ...]
    ) -> None: ...


class ReplanPreferenceReader(Protocol):
    async def interpret(self, text: str) -> PlanConstraints: ...


class PlanKind(StrEnum):
    ASK_TIME = "ask_time"
    ASK_REPLAN = "ask_replan"
    PLAN = "plan"
    CLARIFY = "clarify"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"
    EMPTY = "empty"
    INFO = "info"


@dataclass(frozen=True)
class PlanResult:
    kind: PlanKind
    text: str
    show_plan_buttons: bool = False
    task_ids: tuple[UUID, ...] = ()


class DailyPlanningService:
    def __init__(
        self,
        repository: PlanningTaskSource,
        store: InMemoryPlanningSessionStore,
        clock: Clock,
        replan: ReplanPreferenceReader | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._clock = clock
        self._replan = replan

    def is_awaiting_input(self, user_id: int, chat_id: int) -> bool:
        session = self._store.get(user_id, chat_id)
        if session is None:
            return False
        return session.phase in (PlanningPhase.AWAITING_TIME, PlanningPhase.AWAITING_REPLAN)

    def describe_pending(self, user_id: int, chat_id: int) -> dict | None:
        session = self._store.get(user_id, chat_id)
        if session is None or not self.is_awaiting_input(user_id, chat_id):
            return None
        awaiting = (
            "available_minutes"
            if session.phase == PlanningPhase.AWAITING_TIME
            else "replan_instruction"
        )
        return {
            "pending_action": "daily_plan",
            "awaiting": awaiting,
            "draft": {"available_minutes": session.available_minutes},
        }

    def start(self, user_id: int, chat_id: int) -> PlanResult:
        self._store.put(user_id, chat_id, PlanningSession(phase=PlanningPhase.AWAITING_TIME))
        return PlanResult(kind=PlanKind.ASK_TIME, text=ASK_FREE_TIME)

    async def handle_user_text(self, user_id: int, chat_id: int, text: str) -> PlanResult:
        session = self._store.get(user_id, chat_id)
        if session is None:
            return PlanResult(kind=PlanKind.INFO, text=NO_ACTIVE_PLAN)
        if session.phase == PlanningPhase.AWAITING_TIME:
            return await self._handle_time(user_id, chat_id, session, text)
        if session.phase == PlanningPhase.AWAITING_REPLAN:
            return await self._handle_replan(user_id, chat_id, session, text)
        return PlanResult(kind=PlanKind.INFO, text=NO_ACTIVE_PLAN)

    async def accept(self, user_id: int, chat_id: int) -> PlanResult:
        session = self._store.get(user_id, chat_id)
        if session is None or session.phase != PlanningPhase.PROPOSING:
            return PlanResult(kind=PlanKind.INFO, text=NO_ACTIVE_PLAN)
        if session.selected_ids:
            await self._repository.mark_tasks_today(user_id, session.selected_ids)
        self._store.clear(user_id, chat_id)
        return PlanResult(kind=PlanKind.ACCEPTED, text=PLAN_ACCEPTED)

    def begin_replan(self, user_id: int, chat_id: int) -> PlanResult:
        session = self._store.get(user_id, chat_id)
        if session is None or session.phase != PlanningPhase.PROPOSING:
            return PlanResult(kind=PlanKind.INFO, text=NO_ACTIVE_PLAN)
        session.phase = PlanningPhase.AWAITING_REPLAN
        self._store.put(user_id, chat_id, session)
        return PlanResult(kind=PlanKind.ASK_REPLAN, text=ASK_REPLAN)

    def cancel(self, user_id: int, chat_id: int) -> PlanResult:
        if self._store.get(user_id, chat_id) is None:
            return PlanResult(kind=PlanKind.INFO, text=NO_ACTIVE_PLAN)
        self._store.clear(user_id, chat_id)
        return PlanResult(kind=PlanKind.CANCELLED, text=PLAN_CANCELLED)

    async def _handle_time(
        self,
        user_id: int,
        chat_id: int,
        session: PlanningSession,
        text: str,
    ) -> PlanResult:
        parsed = parse_available_time(text)
        if parsed.minutes is None:
            return PlanResult(kind=PlanKind.CLARIFY, text=parsed.clarify or ASK_FREE_TIME)
        session.available_minutes = parsed.minutes
        return await self._rebuild(user_id, chat_id, session)

    async def plan_with_minutes(
        self, user_id: int, chat_id: int, minutes: int
    ) -> PlanResult:
        session = PlanningSession(
            phase=PlanningPhase.AWAITING_TIME,
            available_minutes=minutes,
        )
        return await self._rebuild(user_id, chat_id, session)

    async def _handle_replan(
        self,
        user_id: int,
        chat_id: int,
        session: PlanningSession,
        text: str,
    ) -> PlanResult:
        if self._replan is None:
            return PlanResult(
                kind=PlanKind.CLARIFY,
                text="Не можу зараз інтерпретувати зміну плану. Спробуй ще раз.",
            )
        try:
            incoming = await self._replan.interpret(text)
        except Exception:
            logger.exception("Replan interpretation failed")
            return PlanResult(
                kind=PlanKind.CLARIFY,
                text="Не зрозумів, що змінити. Напиши коротше, наприклад: не став нічого по Unity Center.",
            )
        session.constraints = merge_constraints(session.constraints, incoming)
        return await self._rebuild(user_id, chat_id, session)

    async def _rebuild(
        self,
        user_id: int,
        chat_id: int,
        session: PlanningSession,
    ) -> PlanResult:
        available = session.available_minutes
        if available is None:
            session.phase = PlanningPhase.AWAITING_TIME
            self._store.put(user_id, chat_id, session)
            return PlanResult(kind=PlanKind.ASK_TIME, text=ASK_FREE_TIME)
        tasks = await self._repository.list_open_tasks_for_telegram_user(user_id)
        if not tasks:
            session.phase = PlanningPhase.PROPOSING
            session.selected_ids = ()
            session.overflow_ids = ()
            self._store.put(user_id, chat_id, session)
            return PlanResult(kind=PlanKind.EMPTY, text=NO_OPEN_TASKS, show_plan_buttons=True)
        today = self._today()
        plan = build_daily_plan(tasks, available, today, session.constraints)
        session.phase = PlanningPhase.PROPOSING
        session.selected_ids = tuple(task.id for task in plan.selected)
        session.overflow_ids = tuple(task.id for task in plan.overflow)
        self._store.put(user_id, chat_id, session)
        return PlanResult(
            kind=PlanKind.PLAN,
            text=format_plan_message(plan, today),
            show_plan_buttons=True,
            task_ids=session.selected_ids,
        )

    def _today(self) -> date:
        return self._clock.now().date()
