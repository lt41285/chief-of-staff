"""In-memory /today session per Telegram user+chat."""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from chief_of_staff.models.plan import PlanConstraints


class PlanningPhase(StrEnum):
    AWAITING_TIME = "awaiting_time"
    PROPOSING = "proposing"
    AWAITING_REPLAN = "awaiting_replan"


@dataclass
class PlanningSession:
    phase: PlanningPhase
    available_minutes: int | None = None
    selected_ids: tuple[UUID, ...] = ()
    overflow_ids: tuple[UUID, ...] = ()
    constraints: PlanConstraints = field(default_factory=PlanConstraints)


class InMemoryPlanningSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[tuple[int, int], PlanningSession] = {}

    @staticmethod
    def _key(user_id: int, chat_id: int) -> tuple[int, int]:
        return (chat_id, user_id)

    def get(self, user_id: int, chat_id: int) -> PlanningSession | None:
        return self._sessions.get(self._key(user_id, chat_id))

    def put(self, user_id: int, chat_id: int, session: PlanningSession) -> None:
        self._sessions[self._key(user_id, chat_id)] = session

    def clear(self, user_id: int, chat_id: int) -> None:
        self._sessions.pop(self._key(user_id, chat_id), None)
