"""Resolve relative and written dates in Europe/Kyiv. No LLM."""

from datetime import date, timedelta
import re

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})

_MONTHS = {
    "січня": 1,
    "січень": 1,
    "лютого": 2,
    "лютий": 2,
    "березня": 3,
    "березень": 3,
    "квітня": 4,
    "квітень": 4,
    "травня": 5,
    "травень": 5,
    "червня": 6,
    "червень": 6,
    "липня": 7,
    "липень": 7,
    "серпня": 8,
    "серпень": 8,
    "вересня": 9,
    "вересень": 9,
    "жовтня": 10,
    "жовтень": 10,
    "листопада": 11,
    "листопад": 11,
    "грудня": 12,
    "грудень": 12,
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_WEEKDAYS = {
    "понеділок": 0,
    "понеділка": 0,
    "monday": 0,
    "вівторок": 1,
    "вівторка": 1,
    "tuesday": 1,
    "середу": 2,
    "середа": 2,
    "wednesday": 2,
    "четвер": 3,
    "четверга": 3,
    "thursday": 3,
    "п'ятницю": 4,
    "п'ятниця": 4,
    "п'ятниці": 4,
    "friday": 4,
    "суботу": 5,
    "субота": 5,
    "saturday": 5,
    "неділю": 6,
    "неділя": 6,
    "sunday": 6,
}

def _uk_ordinals() -> dict[str, int]:
    ones = {
        1: ("перше", "першого", "перший"),
        2: ("друге", "другого", "другий"),
        3: ("третє", "трете", "третього", "третій"),
        4: ("четверте", "четвертого", "четвертий"),
        5: ("п'яте", "п'ятого", "п'ятий"),
        6: ("шосте", "шостого", "шостий"),
        7: ("сьоме", "сьомого", "сьомий"),
        8: ("восьме", "восьмого", "восьмий"),
        9: ("дев'яте", "дев'ятого", "дев'ятий"),
    }
    teens = {
        10: ("десяте", "десятого", "десятий"),
        11: ("одинадцяте", "одинадцятого", "одинадцятий"),
        12: ("дванадцяте", "дванадцятого", "дванадцятий"),
        13: ("тринадцяте", "тринадцятого", "тринадцятий"),
        14: ("чотирнадцяте", "чотирнадцятого", "чотирнадцятий"),
        15: ("п'ятнадцяте", "п'ятнадцятого", "п'ятнадцятий"),
        16: ("шістнадцяте", "шістнадцятого", "шістнадцятий"),
        17: ("сімнадцяте", "сімнадцятого", "сімнадцятий"),
        18: ("вісімнадцяте", "вісімнадцятого", "вісімнадцятий"),
        19: ("дев'ятнадцяте", "дев'ятнадцятого", "дев'ятнадцятий"),
        20: ("двадцяте", "двадцятого", "двадцятий"),
        30: ("тридцяте", "тридцятого", "тридцятий"),
    }
    mapping: dict[str, int] = {}
    for day, forms in {**ones, **teens}.items():
        for form in forms:
            mapping[form] = day
            mapping[form.replace("'", "’")] = day
    for tens, word in ((20, "двадцять"), (30, "тридцять")):
        for day, forms in ones.items():
            value = tens + day
            if value > 31:
                continue
            for form in forms:
                mapping[f"{word} {form}"] = value
                mapping[f"{word} {form.replace(chr(39), '’')}"] = value
                mapping[f"{word}{form}"] = value
    return mapping


_ORDINALS = _uk_ordinals()
# ISO YYYY-MM-DD. Numeric day.month.year (30.09.26 / 30.09.2026 / 30/09/26)
# is always Europe/Kyiv day-then-month, never US month-day.
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DOTTED = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{2}|\d{4})\b")
_MONTH_ALT = "|".join(re.escape(name) for name in _MONTHS)
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})\s+(" + _MONTH_ALT + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_ORDINAL_DAY_MONTH = re.compile(
    r"\b("
    + "|".join(re.escape(name) for name in sorted(_ORDINALS, key=len, reverse=True))
    + r")\s+("
    + _MONTH_ALT
    + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_MONTH_DAY = re.compile(
    r"\b("
    + "|".join(re.escape(name) for name in _MONTHS if name.isascii())
    + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b",
    re.IGNORECASE,
)
_END_OF_NEXT_WEEK = re.compile(
    r"(?i)(?:до\s+)?кінц[яю]\s+наступн\w*\s+тижн|end\s+of\s+next\s+week"
)
_END_OF_WEEK = re.compile(
    r"(?i)(?:до\s+)?кінц[яю]\s+(?:цього\s+)?тижн|end\s+of\s+(?:the\s+)?week"
)
_END_OF_NEXT_MONTH = re.compile(
    r"(?i)(?:до\s+)?кінц[яю]\s+наступн\w*\s+місяц|end\s+of\s+next\s+month"
)
_END_OF_MONTH = re.compile(
    r"(?i)(?:до\s+)?кінц[яю]\s+(?:цього\s+|поточного\s+)?місяц|end\s+of\s+(?:the\s+|this\s+)?month"
)
_START_NEXT_WEEK = re.compile(
    r"(?i)початк[уі]\s+наступн\w*\s+тижн|start\s+of\s+next\s+week"
)
_START_NEXT_MONTH = re.compile(
    r"(?i)початк[уі]\s+наступн\w*\s+місяц|start\s+of\s+next\s+month"
)
_RELATIVE = re.compile(
    r"\b(сьогодні|today|завтра|tomorrow|післязавтра|day after tomorrow)\b",
    re.IGNORECASE,
)
_WEEKDAY = re.compile(
    r"(?:наступн(?:ого|ий)\s+|next\s+)?("
    + "|".join(re.escape(name) for name in _WEEKDAYS)
    + r")\b",
    re.IGNORECASE,
)


def parse_natural_deadline(text: str, today: date) -> date | None:
    cleaned = " ".join(text.translate(_APOS).split())
    if not cleaned:
        return None
    found = _scan(cleaned, today)
    return found[0] if found else None


def strip_deadline_phrase(text: str, today: date) -> tuple[date | None, str]:
    cleaned = " ".join(text.translate(_APOS).split())
    found = _scan(cleaned, today)
    if found is None:
        return None, cleaned
    deadline, span = found
    remainder = (cleaned[: span[0]] + " " + cleaned[span[1] :]).strip()
    remainder = re.sub(
        r"(?i)\b(?:на|до|until|till|to|on)\s*$",
        "",
        remainder,
    )
    remainder = re.sub(r"(?i)^\s*(?:на|до|until|till|to|on)\b", "", remainder)
    return deadline, " ".join(remainder.split()).strip(" .,;:—–-")


def _scan(text: str, today: date) -> tuple[date, tuple[int, int]] | None:
    last: tuple[date, tuple[int, int]] | None = None
    for match in _ISO.finditer(text):
        try:
            last = (date.fromisoformat(match.group(1)), match.span())
        except ValueError:
            continue
    for match in _DOTTED.finditer(text):
        parsed = _dotted_calendar_date(today, int(match.group(1)), int(match.group(2)), match.group(3))
        if parsed is not None:
            last = (parsed, match.span())
    for match in _DAY_MONTH.finditer(text):
        parsed = _safe_calendar_date(today, int(match.group(1)), _month(match.group(2)), match.group(3))
        if parsed is not None:
            last = (parsed, match.span())
    for match in _ORDINAL_DAY_MONTH.finditer(text):
        day = _ORDINALS[match.group(1).translate(_APOS).casefold()]
        parsed = _safe_calendar_date(today, day, _month(match.group(2)), match.group(3))
        if parsed is not None:
            last = (parsed, match.span())
    for match in _MONTH_DAY.finditer(text):
        parsed = _safe_calendar_date(today, int(match.group(2)), _month(match.group(1)), match.group(3))
        if parsed is not None:
            last = (parsed, match.span())
    for pattern, resolver in (
        (_END_OF_NEXT_MONTH, _next_month_end),
        (_END_OF_MONTH, _month_end),
        (_END_OF_NEXT_WEEK, _next_week_end),
        (_END_OF_WEEK, _week_end),
        (_START_NEXT_WEEK, _next_week_start),
        (_START_NEXT_MONTH, _next_month_start),
    ):
        found = pattern.search(text)
        if found:
            last = (resolver(today), found.span())
    relative = _RELATIVE.search(text)
    if relative:
        last = (_relative(relative.group(1), today), relative.span())
    weekday = _WEEKDAY.search(text)
    if weekday:
        force_next = bool(re.search(r"(?i)наступн|next", weekday.group(0)))
        last = (
            _next_weekday(today, _WEEKDAYS[weekday.group(1).translate(_APOS).casefold()], force_next),
            weekday.span(),
        )
    return last


def _month(name: str) -> int:
    return _MONTHS[name.translate(_APOS).casefold()]


def _calendar_date(today: date, day: int, month: int, year_text: str | None) -> date:
    year = int(year_text) if year_text else today.year
    parsed = date(year, month, day)
    if year_text is None and parsed < today:
        parsed = date(year + 1, month, day)
    return parsed


def _dotted_calendar_date(today: date, day: int, month: int, year_text: str) -> date | None:
    """DD.MM.YY / DD.MM.YYYY. Two-digit years are 2000+YY and roll forward if past."""
    if not 1 <= month <= 12:
        return None
    try:
        year = 2000 + int(year_text) if len(year_text) == 2 else int(year_text)
        parsed = date(year, month, day)
    except ValueError:
        return None
    if len(year_text) == 2 and parsed < today:
        try:
            parsed = date(year + 1, month, day)
        except ValueError:
            return None
    return parsed


def _safe_calendar_date(
    today: date, day: int, month: int, year_text: str | None
) -> date | None:
    try:
        return _calendar_date(today, day, month, year_text)
    except ValueError:
        return None


def _week_end(today: date) -> date:
    start = today - timedelta(days=today.weekday())
    return start + timedelta(days=6)


def _month_end(today: date) -> date:
    if today.month == 12:
        return date(today.year + 1, 1, 1) - timedelta(days=1)
    return date(today.year, today.month + 1, 1) - timedelta(days=1)


def _next_month_start(today: date) -> date:
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


def _next_month_end(today: date) -> date:
    start = _next_month_start(today)
    return _month_end(start)


def _next_week_start(today: date) -> date:
    return today - timedelta(days=today.weekday()) + timedelta(days=7)


def _next_week_end(today: date) -> date:
    return _next_week_start(today) + timedelta(days=6)


def _relative(token: str, today: date) -> date:
    folded = token.casefold()
    if folded in {"сьогодні", "today"}:
        return today
    if folded in {"завтра", "tomorrow"}:
        return today + timedelta(days=1)
    return today + timedelta(days=2)


def _next_weekday(today: date, weekday: int, force_next: bool) -> date:
    ahead = (weekday - today.weekday()) % 7
    if ahead == 0:
        ahead = 7
    elif force_next and ahead < 7:
        pass
    return today + timedelta(days=ahead)
