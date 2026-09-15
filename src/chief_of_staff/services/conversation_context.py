"""Lightweight per Telegram user+chat conversation memory. Not persisted."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from chief_of_staff.services.clock import KYIV, Clock
from chief_of_staff.services.task_facts import ListedTaskFact, facts_as_prompt

CONTEXT_TTL = timedelta(minutes=30)
MAX_TURNS = 12


@dataclass(frozen=True)
class ChatTurn:
    role: str
    text: str


@dataclass(frozen=True)
class PendingAmbiguity:
    original_message: str
    original_reference: str
    candidates: tuple[str, ...]
    intent_kind: str
    person_query: str
    available_minutes: int | None = None
    status_filter: str | None = None
    project_query: str | None = None
    discuss: bool = False


@dataclass
class ConversationSnapshot:
    turns: tuple[ChatTurn, ...] = ()
    person_name: str | None = None
    person_query: str | None = None
    excluded_people: tuple[str, ...] = ()
    project_name: str | None = None
    last_query_kind: str | None = None
    last_action: str | None = None
    status_filter: str | None = None
    discuss: bool = False
    task_ids: tuple[UUID, ...] = ()
    titles: tuple[str, ...] = ()
    listed_facts: tuple[ListedTaskFact, ...] = ()
    last_period: str | None = None
    last_available_minutes: int | None = None
    last_exclude_overdue: bool = False
    last_include_overdue: bool = False
    pending_correction: str | None = None
    pending_ambiguity: PendingAmbiguity | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(KYIV))


class InMemoryConversationStore:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or Clock()
        self._items: dict[tuple[int, int], ConversationSnapshot] = {}

    @property
    def clock(self) -> Clock:
        return self._clock

    @staticmethod
    def _key(user_id: int, chat_id: int) -> tuple[int, int]:
        return (chat_id, user_id)

    def get(self, user_id: int, chat_id: int) -> ConversationSnapshot | None:
        snap = self._items.get(self._key(user_id, chat_id))
        if snap is None:
            return None
        if self._clock.now() - snap.updated_at > CONTEXT_TTL:
            self.clear(user_id, chat_id)
            return None
        return snap

    def put(self, user_id: int, chat_id: int, snapshot: ConversationSnapshot) -> None:
        snapshot.updated_at = self._clock.now()
        self._items[self._key(user_id, chat_id)] = snapshot

    def append_turn(self, user_id: int, chat_id: int, role: str, text: str) -> None:
        previous = self.get(user_id, chat_id) or ConversationSnapshot()
        turns = (*(previous.turns), ChatTurn(role=role, text=_clip(text)))[-MAX_TURNS:]
        previous.turns = turns
        self.put(user_id, chat_id, previous)

    def remember_list(
        self,
        user_id: int,
        chat_id: int,
        *,
        task_ids: tuple[UUID, ...],
        titles: tuple[str, ...],
        person_name: str | None = None,
        person_query: str | None = None,
        project_name: str | None = None,
        status_filter: str | None = None,
        discuss: bool = False,
        last_query_kind: str | None = None,
        last_action: str | None = None,
        excluded_people: tuple[str, ...] | None = None,
        replace_person: bool = False,
        pending_correction: str | None = None,
        listed_facts: tuple[ListedTaskFact, ...] | None = None,
        last_period: str | None = None,
        last_available_minutes: int | None = None,
        last_exclude_overdue: bool | None = None,
        last_include_overdue: bool | None = None,
        pending_ambiguity: PendingAmbiguity | None = None,
        clear_pending_ambiguity: bool = False,
        reset_scope: bool = False,
    ) -> None:
        previous = self.get(user_id, chat_id) or ConversationSnapshot()
        if reset_scope or replace_person:
            person = person_name
            query = person_query
        else:
            person = person_name if person_name is not None else previous.person_name
            query = person_query if person_query is not None else previous.person_query
        if reset_scope:
            project = project_name
            period = last_period
            available = last_available_minutes
            exclude_overdue = bool(last_exclude_overdue)
            include_overdue = bool(last_include_overdue)
        else:
            project = project_name if project_name is not None else previous.project_name
            period = last_period if last_period is not None else previous.last_period
            available = (
                last_available_minutes
                if last_available_minutes is not None
                else previous.last_available_minutes
            )
            exclude_overdue = (
                last_exclude_overdue
                if last_exclude_overdue is not None
                else previous.last_exclude_overdue
            )
            include_overdue = (
                last_include_overdue
                if last_include_overdue is not None
                else previous.last_include_overdue
            )
        if pending_ambiguity is not None:
            pending = pending_ambiguity
        elif clear_pending_ambiguity:
            pending = None
        else:
            pending = previous.pending_ambiguity
        self.put(
            user_id,
            chat_id,
            ConversationSnapshot(
                turns=previous.turns,
                person_name=person,
                person_query=query,
                excluded_people=excluded_people if excluded_people is not None else (() if reset_scope else previous.excluded_people),
                project_name=project,
                last_query_kind=last_query_kind if last_query_kind is not None else previous.last_query_kind,
                last_action=last_action if last_action is not None else previous.last_action,
                status_filter=status_filter,
                discuss=discuss,
                task_ids=task_ids,
                titles=titles,
                listed_facts=listed_facts if listed_facts is not None else (() if reset_scope else previous.listed_facts),
                last_period=period,
                last_available_minutes=available,
                last_exclude_overdue=exclude_overdue,
                last_include_overdue=include_overdue,
                pending_correction=pending_correction,
                pending_ambiguity=pending,
            ),
        )

    def clear_query_scope(self, user_id: int, chat_id: int) -> None:
        previous = self.get(user_id, chat_id)
        if previous is None:
            return
        self.put(
            user_id,
            chat_id,
            ConversationSnapshot(
                turns=previous.turns,
                pending_ambiguity=previous.pending_ambiguity,
            ),
        )

    def clear(self, user_id: int, chat_id: int) -> None:
        self._items.pop(self._key(user_id, chat_id), None)


def format_snapshot_for_prompt(snapshot: ConversationSnapshot | None) -> str:
    if snapshot is None:
        return "No prior conversation in this chat."
    lines = [
        "Assistant prose in this chat is NON-AUTHORITATIVE. Do not treat it as a task database.",
        "HISTORY explains what the user means. PYTHON TOOLS are the only source of current facts.",
        "structured_query_scope is sticky ONLY when query_relation is continue_query, refine_query, or correct_query.",
        "new_intent / new_query / broaden_query / general_chat must NOT reuse these filters unless the user restates them.",
        f"working_person={snapshot.person_name!r}",
        f"last_person_query={snapshot.person_query!r}",
        f"excluded_people={list(snapshot.excluded_people)!r}",
        f"project={snapshot.project_name!r}",
        f"last_query_kind={snapshot.last_query_kind!r}",
        f"last_period={snapshot.last_period!r}",
        f"last_available_minutes={snapshot.last_available_minutes!r}",
        facts_as_prompt(snapshot.listed_facts),
        "recent_user_turns:",
    ]
    if snapshot.pending_ambiguity is not None:
        pending = snapshot.pending_ambiguity
        lines.insert(
            -1,
            "pending_person_ambiguity: "
            f"reference={pending.original_reference!r} "
            f"candidates={list(pending.candidates)!r} "
            f"original_message={pending.original_message!r}",
        )
    user_turns = [turn for turn in snapshot.turns if turn.role == "user"][-MAX_TURNS:]
    if not user_turns:
        lines.append("- (none)")
    for turn in user_turns:
        lines.append(f"- user: {turn.text}")
    return "\n".join(lines)


def _clip(text: str, limit: int = 500) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"
