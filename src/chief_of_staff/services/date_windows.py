"""Europe/Kyiv calendar windows. No LLM."""

from datetime import date, timedelta

PeriodName = str


def resolve_period(period: str | None, today: date) -> tuple[date, date] | None:
    if not period:
        return None
    key = period.strip().casefold().replace(" ", "_")
    aliases = {
        "today": "today",
        "сьогодні": "today",
        "tomorrow": "tomorrow",
        "завтра": "tomorrow",
        "this_week": "this_week",
        "цей_тиждень": "this_week",
        "цього_тижня": "this_week",
        "next_week": "next_week",
        "наступний_тиждень": "next_week",
        "наступного_тижня": "next_week",
        "this_month": "this_month",
        "цього_місяця": "this_month",
    }
    kind = aliases.get(key, key)
    if kind == "today":
        return today, today
    if kind == "tomorrow":
        day = today + timedelta(days=1)
        return day, day
    if kind == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if kind == "next_week":
        start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        return start, start + timedelta(days=6)
    if kind == "this_month":
        start = today.replace(day=1)
        if today.month == 12:
            end = date(today.year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(today.year, today.month + 1, 1) - timedelta(days=1)
        return start, end
    return None
