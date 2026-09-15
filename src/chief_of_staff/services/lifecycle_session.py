"""In-memory completion flow. Separate from intake, projects, and /today."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID


class LifecyclePhase(StrEnum):
    CHOOSING = "choosing"
    CONFIRMING = "confirming"
    AWAITING_ACTUAL = "awaiting_actual"
    AWAITING_DEADLINE = "awaiting_deadline"


class LifecycleAction(StrEnum):
    COMPLETE = "complete"
    POSTPONE = "postpone"
    WAITING = "waiting"
    RESUME = "resume"


@dataclass
class PendingLifecycle:
    phase: LifecyclePhase
    action: LifecycleAction = LifecycleAction.COMPLETE
    task_id: UUID | None = None
    candidate_ids: tuple[UUID, ...] = ()
    actual_minutes: int | None = None
    new_deadline: date | None = None
    waiting_for: str | None = None
    original_text: str | None = None
    offer_new_task: bool = False


class InMemoryLifecycleStore:
    def __init__(self) -> None:
        self._pending: dict[tuple[int, int], PendingLifecycle] = {}

    @staticmethod
    def _key(user_id: int, chat_id: int) -> tuple[int, int]:
        return (chat_id, user_id)

    def get(self, user_id: int, chat_id: int) -> PendingLifecycle | None:
        return self._pending.get(self._key(user_id, chat_id))

    def put(self, user_id: int, chat_id: int, pending: PendingLifecycle) -> None:
        self._pending[self._key(user_id, chat_id)] = pending

    def clear(self, user_id: int, chat_id: int) -> None:
        self._pending.pop(self._key(user_id, chat_id), None)
