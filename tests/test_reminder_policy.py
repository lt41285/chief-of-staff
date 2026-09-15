from datetime import date, timezone

from chief_of_staff.models.reminder import ReminderType
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.reminder_messages import format_reminder_message, format_uk_date
from chief_of_staff.services.reminder_policy import at_kyiv, planned_reminders, to_utc


def _specs(**kwargs: object) -> set[ReminderType]:
    return {s.reminder_type for s in planned_reminders(**kwargs)}  # type: ignore[arg-type]


def test_all_tasks_get_day_before() -> None:
    created = at_kyiv(date(2026, 8, 28), 10, 0)
    now = at_kyiv(date(2026, 9, 1), 18, 30)
    types = _specs(
        deadline=date(2026, 9, 2),
        status="inbox",
        created_at=created,
        now=now,
    )
    assert types == {ReminderType.DAY_BEFORE}


def test_all_tasks_get_due_day_after_morning() -> None:
    created = at_kyiv(date(2026, 8, 28), 10, 0)
    now = at_kyiv(date(2026, 9, 2), 9, 10)
    types = _specs(
        deadline=date(2026, 9, 2),
        status="next",
        created_at=created,
        now=now,
    )
    assert types == {ReminderType.DUE_TODAY}


def test_low_priority_legacy_task_still_gets_day_before() -> None:
    created = at_kyiv(date(2026, 8, 28), 10, 0)
    evening_before = _specs(
        deadline=date(2026, 9, 2),
        status="inbox",
        created_at=created,
        now=at_kyiv(date(2026, 9, 1), 18, 30),
    )
    assert evening_before == {ReminderType.DAY_BEFORE}
    due_morning = _specs(
        deadline=date(2026, 9, 2),
        status="inbox",
        created_at=created,
        now=at_kyiv(date(2026, 9, 2), 9, 10),
    )
    assert due_morning == {ReminderType.DUE_TODAY}


def test_done_and_cancelled_are_ignored() -> None:
    kwargs = dict(
        deadline=date(2026, 9, 2),
        created_at=at_kyiv(date(2026, 8, 28), 10, 0),
        now=at_kyiv(date(2026, 9, 2), 9, 10),
    )
    assert _specs(status="done", **kwargs) == set()
    assert _specs(status="cancelled", **kwargs) == set()


def test_overdue_one_slot_per_calendar_day() -> None:
    created = at_kyiv(date(2026, 8, 20), 10, 0)
    day1 = planned_reminders(
        deadline=date(2026, 8, 30),
        status="inbox",
        created_at=created,
        now=at_kyiv(date(2026, 9, 1), 9, 15),
    )
    day2 = planned_reminders(
        deadline=date(2026, 8, 30),
        status="inbox",
        created_at=created,
        now=at_kyiv(date(2026, 9, 2), 9, 15),
    )
    assert [s.reminder_type for s in day1] == [ReminderType.OVERDUE]
    assert day1[0].occurrence_date == date(2026, 9, 1)
    assert day2[0].occurrence_date == date(2026, 9, 2)


def test_stale_day_before_not_sent_after_window() -> None:
    created = at_kyiv(date(2026, 9, 1), 19, 0)
    types = _specs(
        deadline=date(2026, 9, 2),
        status="inbox",
        created_at=created,
        now=at_kyiv(date(2026, 9, 2), 10, 0),
    )
    assert ReminderType.DAY_BEFORE not in types
    assert ReminderType.DUE_TODAY in types


def test_created_after_nine_on_deadline_day_still_gets_due_today_soon() -> None:
    created = at_kyiv(date(2026, 9, 2), 15, 0)
    specs = planned_reminders(
        deadline=date(2026, 9, 2),
        status="inbox",
        created_at=created,
        now=at_kyiv(date(2026, 9, 2), 15, 5),
    )
    assert len(specs) == 1
    assert specs[0].reminder_type is ReminderType.DUE_TODAY
    assert specs[0].scheduled_for <= to_utc(at_kyiv(date(2026, 9, 2), 15, 5))


def test_kyiv_offset_winter_and_dst() -> None:
    winter = at_kyiv(date(2026, 1, 15), 9, 0)
    assert winter.astimezone(timezone.utc).hour == 7  # UTC+2
    summer = at_kyiv(date(2026, 7, 15), 9, 0)
    assert summer.astimezone(timezone.utc).hour == 6  # UTC+3


def test_dst_spring_forward_keeps_wall_clock() -> None:
    before = at_kyiv(date(2026, 3, 29), 18, 0)
    after = at_kyiv(date(2026, 3, 30), 9, 0)
    assert before.tzinfo is KYIV
    assert after.hour == 9
    specs = planned_reminders(
        deadline=date(2026, 3, 30),
        status="inbox",
        created_at=at_kyiv(date(2026, 3, 20), 12, 0),
        now=at_kyiv(date(2026, 3, 29), 18, 5),
    )
    assert specs[0].reminder_type is ReminderType.DAY_BEFORE
    assert specs[0].scheduled_for == to_utc(before)


def test_ukrainian_copy() -> None:
    assert format_uk_date(date(2026, 9, 2)) == "2 вересня"
    text = format_reminder_message(
        reminder_type=ReminderType.DAY_BEFORE,
        title="Поговорити з Тарасом про бюджет",
        project="Unity Center",
        deadline=date(2026, 9, 2),
        estimated_minutes=20,
    )
    assert text.startswith("⏰ Нагадування")
    assert "📋 Поговорити з Тарасом про бюджет" in text
    assert "📁 Unity Center" in text
    assert "📅 Дедлайн: 2 вересня" in text
    assert "⏱ 20 хв" in text
    assert "High" not in text
    assert "Urgent" not in text
    overdue = format_reminder_message(
        reminder_type=ReminderType.OVERDUE,
        title="X",
        project="P",
        deadline=date(2026, 9, 2),
        estimated_minutes=20,
    )
    assert overdue.startswith("🚨 Прострочено")
    assert "Дедлайн був: 2 вересня" in overdue
    due = format_reminder_message(
        reminder_type=ReminderType.DUE_TODAY,
        title="X",
        project="P",
        deadline=date(2026, 9, 2),
        estimated_minutes=20,
    )
    assert due.startswith("⚠️ Дедлайн сьогодні")
