"""Which deadline reminders are due right now (Europe/Kyiv). Pure functions."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from chief_of_staff.models.reminder import ReminderType
from chief_of_staff.services.clock import KYIV

_INACTIVE = frozenset({"done", "cancelled"})
_UTC = timezone.utc


@dataclass(frozen=True)
class ReminderSpec:
    reminder_type: ReminderType
    occurrence_date: date
    scheduled_for: datetime  # UTC


def to_kyiv(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(KYIV)


def to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(_UTC)


def at_kyiv(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KYIV)


def end_of_kyiv_day(day: date) -> datetime:
    return at_kyiv(day + timedelta(days=1), 0, 0)


def planned_reminders(
    *,
    deadline: date,
    status: str,
    created_at: datetime,
    now: datetime,
) -> list[ReminderSpec]:
    """Return reminder slots that should fire at `now` (not future, not stale)."""
    if status in _INACTIVE:
        return []
    now_k = to_kyiv(now)
    created = to_kyiv(created_at)
    specs: list[ReminderSpec] = []
    day_before = deadline - timedelta(days=1)
    spec = _in_window(
        ReminderType.DAY_BEFORE,
        occurrence_date=day_before,
        window_start=at_kyiv(day_before, 18, 0),
        window_end=at_kyiv(deadline, 9, 0),
        created=created,
        now=now_k,
    )
    if spec:
        specs.append(spec)
    spec = _in_window(
        ReminderType.DUE_TODAY,
        occurrence_date=deadline,
        window_start=at_kyiv(deadline, 9, 0),
        window_end=end_of_kyiv_day(deadline),
        created=created,
        now=now_k,
    )
    if spec:
        specs.append(spec)
    today = now_k.date()
    if today > deadline:
        spec = _in_window(
            ReminderType.OVERDUE,
            occurrence_date=today,
            window_start=at_kyiv(today, 9, 0),
            window_end=end_of_kyiv_day(today),
            created=created,
            now=now_k,
        )
        if spec:
            specs.append(spec)
    return specs


def _in_window(
    reminder_type: ReminderType,
    *,
    occurrence_date: date,
    window_start: datetime,
    window_end: datetime,
    created: datetime,
    now: datetime,
) -> ReminderSpec | None:
    if created >= window_end or now >= window_end or now < window_start:
        return None
    scheduled = window_start if created <= window_start else now
    return ReminderSpec(
        reminder_type=reminder_type,
        occurrence_date=occurrence_date,
        scheduled_for=to_utc(scheduled),
    )
