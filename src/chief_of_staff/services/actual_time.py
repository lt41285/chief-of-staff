"""Parse actual effort after completing a task."""

import re

from chief_of_staff.services.available_time import parse_available_time

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})
_SKIP = re.compile(
    r"^\s*(?:не\s+пам['']ятаю|не\s+знаю|пропустити|пропуск|skip|pass|не\s+треба)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_BARE_MINUTES = re.compile(r"^\s*(\d{1,3})\s*$")


def is_skip_actual_time(text: str) -> bool:
    compact = " ".join(text.translate(_APOS).split())
    return bool(_SKIP.match(compact))


def parse_actual_minutes(text: str, *, allow_bare: bool = False) -> int | None:
    """Return minutes, or None if missing/unparsed. Skip is not a duration."""
    if is_skip_actual_time(text):
        return None
    parsed = parse_available_time(text)
    if parsed.minutes is not None:
        return parsed.minutes
    if allow_bare:
        match = _BARE_MINUTES.match(text.strip())
        if match:
            value = int(match.group(1))
            if 1 <= value <= 16 * 60:
                return value
    return None
