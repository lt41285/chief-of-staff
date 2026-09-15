"""In-memory confirmation for merge/rename."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class ProjectPendingKind(StrEnum):
    MERGE = "merge"
    RENAME = "rename"
    CREATE = "create"
    ASK_NAME = "ask_name"
    ARCHIVE = "archive"
    RESTORE = "restore"
    DELETE = "delete"


@dataclass
class PendingProjectOp:
    kind: ProjectPendingKind
    keep_name: str = ""
    source_names: tuple[str, ...] = ()
    old_name: str = ""
    new_name: str = ""
    task_count: int = 0


class InMemoryProjectOpStore:
    def __init__(self) -> None:
        self._pending: dict[tuple[int, int], PendingProjectOp] = {}

    @staticmethod
    def _key(user_id: int, chat_id: int) -> tuple[int, int]:
        return (chat_id, user_id)

    def get(self, user_id: int, chat_id: int) -> PendingProjectOp | None:
        return self._pending.get(self._key(user_id, chat_id))

    def put(self, user_id: int, chat_id: int, op: PendingProjectOp) -> None:
        self._pending[self._key(user_id, chat_id)] = op

    def clear(self, user_id: int, chat_id: int) -> None:
        self._pending.pop(self._key(user_id, chat_id), None)
