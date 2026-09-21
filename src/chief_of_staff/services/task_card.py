"""Confirmation card copy (presentation-agnostic text)."""

from chief_of_staff.models.task import TaskDraft

READY_PROMPT = "Готово створити цю задачу? Так / Редагувати / Скасувати"
CREATED_HEADER = "✅ Задачу створено"
SAVE_FAILED = "Не вдалося зберегти задачу. Натисни Так, щоб повторити."
EDIT_PROMPT = "Що змінити?"
CANCELLED = "Скасовано."
NO_PENDING_TASK = "Немає задачі, яка чекає підтвердження."


def format_task_summary(draft: TaskDraft) -> str:
    people = ", ".join(draft.people) if draft.people else "—"
    deadline = draft.deadline.isoformat() if draft.deadline else "—"
    if draft.estimated_minutes is not None:
        minutes = f"{draft.estimated_minutes} хв"
    else:
        minutes = "—"
    return (
        "📋 Задача\n"
        f"Проєкт: {draft.project or '—'}\n"
        f"Задача: {draft.task_title or '—'}\n"
        f"Люди: {people}\n"
        f"Дедлайн: {deadline}\n"
        f"Оцінка часу: {minutes}\n"
        f"Результат: {draft.desired_outcome or '—'}"
    )


def format_confirmation_card(draft: TaskDraft) -> str:
    return f"{format_task_summary(draft)}\n\n{READY_PROMPT}"


def format_created_message(draft: TaskDraft) -> str:
    return f"{CREATED_HEADER}\n\n{format_task_summary(draft)}"
