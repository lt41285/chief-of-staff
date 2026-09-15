"""Python-owned relative deadlines for task intake. Europe/Kyiv week-end = Sunday."""

from __future__ import annotations

from datetime import date, timedelta
import re

from chief_of_staff.services.date_windows import resolve_period

_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_END_OF_WEEK = re.compile(
    r"(?i)(?:до\s+)?кінц[яю]\s+(?:цього\s+)?тижн|"
    r"end\s+of\s+(?:the\s+)?week|"
    r"by\s+(?:the\s+)?end\s+of\s+(?:the\s+)?week"
)
_THIS_WEEK = re.compile(r"(?i)цього\s+тижн|this\s+week")
_NEXT_WEEK = re.compile(r"(?i)наступн\w*\s+тижн|next\s+week")
_TODAY = re.compile(r"(?i)\bсьогодні\b|\btoday\b")
_TOMORROW = re.compile(r"(?i)\bзавтра\b|\btomorrow\b")
_WEEKDAYS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"(?i)понеділк|monday"), 0),
    (re.compile(r"(?i)вівторк|tuesday"), 1),
    (re.compile(r"(?i)серед[аиу]|wednesday"), 2),
    (re.compile(r"(?i)\bчетвер(?:а|у|ом)?\b|\bthursday\b"), 3),
    (re.compile(r"(?i)п['’]?ятниц|friday"), 4),
    (re.compile(r"(?i)субот|saturday"), 5),
    (re.compile(r"(?i)неділ[юяі]|sunday"), 6),
)


def week_end(today: date) -> date:
    """Sunday of the Europe/Kyiv week that contains `today` (Monday start)."""
    window = resolve_period("this_week", today)
    assert window is not None
    return window[1]


def resolve_deadline_expression(text: str | None, today: date) -> date | None:
    if not text or not text.strip():
        return None
    cleaned = " ".join(text.split()).strip()
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        pass
    iso = _ISO.search(cleaned)
    if iso:
        try:
            return date.fromisoformat(iso.group(1))
        except ValueError:
            pass
    folded = cleaned.casefold()
    if _END_OF_WEEK.search(folded):
        return week_end(today)
    if _TODAY.search(folded):
        return today
    if _TOMORROW.search(folded):
        return today + timedelta(days=1)
    if _NEXT_WEEK.search(folded):
        window = resolve_period("next_week", today)
        return window[1] if window else None
    if _THIS_WEEK.search(folded):
        return week_end(today)
    for pattern, weekday in _WEEKDAYS:
        if pattern.search(folded):
            return _upcoming_weekday(today, weekday)
    return None


def _upcoming_weekday(today: date, weekday: int) -> date:
    delta = (weekday - today.weekday()) % 7
    if delta == 0:
        return today
    return today + timedelta(days=delta)
