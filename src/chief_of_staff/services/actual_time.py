"""Parse actual effort after completing a task."""

import re

from chief_of_staff.services.available_time import parse_available_time

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})
_SKIP = re.compile(
    r"^\s*(?:не\s+пам['']ятаю|не\s+знаю|пропустити|пропуск|skip|pass|не\s+треба)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_BARE_MINUTES = re.compile(r"^\s*(\d{1,3})\s*$")
_HEDGE = re.compile(
    r"(?i)^\s*(?:десь|приблизно|близько|майже|about|around|roughly|~)\s+"
)


def is_skip_actual_time(text: str) -> bool:
    compact = " ".join(text.translate(_APOS).split())
    return bool(_SKIP.match(compact))


def parse_actual_minutes(text: str, *, allow_bare: bool = False) -> int | None:
    """Return minutes, or None if missing/unparsed. Skip is not a duration."""
    if is_skip_actual_time(text):
        return None
    cleaned = _HEDGE.sub("", " ".join(text.split()), count=1).strip() or text
    parsed = parse_available_time(cleaned)
    if parsed.minutes is not None:
        return parsed.minutes
    if allow_bare:
        match = _BARE_MINUTES.match(cleaned)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 16 * 60:
                return value
    return None


def is_actual_time_reply(text: str) -> bool:
    """True when the utterance is a duration or an explicit skip, in pending context."""
    return is_skip_actual_time(text) or parse_actual_minutes(text, allow_bare=True) is not None
