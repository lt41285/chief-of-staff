"""Detect read-only people-task queries. Never creates people or tasks."""

import re

from chief_of_staff.models.task_command import TaskIntent, TaskIntentKind

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})

_INTAKE = re.compile(
    r"(?i)^\s*(?:треба|потрібно|домовитися|поговорити|надіслати|передати|"
    r"зустрітися|підготувати|написати|to\s+|need\s+to|have\s+to)\b"
)

_READ_ONLY = re.compile(
    r"(?i)(?:"
    r"\bпокажи(?:ть)?\b|\bвиведи(?:ть)?\b|\bсписок\b|\bякі\b|\bякий\b|"
    r"що\s+(?:у\s+|в\s+)?мене\s+є|що\s+є\s+по|"
    r"що\s+треба\s+обговорити|що\s+(?:мені\s+)?треба\s+обговорити|"
    r"що\s+я\s+чекаю|покажи\s+waiting|"
    r"\bshow(?:\s+me)?\b|\blist\b|what\s+tasks|what\s+do\s+i\s+have|"
    r"what\s+(?:do\s+i\s+need\s+to\s+)?discuss|what\s+am\s+i\s+waiting|"
    r"everything\s+related\s+to|пов['']язан"
    r")"
)

_WAITING_SCOPE = re.compile(
    r"(?i)що\s+я\s+чекаю|чекаю\s+від|очікую\s+від|\bwaiting\b|"
    r"what\s+am\s+i\s+waiting"
)

_DISCUSS_SCOPE = re.compile(
    r"(?i)обговорити|discuss"
)

_NAME = re.compile(
    r"(?i)(?P<conn>"
    r"\bпо\s+(?!про[еє]кт(?:у|і|а|ом|ами)?\b)"
    r"|\bз\s+"
    r"|\bіз\s+"
    r"|\bвід\s+"
    r"|related\s+to\s+"
    r"|with\s+"
    r"|from\s+"
    r"|for\s+"
    r"|де\s+є\s+"
    r"|пов['']язан\w*\s+з\s+"
    r")(?P<name>[A-Za-zА-Яа-яІіЇїЄєҐґ'’\-]+(?:\s+[A-Za-zА-Яа-яІіЇїЄєҐґ'’\-]+){0,2})"
    r"(?:\s*[.?!])?\s*$"
)

_NOISE = frozenset(
    {
        "мене",
        "мені",
        "все",
        "усе",
        "всі",
        "усі",
        "задачі",
        "задач",
        "завдання",
        "таски",
        "tasks",
        "task",
        "the",
        "my",
        "me",
        "all",
        "всіх",
        "усіх",
        "всі",
        "усі",
        "проєкт",
        "проект",
        "проєкту",
        "проекту",
        "проєкті",
        "проекті",
        "проєктів",
        "проектів",
        "проєктах",
        "проектах",
        "проєктам",
        "проектам",
        "project",
        "projects",
    }
)


def parse_people_tasks_query(text: str) -> TaskIntent | None:
    raw = " ".join(text.translate(_APOS).split()).strip()
    if not raw or _INTAKE.match(raw):
        return None
    if not _READ_ONLY.search(raw):
        return None
    match = _NAME.search(raw)
    if match is None:
        return None
    name = _clean_name(match.group("name"))
    if not name:
        return None
    connector = match.group("conn").casefold().strip()
    # One token after «по»/«for» is a project (BG), not a person.
    if connector in {"по", "for"} and len(name.split()) < 2:
        return None
    status = "waiting" if _WAITING_SCOPE.search(raw) else None
    return TaskIntent(
        kind=TaskIntentKind.PEOPLE_TASKS_QUERY,
        person_query=name,
        status_filter=status,
        task_query="discuss" if status is None and _DISCUSS_SCOPE.search(raw) else None,
    )


def looks_like_people_tasks_query(text: str) -> bool:
    return parse_people_tasks_query(text) is not None


def _clean_name(value: str) -> str:
    parts = [
        part
        for part in value.replace("’", "'").split()
        if part.casefold().strip(".,?!«»\"'") not in _NOISE
    ]
    return " ".join(parts).strip(" .,?!«»\"'")
