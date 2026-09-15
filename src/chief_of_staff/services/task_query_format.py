"""Read-only task list copy."""

from collections.abc import Sequence
from datetime import date, datetime

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.clock import KYIV
from chief_of_staff.services.person_match import PersonHit
from chief_of_staff.services.plan_format import format_duration
from chief_of_staff.services.reminder_messages import format_uk_date

MAX_LISTED_TASKS = 20
EMPTY_ALL = "Немає відкритих задач."
ASK_PROJECTS = "Покажи всі проєкти?"


def format_unknown_project(name: str) -> str:
    return f"Не знайшов проєкт «{name}». {ASK_PROJECTS}"


def format_unknown_person(name: str) -> str:
    return f"Не знайшов людину «{name}» серед людей у твоїх задачах."


def format_person_mention_list(
    name: str,
    tasks: Sequence[PlanCandidate],
    *,
    today: date | None = None,
) -> str:
    if not tasks:
        return format_unknown_person(name)
    count = len(tasks)
    noun = "відкриту задачу" if count == 1 else "відкриті задачі"
    header = (
        f"Не бачу окремого запису {name} в people, але знайшов "
        f"{count} {noun}, де згадується це ім'я."
    )
    body = format_person_task_list(name, tasks, empty_query=name, today=today)
    if body.startswith("👤"):
        return f"{header}\n\n{body}"
    return f"{header}\n\n{body}"


def format_empty_person_tasks(query: str) -> str:
    return f"По {query} немає відкритих задач."


def format_person_picker(query: str, people: Sequence[PersonHit]) -> str:
    lines = [
        f"Уточни, кого саме маєш на увазі під «{query}»:",
        "",
    ]
    for index, person in enumerate(people, start=1):
        lines.append(f"{index}. {person.name}")
    return "\n".join(lines).rstrip()


def format_person_task_list(
    canonical: str,
    tasks: Sequence[PlanCandidate],
    *,
    empty_query: str | None = None,
    today: date | None = None,
) -> str:
    if not tasks:
        return format_empty_person_tasks(empty_query or canonical)
    day = today or _today()
    lines = [f"👤 {canonical}", ""]
    shown = 0
    remaining = MAX_LISTED_TASKS
    groups: dict[str, list[PlanCandidate]] = {}
    for task in tasks:
        groups.setdefault(task.project, []).append(task)
    for name, group in sorted(groups.items(), key=lambda item: item[0].casefold()):
        if remaining <= 0:
            break
        lines.append(f"📁 {name}")
        for task in group:
            if remaining <= 0:
                break
            shown += 1
            remaining -= 1
            lines.extend(
                format_task_list_block(shown, task, today=day, include_people=False)
            )
            lines.append("")
    if len(tasks) > shown:
        lines.append(f"Показано {shown} із {len(tasks)}.")
    return "\n".join(lines).rstrip()


def format_empty_project_tasks(canonical: str) -> str:
    return f"У проєкті {canonical} немає відкритих задач."


def format_project_task_list(
    canonical: str,
    tasks: Sequence[PlanCandidate],
    *,
    total: int | None = None,
    today: date | None = None,
) -> str:
    if not tasks:
        return format_empty_project_tasks(canonical)
    day = today or _today()
    lines = [f"📁 {canonical}", ""]
    shown = list(tasks[:MAX_LISTED_TASKS])
    for index, task in enumerate(shown, start=1):
        lines.extend(format_task_list_block(index, task, today=day))
        lines.append("")
    count = total if total is not None else len(tasks)
    if count > len(shown):
        lines.append(f"Показано {len(shown)} із {count}.")
    return "\n".join(lines).rstrip()


def format_all_tasks_grouped(
    groups: Sequence[tuple[str, Sequence[PlanCandidate]]],
    *,
    total: int,
    today: date | None = None,
) -> str:
    if total <= 0:
        return EMPTY_ALL
    day = today or _today()
    lines: list[str] = []
    shown = 0
    remaining = MAX_LISTED_TASKS
    for name, tasks in groups:
        if remaining <= 0:
            break
        lines.append(f"📁 {name} — {len(tasks)}")
        lines.append("")
        for task in tasks:
            if remaining <= 0:
                break
            shown += 1
            remaining -= 1
            lines.extend(format_task_list_block(shown, task, today=day))
            lines.append("")
    if total > shown:
        lines.append(f"Показано {shown} із {total}.")
    return "\n".join(lines).rstrip()


def format_task_list_block(
    index: int,
    task: PlanCandidate,
    *,
    today: date | None = None,
    include_people: bool = True,
) -> list[str]:
    day = today or _today()
    lines = [
        f"{index}. {task.title}",
        f"   📅 {_deadline_label(task.deadline, day)}",
        f"   ⏱ {_time_line(task)}",
    ]
    if include_people:
        lines.append(f"   👤 Люди: {_people_line(task.people)}")
    lines.extend(
        [
            f"   🎯 Результат: {_outcome_line(task.desired_outcome)}",
            f"   📌 Статус: {task.status}",
        ]
    )
    if task.status == "waiting":
        who = task.waiting_for.strip() if task.waiting_for and task.waiting_for.strip() else "—"
        lines.append(f"   ⏳ Чекаю від: {who}")
    return lines


def _deadline_label(deadline: date, today: date) -> str:
    if deadline < today:
        return f"{format_uk_date(deadline)} · ⚠️ Прострочено"
    if deadline == today:
        return "Сьогодні"
    return format_uk_date(deadline)


def _time_line(task: PlanCandidate) -> str:
    estimate = f"Оцінка: {format_duration(task.estimated_minutes)}"
    if task.actual_minutes is None:
        return estimate
    return f"{estimate} · Факт: {format_duration(task.actual_minutes)}"


def _people_line(people: tuple[str, ...]) -> str:
    names = [name.strip() for name in people if name.strip()]
    return ", ".join(names) if names else "—"


def _outcome_line(value: str) -> str:
    cleaned = " ".join(value.split()).strip() if value else ""
    return cleaned or "—"


def _today() -> date:
    return datetime.now(KYIV).date()
