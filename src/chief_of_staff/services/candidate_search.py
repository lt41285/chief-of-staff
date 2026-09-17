"""Flexible person/task candidate retrieval. No unique-partial auto-expand."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.names import normalize_entity_name
from chief_of_staff.services.person_match import PersonHit
from chief_of_staff.services.person_relevance import (
    explicit_name_in_text,
    structured_related,
)


@dataclass(frozen=True)
class PersonCandidate:
    name: str
    person_id: UUID | None
    match: str  # exact | token | mention


@dataclass(frozen=True)
class SearchBundle:
    query: str
    people: tuple[PersonCandidate, ...]
    tasks: tuple[PlanCandidate, ...]
    exact_person: str | None
    label: str
    status_filter: str | None = None


def search_person_tasks(
    query: str,
    people: Sequence[PersonHit],
    tasks: Sequence[PlanCandidate],
    *,
    exclude_names: Sequence[str] = (),
    broader: bool = False,
) -> SearchBundle:
    cleaned = " ".join(query.split()).strip(" .,!?:;«»\"'")
    excluded = {normalize_entity_name(name) for name in exclude_names if name.strip()}
    people = [person for person in people if normalize_entity_name(person.name) not in excluded]
    tasks = [
        task
        for task in tasks
        if not _task_only_excluded(task, excluded)
    ]
    if not cleaned:
        return SearchBundle(query=query, people=(), tasks=(), exact_person=None, label=query)

    exact = [person for person in people if _exact(cleaned, person.name)]
    token = [
        person
        for person in people
        if person not in exact and _token_match(cleaned, person.name)
    ]
    candidates: list[PersonCandidate] = [
        PersonCandidate(name=person.name, person_id=person.person_id, match="exact")
        for person in exact
    ]
    candidates.extend(
        PersonCandidate(name=person.name, person_id=person.person_id, match="token")
        for person in token
    )

    related: dict[UUID, PlanCandidate] = {}
    focus = exact or token
    for person in focus:
        for task in tasks:
            if structured_related(task, person.name) or explicit_name_in_text(task, person.name):
                related[task.id] = task
    if not exact:
        for task in tasks:
            if explicit_name_in_text(task, cleaned):
                related[task.id] = task
    if broader and not related:
        last = cleaned.split()[-1]
        if last:
            for task in tasks:
                if explicit_name_in_text(task, last):
                    related[task.id] = task
            for person in people:
                if _token_match(last, person.name):
                    for task in tasks:
                        if structured_related(task, person.name) or explicit_name_in_text(
                            task, person.name
                        ):
                            related[task.id] = task

    if exact and not broader:
        label = exact[0].name
        exact_name = exact[0].name
        if len(exact) > 1:
            label = cleaned
            exact_name = None
    else:
        label = cleaned
        exact_name = exact[0].name if len(exact) == 1 else None

    ordered = tuple(sorted(related.values(), key=lambda task: (task.project.casefold(), task.title.casefold())))
    return SearchBundle(
        query=cleaned,
        people=tuple(candidates),
        tasks=ordered,
        exact_person=exact_name,
        label=label,
    )


def _exact(query: str, name: str) -> bool:
    return normalize_entity_name(query) == normalize_entity_name(name)


def _token_match(query: str, name: str) -> bool:
    q_tokens = normalize_entity_name(query).split()
    n_tokens = normalize_entity_name(name).split()
    if not q_tokens or not n_tokens:
        return False
    if q_tokens == n_tokens:
        return True
    if len(q_tokens) == 1:
        return any(_close(q_tokens[0], token) for token in n_tokens)
    if len(n_tokens) == 1:
        return False
    return _close(q_tokens[-1], n_tokens[-1]) and _close(q_tokens[0], n_tokens[0])


def _close(left: str, right: str) -> bool:
    if left == right:
        return True
    size = 4 if min(len(left), len(right)) >= 5 else 3
    if min(len(left), len(right)) >= size and (
        left.startswith(right[:size]) or right.startswith(left[:size])
    ):
        return True
    return False


def _task_only_excluded(task: PlanCandidate, excluded: set[str]) -> bool:
    if not excluded:
        return False
    people = {normalize_entity_name(name) for name in task.people}
    waiting = normalize_entity_name(task.waiting_for) if task.waiting_for else ""
    if waiting in excluded:
        people.add(waiting)
    if not people:
        return False
    return people <= excluded
