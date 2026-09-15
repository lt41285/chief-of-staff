"""Deterministic task arithmetic. The model must not estimate these."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from chief_of_staff.models.plan import PlanCandidate


@dataclass(frozen=True)
class EstimateSummary:
    total_minutes: int
    with_estimates: int
    missing: int
    count: int


def sum_estimated_minutes(tasks: Sequence[PlanCandidate]) -> EstimateSummary:
    present = [task.estimated_minutes for task in tasks if task.estimated_minutes and task.estimated_minutes > 0]
    return EstimateSummary(
        total_minutes=sum(present),
        with_estimates=len(present),
        missing=len(tasks) - len(present),
        count=len(tasks),
    )


def filter_overdue(tasks: Sequence[PlanCandidate], today: date) -> list[PlanCandidate]:
    return [task for task in tasks if task.deadline < today]


def filter_due_between(
    tasks: Sequence[PlanCandidate], start: date, end: date
) -> list[PlanCandidate]:
    return [task for task in tasks if start <= task.deadline <= end]


def fit_tasks_into_minutes(
    tasks: Sequence[PlanCandidate], budget: int
) -> tuple[list[PlanCandidate], int]:
    selected: list[PlanCandidate] = []
    used = 0
    ordered = sorted(
        tasks,
        key=lambda task: (task.deadline, task.estimated_minutes or 10_000, task.title),
    )
    for task in ordered:
        cost = task.estimated_minutes if task.estimated_minutes and task.estimated_minutes > 0 else None
        if cost is None:
            continue
        if used + cost <= budget:
            selected.append(task)
            used += cost
    return selected, used
