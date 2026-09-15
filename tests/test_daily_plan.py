from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from chief_of_staff.models.plan import PlanCandidate, PlanConstraints
from chief_of_staff.models.task import Importance, Urgency
from chief_of_staff.services.daily_plan import BUFFER_RATIO, build_daily_plan, effort_minutes, rank_category
from chief_of_staff.services.plan_format import NOT_ENOUGH_TASKS, format_plan_message


TODAY = date(2026, 9, 2)


def _task(
    *,
    title: str,
    deadline: date,
    minutes: int,
    importance: Importance = Importance.MEDIUM,
    urgency: Urgency = Urgency.NOT_URGENT,
    project: str = "Unity Center",
    people: tuple[str, ...] = (),
    status: str = "inbox",
    created_at: datetime | None = None,
    task_id: UUID | None = None,
) -> PlanCandidate:
    return PlanCandidate(
        id=task_id or uuid4(),
        title=title,
        project=project,
        people=people,
        deadline=deadline,
        importance=importance,
        urgency=urgency,
        estimated_minutes=minutes,
        status=status,
        created_at=created_at,
    )


def test_overdue_ranked_before_due_today() -> None:
    overdue = _task(title="overdue", deadline=date(2026, 8, 30), minutes=20)
    due = _task(title="today", deadline=TODAY, minutes=20)
    assert rank_category(overdue, TODAY) < rank_category(due, TODAY)
    plan = build_daily_plan([due, overdue], 60, TODAY)
    assert plan.selected[0].title == "overdue"


def test_due_today_before_later() -> None:
    due = _task(title="today", deadline=TODAY, minutes=20)
    later = _task(title="later", deadline=date(2026, 9, 10), minutes=20)
    plan = build_daily_plan([later, due], 60, TODAY)
    assert plan.selected[0].title == "today"


def test_deadline_buckets() -> None:
    overdue = _task(title="overdue", deadline=date(2026, 8, 30), minutes=20)
    due = _task(title="today", deadline=TODAY, minutes=20)
    tomorrow = _task(title="tomorrow", deadline=date(2026, 9, 3), minutes=20)
    soon = _task(title="soon", deadline=date(2026, 9, 5), minutes=20)
    later = _task(title="later", deadline=date(2026, 9, 20), minutes=20)
    assert rank_category(overdue, TODAY) == 0
    assert rank_category(due, TODAY) == 1
    assert rank_category(tomorrow, TODAY) == 2
    assert rank_category(soon, TODAY) == 3
    assert rank_category(later, TODAY) == 4
    plan = build_daily_plan([later, soon, tomorrow, due, overdue], 200, TODAY)
    assert [task.title for task in plan.selected] == [
        "overdue",
        "today",
        "tomorrow",
        "soon",
        "later",
    ]


def test_legacy_high_urgent_gets_no_special_priority() -> None:
    later = datetime(2026, 8, 20, tzinfo=timezone.utc)
    earlier = datetime(2026, 8, 10, tzinfo=timezone.utc)
    hot = _task(
        title="high-urgent",
        deadline=date(2026, 8, 30),
        minutes=20,
        importance=Importance.HIGH,
        urgency=Urgency.URGENT,
        created_at=later,
    )
    cold = _task(
        title="low-not-urgent",
        deadline=date(2026, 8, 30),
        minutes=20,
        importance=Importance.LOW,
        urgency=Urgency.NOT_URGENT,
        created_at=earlier,
    )
    assert rank_category(hot, TODAY) == rank_category(cold, TODAY)
    plan = build_daily_plan([hot, cold], 60, TODAY)
    assert [task.title for task in plan.selected] == ["low-not-urgent", "high-urgent"]


def test_legacy_low_not_urgent_gets_no_penalty() -> None:
    hot = _task(
        title="high",
        deadline=TODAY,
        minutes=20,
        importance=Importance.HIGH,
        urgency=Urgency.URGENT,
    )
    cold = _task(
        title="low",
        deadline=TODAY,
        minutes=20,
        importance=Importance.LOW,
        urgency=Urgency.NOT_URGENT,
    )
    assert rank_category(hot, TODAY) == rank_category(cold, TODAY) == 1


def test_status_today_before_inbox_same_deadline() -> None:
    inbox = _task(title="inbox", deadline=TODAY, minutes=20, status="inbox")
    today = _task(title="today-status", deadline=TODAY, minutes=20, status="today")
    plan = build_daily_plan([inbox, today], 60, TODAY)
    assert plan.selected[0].title == "today-status"


def test_earlier_deadline_wins_inside_soon_bucket() -> None:
    later_soon = _task(title="in-3-days", deadline=date(2026, 9, 5), minutes=20)
    sooner = _task(title="in-2-days", deadline=date(2026, 9, 4), minutes=20)
    plan = build_daily_plan([later_soon, sooner], 60, TODAY)
    assert plan.selected[0].title == "in-2-days"


def test_plan_stays_within_available_minutes() -> None:
    tasks = [
        _task(title="a", deadline=date(2026, 8, 30), minutes=90, importance=Importance.HIGH),
        _task(title="b", deadline=TODAY, minutes=90, importance=Importance.HIGH),
        _task(title="c", deadline=date(2026, 9, 3), minutes=90, importance=Importance.HIGH),
    ]
    plan = build_daily_plan(tasks, 180, TODAY)
    used = sum(effort_minutes(t) for t in plan.selected)
    assert used <= 180
    assert used <= 180 - round(180 * BUFFER_RATIO) or len(plan.selected) == 1


def test_safety_buffer_is_left() -> None:
    available = 210
    tasks = [
        _task(title="big", deadline=date(2026, 8, 30), minutes=120, importance=Importance.HIGH, urgency=Urgency.URGENT),
        _task(title="mid", deadline=TODAY, minutes=60, importance=Importance.HIGH),
        _task(title="small", deadline=date(2026, 9, 3), minutes=30, importance=Importance.HIGH),
    ]
    plan = build_daily_plan(tasks, available, TODAY)
    used = sum(effort_minutes(t) for t in plan.selected)
    assert used <= available
    assert plan.reserve_minutes >= round(available * BUFFER_RATIO) - 5
    assert used == 180
    assert plan.reserve_minutes == round(available * BUFFER_RATIO)
    assert plan.unplanned_minutes == available - used - plan.reserve_minutes
    assert {t.title for t in plan.selected} == {"big", "mid"}


def test_does_not_fill_with_tiny_low_value_tasks() -> None:
    important = _task(
        title="important",
        deadline=date(2026, 8, 30),
        minutes=90,
        importance=Importance.HIGH,
        urgency=Urgency.URGENT,
    )
    tinies = [
        _task(
            title=f"tiny-{i}",
            deadline=date(2026, 9, 20),
            minutes=5,
            importance=Importance.LOW,
        )
        for i in range(20)
    ]
    plan = build_daily_plan([important, *tinies], 180, TODAY)
    assert plan.selected == (important,)


def test_oversized_critical_is_overflow() -> None:
    huge = _task(
        title="Rewrite the budget model",
        deadline=TODAY,
        minutes=240,
        importance=Importance.HIGH,
        urgency=Urgency.URGENT,
    )
    small = _task(title="Call Taras", deadline=TODAY, minutes=20, importance=Importance.HIGH)
    plan = build_daily_plan([huge, small], 90, TODAY)
    assert huge in plan.overflow
    assert huge not in plan.selected
    assert small in plan.selected
    used = sum(effort_minutes(t) for t in plan.selected)
    assert used <= 90


def test_exclude_project_constraint() -> None:
    unity = _task(title="Unity work", deadline=TODAY, minutes=20, project="Unity Center")
    other = _task(title="Other work", deadline=TODAY, minutes=20, project="Alpha")
    plan = build_daily_plan(
        [unity, other],
        120,
        TODAY,
        PlanConstraints(exclude_projects=("Unity Center",)),
    )
    assert [t.title for t in plan.selected] == ["Other work"]


def test_sparse_plan_keeps_configured_reserve_not_leftover_time() -> None:
    task = _task(
        title="Call Taras",
        deadline=TODAY,
        minutes=5,
        importance=Importance.HIGH,
        urgency=Urgency.URGENT,
    )
    plan = build_daily_plan([task], 120, TODAY)
    planned = sum(effort_minutes(t) for t in plan.selected)
    assert planned == 5
    assert plan.reserve_minutes == round(120 * BUFFER_RATIO)
    assert plan.reserve_minutes == 14
    assert plan.unplanned_minutes == 101
    assert plan.unplanned_minutes == 120 - planned - plan.reserve_minutes
    text = format_plan_message(plan, TODAY)
    assert "Доступно: 2 год" in text
    assert "Заплановано: 5 хв" in text
    assert "Резерв: 14 хв" in text
    assert "Незаплановано: 1 год 41 хв" in text
    assert NOT_ENOUGH_TASKS in text
    assert "High" not in text
    assert "Urgent" not in text
    assert " · " not in text


def test_nearly_full_plan_does_not_warn_about_unplanned() -> None:
    tasks = [
        _task(
            title="a",
            deadline=date(2026, 8, 30),
            minutes=50,
            importance=Importance.HIGH,
            urgency=Urgency.URGENT,
        ),
        _task(title="b", deadline=TODAY, minutes=50, importance=Importance.HIGH),
    ]
    plan = build_daily_plan(tasks, 120, TODAY)
    planned = sum(effort_minutes(t) for t in plan.selected)
    assert planned == 100
    assert plan.reserve_minutes == 14
    assert plan.unplanned_minutes == 6
    assert NOT_ENOUGH_TASKS not in format_plan_message(plan, TODAY)


def test_reserve_stays_twelve_percent_regardless_of_selection_size() -> None:
    expected = round(120 * BUFFER_RATIO)
    for minutes in (5, 40, 90):
        task = _task(
            title=f"work-{minutes}",
            deadline=TODAY,
            minutes=minutes,
            importance=Importance.HIGH,
        )
        plan = build_daily_plan([task], 120, TODAY)
        assert plan.reserve_minutes == expected
        assert plan.unplanned_minutes == 120 - minutes - expected


def test_unplanned_minutes_never_negative() -> None:
    huge = _task(
        title="All-day critical",
        deadline=date(2026, 8, 30),
        minutes=120,
        importance=Importance.HIGH,
        urgency=Urgency.URGENT,
    )
    plan = build_daily_plan([huge], 120, TODAY)
    planned = sum(effort_minutes(t) for t in plan.selected)
    assert plan.unplanned_minutes >= 0
    assert plan.unplanned_minutes == max(120 - planned - plan.reserve_minutes, 0)
    assert plan.reserve_minutes == round(120 * BUFFER_RATIO)
