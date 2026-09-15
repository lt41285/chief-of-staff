"""Ukrainian copy for daily planning."""

from datetime import date, timedelta

from chief_of_staff.models.plan import DailyPlan, PlanCandidate
from chief_of_staff.services.daily_plan import effort_minutes, rank_category
from chief_of_staff.services.reminder_messages import format_uk_date

ASK_FREE_TIME = "Скільки в тебе сьогодні є вільного робочого часу без зустрічей?"
ASK_REPLAN = "Що змінити?"
REPLAN_FOOTER = "Якщо хочеш, можу перебудувати план."
NO_OPEN_TASKS = "Немає відкритих завдань для планування на сьогодні."
NO_ACTIVE_PLAN = "Немає активного плану на сьогодні."
PLAN_ACCEPTED = "✅ План прийнято. Обрані завдання позначені як робота на сьогодні."
PLAN_CANCELLED = "❌ Планування скасовано."
EMPTY_FIT = "У доступний час не вміщується жодне відкрите завдання, крім критичних, які зазначені окремо."
OVERFLOW_HEADER = "⚠️ Критичне завдання не вміщується у доступний час"
NOT_ENOUGH_TASKS = "⚠️ Недостатньо активних задач, щоб заповнити доступний час."

# Slack from packing/rounding: do not warn when leftover is only a few minutes.
UNPLANNED_WARNING_MINUTES = 15


def format_duration(minutes: int) -> str:
    hours, mins = divmod(max(minutes, 0), 60)
    if hours and mins:
        return f"{hours} год {mins} хв"
    if hours:
        return f"{hours} год"
    return f"{mins} хв"


def format_plan_message(plan: DailyPlan, today: date) -> str:
    planned = sum(effort_minutes(task) for task in plan.selected)
    lines = [
        "🗓 План на сьогодні",
        f"Доступно: {format_duration(plan.available_minutes)}",
        f"Заплановано: {format_duration(planned)}",
        f"Резерв: {format_duration(plan.reserve_minutes)}",
        f"Незаплановано: {format_duration(plan.unplanned_minutes)}",
        "",
    ]
    if plan.unplanned_minutes >= UNPLANNED_WARNING_MINUTES:
        lines.append(NOT_ENOUGH_TASKS)
        lines.append("")
    if plan.selected:
        for index, task in enumerate(plan.selected, start=1):
            lines.extend(_task_block(index, task, today))
            lines.append("")
    else:
        lines.append(EMPTY_FIT)
        lines.append("")
    for task in plan.overflow:
        lines.append(OVERFLOW_HEADER)
        lines.append(f"«{task.title}» потребує {format_duration(effort_minutes(task))}.")
        lines.append(f"📁 {task.project}")
        lines.append(f"📅 {_deadline_line(task.deadline, today)}")
        lines.append("")
    if not plan.selected and not plan.overflow:
        return NO_OPEN_TASKS
    lines.append(REPLAN_FOOTER)
    return "\n".join(lines).rstrip()


def _task_block(index: int, task: PlanCandidate, today: date) -> list[str]:
    emoji = _emoji(task, today)
    return [
        f"{index}. {emoji} {task.title}",
        f"   📁 {task.project}",
        f"   📅 {_deadline_line(task.deadline, today)}",
        f"   ⏱ {format_duration(effort_minutes(task))}",
    ]


def _deadline_line(deadline: date, today: date) -> str:
    if deadline < today:
        return f"Дедлайн: прострочено ({format_uk_date(deadline)})"
    if deadline == today:
        return "Дедлайн: сьогодні"
    if deadline == today + timedelta(days=1):
        return "Дедлайн: завтра"
    return f"Дедлайн: {format_uk_date(deadline)}"


def _emoji(task: PlanCandidate, today: date) -> str:
    category = rank_category(task, today)
    if category == 0:
        return "🔴"
    if category == 1:
        return "🟠"
    return "🟡"
