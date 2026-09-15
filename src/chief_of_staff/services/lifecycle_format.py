"""Ukrainian copy for completing, postponing, waiting, and resuming tasks."""

from datetime import date

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.plan_format import format_duration
from chief_of_staff.services.reminder_messages import format_uk_date

ASK_COMPLETE = "✅ Позначити виконаним?"
ASK_POSTPONE = "📅 Змінити дедлайн?"
ASK_WAITING = "⏳ Перевести у Waiting?"
ASK_RESUME = "▶️ Повернути в роботу?"
ASK_ACTUAL = "⏱ Скільки часу фактично зайняла ця задача?"
ASK_WHICH = "Яку задачу позначити виконаною?"
ASK_WHICH_POSTPONE = "Яку задачу перенести?"
ASK_WHICH_WAITING = "Яку задачу перевести у waiting?"
ASK_WHICH_RESUME = "Яку задачу повернути в роботу?"
ASK_DEADLINE = "На коли перенести?"
COMPLETED = "✅ Позначено виконаним."
POSTPONED = "✅ Дедлайн змінено."
WAITING_SET = "✅ Задачу переведено у waiting."
RESUMED = "✅ Задачу повернуто в роботу."
CANCELLED = "Скасовано. Задачу не змінено."
ALREADY_DONE = "Ця задача вже виконана."
NOT_FOUND = "Не знайшов такої відкритої задачі."
NO_PENDING = "Немає задачі, яка чекає підтвердження."
UNSUPPORTED = "Ця дія поки не підтримується."
SEPARATE_ACTIONS = (
    "Це дві різні дії. Спочатку перенеси дедлайн, потім постав waiting — окремими повідомленнями."
)
ASK_WHICH_TIME = "Напиши час, наприклад «35 хв», або «пропустити»."
NEED_TASK_HINT = "Уточни, яку саме задачу маєш на увазі."
NOT_WAITING = "Ця задача не у waiting."
BTN_DONE = "✅ Виконано"
BTN_YES_DONE = "✅ Так, виконано"
BTN_NEW_TASK = "➕ Це нова задача"
BTN_POSTPONE = "✅ Перенести"
BTN_WAITING = "✅ Waiting"
BTN_RESUME = "✅ В роботу"


def format_complete_preview(task: PlanCandidate) -> str:
    return (
        f"{ASK_COMPLETE}\n\n"
        f"📌 {task.title}\n"
        f"📁 {task.project}\n"
        f"📅 Дедлайн: {format_uk_date(task.deadline)}\n"
        f"⏱ Оцінка: {format_duration(task.estimated_minutes)}"
    )


def format_statement_complete_preview(task: PlanCandidate) -> str:
    return (
        "Схоже, ти виконав цю задачу:\n\n"
        f"✅ {task.title}\n\n"
        "Позначити виконаною?"
    )


def format_postpone_preview(task: PlanCandidate, new_deadline: date) -> str:
    return (
        f"{ASK_POSTPONE}\n\n"
        f"📌 {task.title}\n"
        f"📁 {task.project}\n\n"
        f"Було: {format_uk_date(task.deadline)}\n"
        f"Буде: {format_uk_date(new_deadline)}"
    )


def format_waiting_preview(task: PlanCandidate, waiting_for: str | None) -> str:
    who = waiting_for.strip() if waiting_for and waiting_for.strip() else "—"
    return (
        f"{ASK_WAITING}\n\n"
        f"📌 {task.title}\n"
        f"📁 {task.project}\n"
        f"👤 Чекаємо від: {who}"
    )


def format_resume_preview(task: PlanCandidate) -> str:
    return f"{ASK_RESUME}\n\n📌 {task.title}\n📁 {task.project}"


def format_choice_list(tasks: tuple[PlanCandidate, ...], header: str = ASK_WHICH) -> str:
    lines = [header, ""]
    for index, task in enumerate(tasks, start=1):
        lines.append(f"{index}. {task.title}")
        lines.append(f"   📁 {task.project}")
    return "\n".join(lines)


def format_completed_message(actual_minutes: int | None) -> str:
    if actual_minutes is None:
        return COMPLETED
    return f"{COMPLETED}\n⏱ Фактично: {format_duration(actual_minutes)}"


def format_postponed_message(new_deadline: date) -> str:
    return f"{POSTPONED}\n📅 Новий дедлайн: {format_uk_date(new_deadline)}"
