"""Deterministic parse of free working time into minutes."""

from dataclasses import dataclass
import re

_CLARIFY = (
    "Не зрозумів, скільки саме вільного часу. "
    "Напиши, наприклад: 3 години, 90 хвилин або 2.5 год."
)

_WORD_NUMBERS: dict[str, float] = {
    "один": 1,
    "одна": 1,
    "одну": 1,
    "одне": 1,
    "два": 2,
    "дві": 2,
    "три": 3,
    "чотири": 4,
    "п'ять": 5,
    "п’ять": 5,
    "шість": 6,
    "сім": 7,
    "вісім": 8,
    "дев'ять": 9,
    "дев’ять": 9,
    "десять": 10,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_HOURS = r"(?:годину|година|години|годин|год\.?|hours?|hrs?|h)"
_MINUTES = r"(?:хвилин(?:и|у)?|хв\.?|minutes?|mins?|min)"
_TOKEN = (
    r"(?:\d+(?:[.,]\d+)?|"
    + "|".join(re.escape(w) for w in sorted(_WORD_NUMBERS, key=len, reverse=True))
    + r")"
)
_HOURS_AND_MINUTES = re.compile(
    rf"({_TOKEN})\s*{_HOURS}(?:\s+(?:і|й|and)?\s*)({_TOKEN})\s*{_MINUTES}",
    re.IGNORECASE,
)
_HOURS_ONLY = re.compile(rf"({_TOKEN})\s*{_HOURS}", re.IGNORECASE)
_MINUTES_ONLY = re.compile(rf"({_TOKEN})\s*{_MINUTES}", re.IGNORECASE)


@dataclass(frozen=True)
class AvailableTimeParse:
    minutes: int | None
    clarify: str | None


def parse_available_time(text: str) -> AvailableTimeParse:
    raw = re.sub(r"\s+", " ", text).strip()
    if not raw:
        return AvailableTimeParse(minutes=None, clarify=_CLARIFY)
    folded = raw.casefold().replace(",", ".")

    special = _special_phrases(folded)
    if special is not None:
        return _ok(special)

    found: list[float] = []
    for match in _HOURS_AND_MINUTES.finditer(folded):
        hours = _to_number(match.group(1))
        minutes = _to_number(match.group(2))
        if hours is None or minutes is None:
            continue
        found.append(hours * 60 + minutes)
    if not found:
        for match in _HOURS_ONLY.finditer(folded):
            hours = _to_number(match.group(1))
            if hours is not None:
                found.append(hours * 60)
    if not found:
        for match in _MINUTES_ONLY.finditer(folded):
            minutes = _to_number(match.group(1))
            if minutes is not None:
                found.append(minutes)

    unique = {round(value, 4) for value in found}
    if len(unique) != 1:
        return AvailableTimeParse(minutes=None, clarify=_CLARIFY)
    return _ok(unique.pop())


def _special_phrases(folded: str) -> float | None:
    if re.search(r"пів\s*години", folded):
        return 30
    if re.search(r"півтори(\s+годин[уи]?)?", folded):
        return 90
    return None


def _to_number(token: str) -> float | None:
    key = token.casefold().replace(",", ".")
    if key in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[key])
    try:
        return float(key)
    except ValueError:
        return None


def _ok(minutes: float) -> AvailableTimeParse:
    if minutes <= 0 or minutes > 16 * 60:
        return AvailableTimeParse(minutes=None, clarify=_CLARIFY)
    rounded = int(round(minutes))
    if rounded <= 0:
        return AvailableTimeParse(minutes=None, clarify=_CLARIFY)
    return AvailableTimeParse(minutes=rounded, clarify=None)
