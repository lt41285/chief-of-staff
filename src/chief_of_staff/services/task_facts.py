"""Authoritative task rows carried in conversation. Never reconstructed from prose."""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from chief_of_staff.models.plan import PlanCandidate


@dataclass(frozen=True)
class ListedTaskFact:
    id: UUID
    title: str
    project: str
    project_id: UUID | None
    people: tuple[str, ...]
    deadline: date
    estimated_minutes: int
    status: str
    waiting_for: str | None = None


def facts_from_tasks(tasks: list[PlanCandidate]) -> tuple[ListedTaskFact, ...]:
    return tuple(
        ListedTaskFact(
            id=task.id,
            title=task.title,
            project=task.project,
            project_id=task.project_id,
            people=task.people,
            deadline=task.deadline,
            estimated_minutes=task.estimated_minutes,
            status=task.status,
            waiting_for=task.waiting_for,
        )
        for task in tasks
    )


def facts_as_prompt(facts: tuple[ListedTaskFact, ...]) -> str:
    if not facts:
        return "listed_tasks: (none)"
    lines = ["listed_tasks (Python, authoritative):"]
    for index, fact in enumerate(facts, start=1):
        lines.append(
            f"{index}. id={fact.id} | {fact.title} | {fact.project} | "
            f"deadline={fact.deadline.isoformat()} | est={fact.estimated_minutes} | "
            f"status={fact.status} | people={list(fact.people)}"
        )
    return "\n".join(lines)
