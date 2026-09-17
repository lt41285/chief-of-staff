"""Turn trusted search facts into a short Ukrainian reply. No invented data."""

from collections.abc import Sequence

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.candidate_search import PersonCandidate, SearchBundle
from chief_of_staff.services.reminder_messages import format_uk_date
from chief_of_staff.services.task_query_format import (
    format_empty_status_tasks,
    format_person_task_list,
    format_task_count_phrase,
)


def format_search_facts(bundle: SearchBundle) -> str:
    names = [person.name for person in bundle.people]
    status = bundle.status_filter
    if len(bundle.people) > 1 and _ambiguous_people(bundle.people):
        options = " чи ".join(f"«{name}»" for name in names[:4])
        return f"Ти маєш на увазі {options}?"
    if not bundle.tasks:
        if bundle.people:
            shown = ", ".join(f"«{name}»" for name in names)
            empty = format_empty_status_tasks("зараз", status)
            return f"Є {shown}, але {empty}"
        return (
            f"Не бачу {_status_tasks_word(status)} по «{bundle.label}». "
            "Можу пошукати ширше в назвах і результатах."
        )
    header = _header(bundle)
    body = format_person_task_list(
        bundle.label,
        bundle.tasks,
        empty_query=bundle.label,
        status_filter=status,
    )
    compact = _compact_tasks(bundle.tasks)
    if header:
        return f"{header}\n\n{compact}"
    return compact or body


def _status_tasks_word(status_filter: str | None) -> str:
    if status_filter == "done":
        return "виконаних задач"
    if status_filter == "waiting":
        return "задач у waiting"
    return "відкритих задач"


def _header(bundle: SearchBundle) -> str:
    count = len(bundle.tasks)
    noun = format_task_count_phrase(count, bundle.status_filter)
    if bundle.exact_person:
        if count == 1:
            return f"Ок, бачу {noun}, пов'язану з {bundle.exact_person}:"
        return f"Ок, бачу {noun}, пов'язані з {bundle.exact_person}:"
    if bundle.people:
        found = bundle.people[0].name
        return (
            f"Окремого запису «{bundle.query}» немає. "
            f"Але є людина «{found}» і {count} "
            f"{'задача' if count == 1 else 'задачі'} з цим ім'ям."
        )
    return f"Ок, бачу {noun}, де згадується «{bundle.label}»:"


def _compact_tasks(tasks: Sequence[PlanCandidate]) -> str:
    lines: list[str] = []
    for task in tasks[:20]:
        lines.append(
            f"— {task.title}, дедлайн {format_uk_date(task.deadline)}"
        )
    return "\n".join(lines)


def _ambiguous_people(people: Sequence[PersonCandidate]) -> bool:
    exact = [person for person in people if person.match == "exact"]
    if len(exact) > 1:
        return True
    if exact:
        return False
    tokens = [person for person in people if person.match == "token"]
    return len(tokens) > 1
