"""Deterministic ranking and packing for /today."""

from datetime import date, datetime, timezone

from chief_of_staff.models.plan import DailyPlan, PlanCandidate, PlanConstraints
from chief_of_staff.services.names import normalize_project_name

BUFFER_RATIO = 0.12
_TINY_MINUTES = 15
_DEFAULT_ESTIMATE = 30

_CAT_OVERDUE = 0
_CAT_DUE_TODAY = 1
_CAT_TOMORROW = 2
_CAT_SOON = 3
_CAT_LATER = 4

_STATUS_ORDER = {"today": 0, "next": 1, "inbox": 2, "waiting": 3}
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def effort_minutes(task: PlanCandidate) -> int:
    if task.estimated_minutes and task.estimated_minutes > 0:
        return task.estimated_minutes
    return _DEFAULT_ESTIMATE


def rank_category(task: PlanCandidate, today: date) -> int:
    delta = (task.deadline - today).days
    if delta < 0:
        return _CAT_OVERDUE
    if delta == 0:
        return _CAT_DUE_TODAY
    if delta == 1:
        return _CAT_TOMORROW
    if delta <= 3:
        return _CAT_SOON
    return _CAT_LATER


def is_critical(task: PlanCandidate, today: date) -> bool:
    return rank_category(task, today) <= _CAT_DUE_TODAY


def target_reserve(available_minutes: int, extra_reserve_minutes: int | None) -> int:
    baseline = round(available_minutes * BUFFER_RATIO)
    extra = extra_reserve_minutes or 0
    reserve = max(baseline, extra)
    return min(reserve, max(available_minutes - 1, 0))


def apply_constraints(
    tasks: list[PlanCandidate],
    constraints: PlanConstraints,
) -> list[PlanCandidate]:
    kept: list[PlanCandidate] = []
    for task in tasks:
        if _excluded_project(task.project, constraints.exclude_projects):
            continue
        if any(_name_match(person, needle) for person in task.people for needle in constraints.exclude_people):
            continue
        kept.append(task)
    return kept


def build_daily_plan(
    tasks: list[PlanCandidate],
    available_minutes: int,
    today: date,
    constraints: PlanConstraints | None = None,
) -> DailyPlan:
    constraints = constraints or PlanConstraints()
    eligible = apply_constraints(tasks, constraints)
    reserve = target_reserve(available_minutes, constraints.extra_reserve_minutes)
    budget = max(available_minutes - reserve, 0)
    ordered = _sorted(eligible, today, prefer_short=constraints.prefer_short_tasks)

    overflow = tuple(
        task
        for task in ordered
        if is_critical(task, today) and effort_minutes(task) > available_minutes
    )
    overflow_ids = {task.id for task in overflow}

    selected: list[PlanCandidate] = []
    used = 0
    for task in ordered:
        if task.id in overflow_ids:
            continue
        cost = effort_minutes(task)
        if used + cost > budget:
            continue
        if not _should_add(task, today, selected, used, available_minutes, budget):
            continue
        selected.append(task)
        used += cost

    if not selected:
        for task in ordered:
            if task.id in overflow_ids:
                continue
            cost = effort_minutes(task)
            if cost <= available_minutes:
                selected.append(task)
                used = cost
                break

    planned = sum(effort_minutes(t) for t in selected)
    unplanned = max(available_minutes - planned - reserve, 0)
    return DailyPlan(
        available_minutes=available_minutes,
        reserve_minutes=reserve,
        unplanned_minutes=unplanned,
        selected=tuple(selected),
        overflow=overflow,
    )


def _sorted(tasks: list[PlanCandidate], today: date, *, prefer_short: bool) -> list[PlanCandidate]:
    return sorted(
        tasks,
        key=lambda task: (
            rank_category(task, today),
            task.deadline,
            _STATUS_ORDER.get(task.status, 9),
            effort_minutes(task) if prefer_short else 0,
            _created_sort_key(task),
            str(task.id),
        ),
    )


def _created_sort_key(task: PlanCandidate) -> datetime:
    created = task.created_at
    if created is None:
        return _EPOCH
    if created.tzinfo is None:
        return created.replace(tzinfo=timezone.utc)
    return created.astimezone(timezone.utc)


def _should_add(
    task: PlanCandidate,
    today: date,
    selected: list[PlanCandidate],
    used: int,
    available: int,
    budget: int,
) -> bool:
    category = rank_category(task, today)
    cost = effort_minutes(task)
    if category <= _CAT_DUE_TODAY:
        return True
    if not selected:
        return True
    if cost < _TINY_MINUTES and (budget - used) < available * 0.25:
        return False
    if category == _CAT_LATER and cost < _TINY_MINUTES:
        return False
    return True


def _excluded_project(project: str, needles: tuple[str, ...]) -> bool:
    return any(_name_match(project, needle) for needle in needles)


def _name_match(value: str, needle: str) -> bool:
    left = normalize_project_name(value)
    right = normalize_project_name(needle)
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)
