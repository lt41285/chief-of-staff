"""Read-only text fallback for person/task relevance. No writes, no other users."""

from collections.abc import Sequence

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.person_match import text_mentions_person


def tasks_mentioning_person(
    tasks: Sequence[PlanCandidate],
    person_query: str,
) -> list[PlanCandidate]:
    found: dict[object, PlanCandidate] = {}
    for task in tasks:
        if _mentions(task, person_query):
            found[task.id] = task
    return list(found.values())


def _mentions(task: PlanCandidate, person_query: str) -> bool:
    blobs = (
        task.title,
        task.desired_outcome,
        task.project,
        " ".join(task.people),
        task.waiting_for or "",
    )
    hay = " ".join(part for part in blobs if part and part.strip())
    return text_mentions_person(person_query, hay)
