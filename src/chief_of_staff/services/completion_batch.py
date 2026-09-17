"""Split a multi-item completion report and resolve every item on its own.

One message may report several finished tasks. Scoring the whole message
against a single task cannot work: most of its words belong to the other
items, so the credibility gate in `completion_statement` rejects everything.
Items are therefore cut apart here and matched one by one with the existing
single-task matcher, which keeps its thresholds untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
import re
from uuid import UUID

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.completion_statement import match_completion_statement
from chief_of_staff.services.deadline_parse import strip_deadline_phrase

MIN_BATCH_ITEMS = 2

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})
_BULLET = re.compile(r"^\s*(?:[-–—•*·>]+|\d{1,2}[.)])\s+")
_INLINE_BULLET = re.compile(r"(?:(?<=\s)|^)[—–•]\s+")
_HEAD_COLON = re.compile(r":\s*$")
_DEADLINE_WORD = re.compile(r"(?i)[\s,;:.–—-]*\b(?:дедлайн|deadline|due)\b[\s,;:.–—-]*$")
_COUNT_ONLY = re.compile(
    r"(?i)^(?:я\s+|ми\s+)?"
    r"(?:викона(?:в|ла|ли|но)|зробив(?:ла|ли)?|закрив(?:ла|ли)?|"
    r"completed|finished|closed|did)\s+"
    r"(?:всі\s+|усі\s+|all\s+|these\s+)?\d{1,2}\s*"
    r"(?:задач\w*|завдан\w*|таск\w*|tasks?)\s*[.!]?$"
)
_ALL_REFERENCE = re.compile(
    r"(?i)^(?:всі|усі|все|усе|all|both|обидві|обидва)"
    r"(?:\s+(?:\d{1,2}|два|дві|три|чотири|п'ять|шість))?"
    r"(?:\s+(?:задач\w*|завдан\w*|таск\w*|tasks?|of\s+them|them))?\s*[.!]?$"
)


@dataclass(frozen=True)
class BatchMatch:
    """Tasks resolved one item each, plus items nothing could be matched to."""

    tasks: tuple[PlanCandidate, ...]
    unmatched: tuple[str, ...]


def looks_like_all_reference(text: str) -> bool:
    """True for «всі», «всі 4», «all 4 tasks» — a reference to everything asked about."""
    return bool(_ALL_REFERENCE.fullmatch(" ".join(text.translate(_APOS).split())))


def is_count_only_completion(text: str) -> bool:
    """True for «я виконав 4 задачі» — a count with no task named."""
    return bool(_COUNT_ONLY.fullmatch(" ".join(text.translate(_APOS).split())))


def split_completion_items(text: str) -> tuple[str, ...]:
    """Cut a list-shaped message into items. Empty when it is not a list."""
    lines = [line.strip() for line in text.translate(_APOS).splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ()
    if len(lines) == 1:
        lines = _split_inline(lines[0])
    items = [_strip_bullet(line) for line in _drop_head(lines)]
    return tuple(item for item in items if item)


def clean_completion_item(item: str, today: date) -> str:
    """Drop the bullet and the trailing «, дедлайн 9 вересня» metadata."""
    _deadline, rest = strip_deadline_phrase(_strip_bullet(item), today)
    rest = _DEADLINE_WORD.sub("", rest)
    return " ".join(rest.split()).strip(" .,;:—–-")


def match_completion_batch(
    items: Sequence[str],
    open_tasks: list[PlanCandidate],
    *,
    today: date,
) -> BatchMatch:
    """Resolve each item against open tasks. One task is never claimed twice."""
    resolved: list[PlanCandidate] = []
    unmatched: list[str] = []
    seen: set[UUID] = set()
    for item in items:
        cleaned = clean_completion_item(item, today)
        if not cleaned:
            continue
        found = match_completion_statement(cleaned, open_tasks)
        picked = found.selected
        if picked is None and len(found.candidates) == 1:
            picked = found.candidates[0]
        if picked is None or picked.id in seen:
            unmatched.append(cleaned)
            continue
        seen.add(picked.id)
        resolved.append(picked)
    return BatchMatch(tasks=tuple(resolved), unmatched=tuple(unmatched))


def _drop_head(lines: list[str]) -> list[str]:
    """Remove a leading «Ці завдання виконав:» header, not an item itself."""
    if len(lines) < 2:
        return lines
    first, rest = lines[0], lines[1:]
    if _HEAD_COLON.search(first):
        return rest
    bulleted = sum(1 for line in rest if _BULLET.match(line))
    if bulleted >= MIN_BATCH_ITEMS and not _BULLET.match(first):
        return rest
    return lines


def _split_inline(line: str) -> list[str]:
    """Split «… — item — item» only when bullets are unambiguously repeated."""
    marks = list(_INLINE_BULLET.finditer(line))
    if len(marks) < MIN_BATCH_ITEMS:
        return [line]
    pieces: list[str] = []
    head = line[: marks[0].start()].strip()
    if head:
        pieces.append(head)
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(line)
        piece = line[mark.end() : end].strip()
        if piece:
            pieces.append(piece)
    return pieces


def _strip_bullet(line: str) -> str:
    return _BULLET.sub("", line).strip()
