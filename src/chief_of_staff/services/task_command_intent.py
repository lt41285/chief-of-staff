"""Parse task-management commands. Prefer deterministic rules; AI is fallback."""

import re
from datetime import datetime
from typing import Protocol

from chief_of_staff.models.task_command import TaskIntent, TaskIntentKind
from chief_of_staff.prompts.task_command import TASK_COMMAND_INSTRUCTIONS
from chief_of_staff.services.available_time import parse_available_time
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.completion_statement import (
    COMPLETION_ACTION_INSTRUCTIONS,
    CompletionActionLabel,
)
from chief_of_staff.services.deadline_parse import strip_deadline_phrase
from chief_of_staff.services.people_query import (
    looks_like_people_tasks_query,
    parse_people_tasks_query,
)

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})

_HEAD = (
    r"(?:покажи(?:ть)?(?:\s+мені)?|виведи(?:ть)?|"
    r"дай(?:те)?(?:\s+мені)?\s+список|список|покажи\s+список|"
    r"show(?:\s+me)?|list|які|який)"
)
_ALL = r"(?:всіх|всі|усі|all|моїх|мої|my)"
_OPEN_ADJ = r"(?:відкрит(?:і|их|у)?|open)\s+"
_TASK_NOUN = r"(?:завдан(?:ня|ь)|задач(?:а|і|у|ей)?|таск(?:и|а)?|tasks?)"
_PROJ_NOUN = r"(?:про[еє]кт(?:і|у|ах|и|ів|а|ам|ами)?|projects?)"
_PREP = r"(?:по|в|у|з|із|для|for|in|on|from)"
_GROUP_TAIL = r"(?:по\s+про[еє]ктах|за\s+про[еє]ктами|by\s+projects?|across\s+projects?)"
_ALL_QUANT = r"(?:всіх|всі|всім|всіма|усіх|усі|усім|усіма|all(?:\s+of)?)"
_MY_MOD = r"(?:моїх|мої|моїми|my)"
_ALL_PROJECT_PHRASE = re.compile(
    r"(?:"
    r"(?:по|у|в|з|із|за)\s+"
    rf"{_ALL_QUANT}\s+(?:{_MY_MOD}\s+)?{_PROJ_NOUN}"
    r"|(?:across|from|for)\s+all(?:\s+of)?(?:\s+my)?\s+projects?"
    r")",
    re.IGNORECASE,
)
_TASK_NOUN_SEARCH = re.compile(_TASK_NOUN, re.IGNORECASE)
_ALL_ACROSS = re.compile(
    rf"^{_HEAD}\s+(?:{_ALL}\s+)?(?:{_OPEN_ADJ})?{_TASK_NOUN}\s+"
    rf"{_GROUP_TAIL}\s*[.?!]?$",
    re.IGNORECASE,
)
_ALL_BARE = re.compile(
    rf"^{_HEAD}\s+(?:{_ALL}\s+)?(?:{_OPEN_ADJ})?(?:мої\s+|my\s+)?{_TASK_NOUN}\s*[.?!]?$",
    re.IGNORECASE,
)
_SCOPED = re.compile(
    rf"^{_HEAD}\s+(?:{_ALL}\s+)?(?:{_OPEN_ADJ})?"
    rf"(?:виконан(?:і|их)\s+|completed\s+|done\s+|waiting\s+)?"
    rf"{_TASK_NOUN}\s+(?:є\s+|are\s+)?{_PREP}\s+(?:{_PROJ_NOUN}\s+)?(?P<q>.+?)\s*[.?!]?$",
    re.IGNORECASE,
)
_GROUPED_LOOSE = re.compile(
    rf"{_ALL}\s+{_TASK_NOUN}.*{_GROUP_TAIL}",
    re.IGNORECASE,
)
_CREATE_VERB = re.compile(r"^\s*(?:додай|створи|create|add)\b", re.IGNORECASE)
_GENERIC_PROJECT = frozenset(
    {"проєктах", "проектах", "проєкти", "проекти", "projects", "project"}
)
_GENERIC_PROJECT_QUERY = re.compile(
    rf"^{_ALL_QUANT}(?:\s+{_MY_MOD})*(?:\s+{_PROJ_NOUN})?$",
    re.IGNORECASE,
)
_POSTPONE = re.compile(
    r"(?:відклад|перенес|змін\w*\s+дедлайн|дедлайн\s+задач|"
    r"postpone|defer|snooze)",
    re.IGNORECASE,
)
_WAITING = re.compile(
    r"(?:\bwaiting\b|чекаю|очікую|у\s+waiting|as\s+waiting)",
    re.IGNORECASE,
)
_RESUME = re.compile(
    r"(?:поверни|повернути|\bв\s+роботу\b|більше\s+не\s+waiting|"
    r"не\s+waiting|resume(?:\s+the)?)",
    re.IGNORECASE,
)
_CANCEL_TASK = re.compile(
    r"(?:скасуй(?:те)?\s+(?:цю\s+)?(?:задач|завдан)|"
    r"cancel\s+(?:this\s+)?task|скасувати\s+(?:цю\s+)?(?:задач|завдан))",
    re.IGNORECASE,
)
_UPDATE = re.compile(
    r"(?:онов(?:и|ити)\s+(?:задач|завдан)|зміни(?:ти)?\s+(?:задач|завдан)|"
    r"update\s+(?:the\s+)?task|change\s+(?:the\s+)?task)",
    re.IGNORECASE,
)
_COMPLETE = re.compile(
    r"(?:виконан[оаиуеим]+|\bвикона(?:в|ла|ли)\b|"
    r"познач(?:ити)?\s+як\s+(?:виконан|готов)|"
    r"як\s+(?:виконан[оаиуеим]+|готов[уаое])|"
    r"\bготово\b|\bdone\b|\bcompleted\b|"
    r"я\s+вже|already\s+(?:did|done|called|spoke|talked)|"
    r"mark\s+.+\s+as\s+(?:done|complete))",
    re.IGNORECASE,
)
_DURATION_TAIL = re.compile(
    r"(?i)(?:,\s*)?(?:зайняло|took(?:\s+me)?)\s+.+$"
)
_TRIM_PATTERNS = (
    re.compile(r"^(?:done|completed|виконано|готово)\s*[:\-—–]?\s*", re.IGNORECASE),
    re.compile(r"^познач(?:и)?\s+(?:задачу|завдання|task)?\s*", re.IGNORECASE),
    re.compile(r"\s+як\s+(?:виконан\w*|готов\w*)\s*[.!]?\s*$", re.IGNORECASE),
    re.compile(r"^я\s+вже\s+", re.IGNORECASE),
    re.compile(r"^(?:задач(?:а|у|і)|завдання)\s+", re.IGNORECASE),
    re.compile(r"\s*[—–-]\s*виконан\w*\s*[.!]?\s*$", re.IGNORECASE),
    re.compile(r"^mark\s+(?:the\s+)?(?:task\s+)?", re.IGNORECASE),
    re.compile(r"\s+as\s+(?:done|complete)\s*[.!]?\s*$", re.IGNORECASE),
    re.compile(
        r"(?:,\s*)?(?:познач(?:и)?\s+як\s+(?:виконан\w*|готов\w*))\s*[.!]?\s*$",
        re.IGNORECASE,
    ),
)


class TaskIntentParser(Protocol):
    async def parse(self, text: str) -> TaskIntent: ...


def parse_task_intent_deterministic(text: str) -> TaskIntent | None:
    raw = " ".join(text.translate(_APOS).split()).strip()
    if not raw:
        return None
    person_query = parse_people_tasks_query(raw)
    if person_query is not None:
        return person_query
    listed = _parse_list_query(raw)
    if listed is not None:
        return listed
    if _POSTPONE.search(raw) and _WAITING.search(raw) and not _RESUME.search(raw):
        return TaskIntent(kind=TaskIntentKind.UPDATE_TASK, task_query=_query_from(raw))
    if _RESUME.search(raw) and not _COMPLETE.search(raw):
        return TaskIntent(
            kind=TaskIntentKind.RESUME_TASK,
            task_query=_lifecycle_query(raw, extra=_RESUME),
        )
    if _POSTPONE.search(raw) and not _COMPLETE.search(raw):
        return _postpone_intent(raw)
    if _WAITING.search(raw) and not _COMPLETE.search(raw):
        return _waiting_intent(raw)
    if _CANCEL_TASK.search(raw):
        return TaskIntent(kind=TaskIntentKind.CANCEL_TASK, task_query=_query_from(raw))
    if _UPDATE.search(raw) and not _COMPLETE.search(raw):
        return TaskIntent(kind=TaskIntentKind.UPDATE_TASK, task_query=_query_from(raw))
    if _COMPLETE.search(raw) and not _looks_like_done_list(raw):
        return TaskIntent(kind=TaskIntentKind.COMPLETE_TASK, task_query=_complete_query(raw))
    return None


def looks_like_task_command(text: str) -> bool:
    if parse_task_intent_deterministic(text) is not None:
        return True
    folded = text.translate(_APOS).casefold()
    markers = (
        "виконан",
        "виконав",
        "виконала",
        "виконали",
        "готово",
        "познач",
        "я вже",
        "done",
        "completed",
        "відклад",
        "перенес",
        "postpone",
        "чекаю",
        "waiting",
        "поверни",
        "resume",
        "скасуй задачу",
        "cancel task",
        "онови задачу",
        "update task",
    )
    return any(m in folded for m in markers)


def looks_like_task_query(text: str) -> bool:
    parsed = parse_task_intent_deterministic(text)
    if parsed is not None and parsed.kind in {
        TaskIntentKind.LIST_TASKS,
        TaskIntentKind.LIST_PROJECT_TASKS,
        TaskIntentKind.LIST_ALL_TASKS,
        TaskIntentKind.PEOPLE_TASKS_QUERY,
    }:
        return True
    if looks_like_people_tasks_query(text):
        return True
    if looks_like_status_list_query(text):
        return True
    folded = text.translate(_APOS).casefold()
    has_head = bool(
        re.search(
            r"\b(?:покажи|покажіть|виведи|список|які|який|show|list|чи\s+є)\b",
            folded,
        )
    )
    has_all_tasks = bool(
        re.search(r"(?:всіх?|усіх?|all)\s+(?:задач|завдан|таск|tasks?)", folded)
    )
    has_noun = bool(re.search(r"задач|завдан|таск|\btasks?\b", folded))
    return (has_head or has_all_tasks) and has_noun


def looks_like_status_list_query(text: str) -> bool:
    """Read-only list by status. Past-tense completion is not this."""
    raw = " ".join(text.translate(_APOS).split())
    if not raw:
        return False
    if _looks_like_done_list(raw):
        return True
    folded = raw.casefold()
    has_status = bool(
        re.search(
            r"виконан[іих]+|архівн[іих]+|відкрит[іих]+|\bwaiting\b|"
            r"completed|\bopen\s+tasks?\b|\bdone\s+tasks?\b",
            folded,
        )
    )
    if not has_status:
        return False
    has_list_shape = bool(
        looks_like_list_head(raw)
        or re.search(r"чи\s+є|є\s+(?:виконан|архівн|відкрит)|покажи|які|список", folded)
    )
    has_noun = bool(re.search(r"задач|завдан|таск|\btasks?\b|\bwaiting\b", folded))
    return has_list_shape and has_noun


def _parse_list_query(raw: str) -> TaskIntent | None:
    if _CREATE_VERB.match(raw):
        return None
    status = _status_filter(raw)
    if _is_all_projects_list(raw):
        return TaskIntent(kind=TaskIntentKind.LIST_ALL_TASKS, status_filter=status)
    if _ALL_ACROSS.match(raw):
        return TaskIntent(kind=TaskIntentKind.LIST_ALL_TASKS, status_filter=status)
    scoped = _SCOPED.match(raw)
    if scoped:
        query = _clean_project_query(scoped.group("q"))
        if _is_generic_project_query(query):
            return TaskIntent(kind=TaskIntentKind.LIST_ALL_TASKS, status_filter=status)
        if query:
            return TaskIntent(
                kind=TaskIntentKind.LIST_PROJECT_TASKS,
                project_query=query,
                status_filter=status,
            )
    if _ALL_BARE.match(raw):
        return TaskIntent(kind=TaskIntentKind.LIST_ALL_TASKS, status_filter=status)
    if _GROUPED_LOOSE.search(raw):
        return TaskIntent(kind=TaskIntentKind.LIST_ALL_TASKS, status_filter=status)
    return None


def _is_all_projects_list(raw: str) -> bool:
    if not _TASK_NOUN_SEARCH.search(raw) or not _ALL_PROJECT_PHRASE.search(raw):
        return False
    if looks_like_list_head(raw):
        return True
    return bool(
        re.search(
            r"(?:всіх?|усіх?|all)\s+(?:задач|завдан|таск|tasks?)",
            raw.casefold(),
        )
    )


def _is_generic_project_query(query: str) -> bool:
    folded = " ".join(query.casefold().split())
    return folded in _GENERIC_PROJECT or bool(_GENERIC_PROJECT_QUERY.fullmatch(folded))


def _status_filter(raw: str) -> str | None:
    folded = raw.casefold()
    if re.search(r"waiting|очікуван", folded):
        return "waiting"
    if re.search(
        r"виконан[іих]+|архівн[іих]+|completed|\bdone\s+tasks?\b",
        folded,
    ) and (
        looks_like_list_head(raw)
        or re.search(r"чи\s+є|є\s+(?:виконан|архівн)|задач", folded)
    ):
        return "done"
    return None


def looks_like_list_head(text: str) -> bool:
    return bool(
        re.search(
            r"^\s*(?:покажи|покажіть|виведи|дай(?:те)?(?:\s+мені)?\s+список|"
            r"список|які|який|show|list)\b",
            text.translate(_APOS),
            re.IGNORECASE,
        )
    )


def _clean_project_query(value: str) -> str:
    text = " ".join(value.split()).strip(" .?!")
    text = re.sub(r"(?i)^(проєкт|проект|project)\s+", "", text).strip()
    return text


def looks_like_complete_command(text: str) -> bool:
    parsed = parse_task_intent_deterministic(text)
    if parsed is not None:
        return parsed.kind == TaskIntentKind.COMPLETE_TASK
    if _looks_like_done_list(text.translate(_APOS)):
        return False
    return bool(_COMPLETE.search(text.translate(_APOS)))


def _looks_like_done_list(raw: str) -> bool:
    return bool(
        re.search(
            r"(?i)(?:виконан[іих]+|архівн[іих]+|completed|done)\s+"
            r"(?:задач|завдан|таск|tasks?)",
            raw,
        )
    )


def _postpone_intent(raw: str) -> TaskIntent:
    today = datetime.now(KYIV).date()
    deadline, rest = strip_deadline_phrase(raw, today)
    query = _lifecycle_query(rest, extra=_POSTPONE)
    return TaskIntent(
        kind=TaskIntentKind.POSTPONE_TASK,
        task_query=query or rest,
        new_deadline=deadline.isoformat() if deadline else None,
    )


def _waiting_intent(raw: str) -> TaskIntent:
    person, rest = _extract_waiting_person(raw)
    query = _lifecycle_query(rest, extra=_WAITING)
    return TaskIntent(
        kind=TaskIntentKind.WAITING_TASK,
        task_query=query or person or rest,
        waiting_for=person,
    )


def _lifecycle_query(raw: str, extra: re.Pattern[str]) -> str:
    text = extra.sub(" ", raw)
    text = re.sub(
        r"(?i)\b(?:задач(?:у|а|і|ей)?|завдання|task|дедлайн|deadline|until|till|"
        r"постав(?:ити)?|познач(?:ити)?|mark|as|the|про|по|у|в|на|до|"
        r"відповідь|документ|зараз|now)\b",
        " ",
        text,
    )
    return _query_from(text)


def _extract_waiting_person(raw: str) -> tuple[str | None, str]:
    match = re.search(
        r"(?i)(?:відповідь\s+)?(?:від|from)\s+(?P<name>[A-Za-zА-Яа-яІіЇїЄєҐґ'’\-]+(?:\s+[A-Za-zА-Яа-яІіЇїЄєҐґ'’\-]+)?)",
        raw,
    )
    if not match:
        waiting_for = re.search(r"(?i)waiting\s+for\s+(?P<name>[A-Za-zА-Яа-яІіЇїЄєҐґ'’\-]+)", raw)
        if waiting_for is None:
            return None, raw
        name = waiting_for.group("name").strip()
        rest = (raw[: waiting_for.start()] + " " + raw[waiting_for.end() :]).strip()
        return name, rest
    name = match.group("name").strip()
    rest = (raw[: match.start()] + " " + raw[match.end() :]).strip()
    parts = [
        part
        for part in name.split()
        if part.casefold()
        not in {"бюджет", "візу", "візі", "задачу", "завдання", "task", "the", "документ"}
    ]
    return (" ".join(parts) or None, rest)


def _complete_query(raw: str) -> str:
    text = _DURATION_TAIL.sub("", raw).strip()
    for pattern in _TRIM_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(r"(?i)\b(?:задачу|завдання|task)\b", " ", text)
    return _query_from(text)


def _query_from(value: str) -> str:
    text = " ".join(value.split()).strip(" .,!;:—–-")
    duration = parse_available_time(text)
    if duration.minutes is not None:
        text = re.sub(
            r"(?i)\b\d+(?:[.,]\d+)?\s*(?:год(?:ин[уи]?)?|хв(?:илин(?:и|у)?)?|hours?|hrs?|minutes?|mins?)\b",
            " ",
            text,
        )
        text = re.sub(r"(?i)пів\s*години|півтори(\s+годин[уи]?)?", " ", text)
    return " ".join(text.split()).strip(" .,!;:—–-")


class OpenAITaskIntentParser:
    def __init__(self, client: object) -> None:
        self._client = client

    async def parse(self, text: str) -> TaskIntent:
        parsed = parse_task_intent_deterministic(text)
        if parsed is not None:
            return parsed
        schema = await self._client.parse_structured(  # type: ignore[attr-defined]
            user_input=text,
            instructions=TASK_COMMAND_INSTRUCTIONS,
            text_format=TaskIntent,
        )
        return schema

    async def classify_as_completed_action(self, text: str) -> bool:
        schema = await self._client.parse_structured(  # type: ignore[attr-defined]
            user_input=text,
            instructions=COMPLETION_ACTION_INSTRUCTIONS,
            text_format=CompletionActionLabel,
        )
        return bool(
            schema.is_completed_past_action and not schema.is_prospective_or_imperative
        )
