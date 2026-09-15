"""In-memory per Telegram user+chat task draft. Nothing is written to the database."""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from chief_of_staff.models.task import TaskDraft


class DraftPhase(StrEnum):
    COLLECTING = "collecting"
    CONFIRMING = "confirming"
    AWAITING_PROJECT = "awaiting_project"
    DISAMBIGUATING = "disambiguating"


@dataclass
class TaskSession:
    draft: TaskDraft = field(default_factory=TaskDraft)
    phase: DraftPhase = DraftPhase.COLLECTING
    similar_project_id: UUID | None = None
    similar_project_name: str | None = None
    requested_project: str | None = None
    presented_project_names: tuple[str, ...] = ()


class InMemoryTaskSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[tuple[int, int], TaskSession] = {}

    @staticmethod
    def _key(user_id: int, chat_id: int) -> tuple[int, int]:
        return (chat_id, user_id)

    def get(self, user_id: int, chat_id: int) -> TaskSession | None:
        return self._sessions.get(self._key(user_id, chat_id))

    def put(self, user_id: int, chat_id: int, session: TaskSession) -> None:
        self._sessions[self._key(user_id, chat_id)] = session

    def clear(self, user_id: int, chat_id: int) -> None:
        self._sessions.pop(self._key(user_id, chat_id), None)
