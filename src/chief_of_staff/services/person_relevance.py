"""Strict person–task eligibility. Weak similarity never creates a link."""

from collections.abc import Sequence

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.names import normalize_entity_name
from chief_of_staff.services.person_match import people_names_match


def task_related_to_person(task: PlanCandidate, query: str) -> bool:
    """True only with structured links or an explicit name in title/outcome."""
    cleaned = " ".join(query.split()).strip()
    if not cleaned:
        return False
    if structured_related(task, cleaned):
        return True
    return explicit_name_in_text(task, cleaned)


def filter_related(tasks: Sequence[PlanCandidate], query: str) -> list[PlanCandidate]:
    return [task for task in tasks if task_related_to_person(task, query)]


def structured_related(task: PlanCandidate, canonical: str) -> bool:
    if any(_same_person_record(canonical, name) for name in task.people):
        return True
    if task.waiting_for and _same_person_record(canonical, task.waiting_for):
        return True
    return False


def explicit_name_in_text(task: PlanCandidate, query: str) -> bool:
    hay = _text_tokens(f"{task.title} {task.desired_outcome}")
    needed = [token for token in normalize_entity_name(query).split() if len(token) >= 3]
    if not needed:
        return False
    return all(any(_same_token(token, item) for item in hay) for token in needed)


def _same_person_record(query: str, stored: str) -> bool:
    q = normalize_entity_name(query).split()
    s = normalize_entity_name(stored).split()
    if not q or not s:
        return False
    if q == s:
        return True
    if len(q) == len(s) and all(_same_token(left, right) for left, right in zip(q, s)):
        return True
    if people_names_match(query, stored) and len(q) == len(s):
        return True
    return False


def _text_tokens(value: str) -> list[str]:
    return [token for token in normalize_entity_name(value).split() if token]


def _same_token(left: str, right: str) -> bool:
    if left == right:
        return True
    size = 4 if min(len(left), len(right)) >= 5 else 3
    if min(len(left), len(right)) >= size and (
        left.startswith(right[:size]) or right.startswith(left[:size])
    ):
        return True
    return False
