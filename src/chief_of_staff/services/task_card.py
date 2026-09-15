"""Confirmation card copy (presentation-agnostic text)."""

from chief_of_staff.models.task import TaskDraft

READY_PROMPT = "Ready to create this task? Yes / Edit / Cancel"
CREATED_HEADER = "✅ Task created"
SAVE_FAILED = "Could not save the task. Tap Yes to retry."
EDIT_PROMPT = "What should I change?"
CANCELLED = "Cancelled."
NO_PENDING_TASK = "No task is waiting for confirmation."


def format_task_summary(draft: TaskDraft) -> str:
    people = ", ".join(draft.people) if draft.people else "—"
    deadline = draft.deadline.isoformat() if draft.deadline else "—"
    minutes = f"{draft.estimated_minutes} min" if draft.estimated_minutes is not None else "—"
    return (
        "📋 Task\n"
        f"Project: {draft.project or '—'}\n"
        f"Task: {draft.task_title or '—'}\n"
        f"People: {people}\n"
        f"Deadline: {deadline}\n"
        f"Estimated time: {minutes}\n"
        f"Outcome: {draft.desired_outcome or '—'}"
    )


def format_confirmation_card(draft: TaskDraft) -> str:
    return f"{format_task_summary(draft)}\n\n{READY_PROMPT}"


def format_created_message(draft: TaskDraft) -> str:
    return f"{CREATED_HEADER}\n\n{format_task_summary(draft)}"
