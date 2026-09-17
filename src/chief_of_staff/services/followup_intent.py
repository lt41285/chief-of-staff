"""Deterministic follow-ups against conversation context. No OpenAI."""

from datetime import date, timedelta
import re

from chief_of_staff.models.task_command import TaskIntent, TaskIntentKind
from chief_of_staff.services.conversation_context import ConversationSnapshot
from chief_of_staff.services.deadline_parse import parse_natural_deadline

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})

_ONLY_PROJECT = re.compile(
    r"(?i)^\s*а?\s*тільки\s+по\s+(?P<proj>.+?)\s*[.?!]?\s*$"
)
_WAITING_HIM = re.compile(
    r"(?i)(?:а\s+)?що\s+я\s+(?:від\s+(?:нього|неї|них)\s+)?чекаю|"
    r"а\s+що\s+я\s+від\s+(?:нього|неї)"
)
_DISCUSS = re.compile(r"(?i)обговорити|discuss")
_FROM_THIS = re.compile(r"(?i)з\s+цього|з\s+цих|from\s+(?:this|these)")
_DONE_THIS = re.compile(
    r"(?i)я\s+це\s+вже\s+зробив|я\s+вже\s+зробив\s+це|"
    r"already\s+did\s+(?:this|it)|i\s+already\s+did\s+(?:this|it)"
)
_POSTPONE = re.compile(r"(?i)перенес|відклад|postpone")
_ORDINAL = re.compile(
    r"(?i)\b(?:перш[ауие]|1-?[шщ]?у|first)\b|\b(?:друг[ауеи]|2-?гу|second)\b|"
    r"\b(?:трет[яюе]|3-?тю|third)\b"
)
_PRONOUN = re.compile(
    r"(?i)\b(?:він|нього|йому|ним|вона|неї|їй|нею|його)\b"
)
_STATUS_FOLLOWUP = re.compile(
    r"(?i)^\s*(?:а\s+|чи\s+є\s+)?"
    r"(?:покажи(?:ть)?\s+)?"
    r"(?P<kind>виконан[іих]+|архівн[іих]+|completed|done|waiting|"
    r"відкрит[іих]+|open)"
    r"(?:\s+(?:задач\w*|завдан\w*|таск\w*|tasks?))?"
    r"\s*[.?!]?\s*$"
)
_STATUS_DISPUTE = re.compile(
    r"(?i)(?:я\s+закрив|вони\s+(?:вже\s+)?(?:закрит|виконан)).{0,80}"
    r"(?:ти\s+кажеш|ти\s+пишеш|ти\s+показав)|"
    r"ти\s+кажеш.{0,80}(?:відкрит|закрит|виконан)"
)


def parse_context_followup(
    text: str,
    snapshot: ConversationSnapshot | None,
    *,
    today: date,
) -> TaskIntent | None:
    if snapshot is None:
        return None
    raw = " ".join(text.translate(_APOS).split()).strip()
    if not raw:
        return None

    only = _ONLY_PROJECT.match(raw)
    if only and snapshot.person_name:
        return TaskIntent(
            kind=TaskIntentKind.PEOPLE_TASKS_QUERY,
            person_query=snapshot.person_name,
            project_query=only.group("proj").strip(" .?!"),
        )
    if only:
        return TaskIntent(
            kind=TaskIntentKind.LIST_PROJECT_TASKS,
            project_query=only.group("proj").strip(" .?!"),
        )

    if _WAITING_HIM.search(raw) and snapshot.person_name:
        return TaskIntent(
            kind=TaskIntentKind.PEOPLE_TASKS_QUERY,
            person_query=snapshot.person_name,
            status_filter="waiting",
        )

    if _DISCUSS.search(raw) and snapshot.person_name:
        if _FROM_THIS.search(raw) or _PRONOUN.search(raw) or raw.casefold().startswith("а "):
            deadline = None
            if re.search(r"(?i)завтра|tomorrow", raw):
                deadline = (today + timedelta(days=1)).isoformat()
            return TaskIntent(
                kind=TaskIntentKind.PEOPLE_TASKS_QUERY,
                person_query=snapshot.person_name,
                task_query="discuss",
                deadline_on=deadline,
            )

    if _DONE_THIS.search(raw) and snapshot.task_ids:
        index = 1 if len(snapshot.task_ids) == 1 else _ordinal_index(raw)
        title = _title_at(snapshot, index or 1)
        if title:
            return TaskIntent(kind=TaskIntentKind.COMPLETE_TASK, task_query=title, task_index=index)

    if _POSTPONE.search(raw) and snapshot.task_ids:
        index = _ordinal_index(raw)
        if index is None and len(snapshot.task_ids) == 1:
            index = 1
        title = _title_at(snapshot, index) if index else None
        if title:
            dated = parse_natural_deadline(raw, today)
            return TaskIntent(
                kind=TaskIntentKind.POSTPONE_TASK,
                task_query=title,
                task_index=index,
                new_deadline=dated.isoformat() if dated else None,
            )

    if _PRONOUN.search(raw) and snapshot.person_name and re.search(
        r"(?i)покажи|що\s+у\s+мене|які\s+задач|все\s+по",
        raw,
    ):
        return TaskIntent(
            kind=TaskIntentKind.PEOPLE_TASKS_QUERY,
            person_query=snapshot.person_name,
        )

    status_hit, status = _status_followup(raw)
    person = snapshot.person_query or snapshot.person_name
    if person:
        person = person.strip().strip(" —–-\t") or None
    if status_hit and person:
        return TaskIntent(
            kind=TaskIntentKind.PEOPLE_TASKS_QUERY,
            person_query=person,
            status_filter=status,
        )
    return None


def _ordinal_index(raw: str) -> int | None:
    folded = raw.casefold()
    if re.search(r"перш|first|\b1-?", folded):
        return 1
    if re.search(r"друг|second|\b2-?", folded):
        return 2
    if re.search(r"трет|third|\b3-?", folded):
        return 3
    return None


def _title_at(snapshot: ConversationSnapshot, index: int | None) -> str | None:
    if index is None or index < 1:
        return None
    pos = index - 1
    if pos < len(snapshot.titles):
        return snapshot.titles[pos]
    return None


def looks_like_status_followup(text: str) -> bool:
    raw = " ".join(text.translate(_APOS).split()).strip()
    return _status_followup(raw)[0]


def looks_like_listed_status_dispute(text: str) -> bool:
    raw = " ".join(text.translate(_APOS).split()).strip()
    return bool(raw and _STATUS_DISPUTE.search(raw))


def followup_status_filter(text: str) -> str | None:
    """done / waiting / None(open) when the utterance is a status follow-up."""
    raw = " ".join(text.translate(_APOS).split()).strip()
    hit, status = _status_followup(raw)
    return status if hit else None


def _status_followup(raw: str) -> tuple[bool, str | None]:
    """True when the utterance only changes status of the current person query."""
    match = _STATUS_FOLLOWUP.match(raw)
    if match is None:
        return False, None
    kind = match.group("kind").casefold()
    if kind.startswith("виконан") or kind.startswith("архів") or kind in {"completed", "done"}:
        return True, "done"
    if kind == "waiting":
        return True, "waiting"
    return True, None
