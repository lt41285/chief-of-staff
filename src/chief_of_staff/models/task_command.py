"""Structured task-management commands. Execution stays in Python."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TaskIntentKind(StrEnum):
    LIST_TASKS = "list_tasks"
    LIST_PROJECT_TASKS = "list_project_tasks"
    LIST_ALL_TASKS = "list_all_tasks"
    PEOPLE_TASKS_QUERY = "people_tasks_query"
    COMPLETE_TASK = "complete_task"
    CANCEL_TASK = "cancel_task"
    POSTPONE_TASK = "postpone_task"
    WAITING_TASK = "waiting_task"
    RESUME_TASK = "resume_task"
    UPDATE_TASK = "update_task"
    NORMAL_TASK_INPUT = "normal_task_input"


QUERY_INTENT_KINDS = frozenset(
    {
        TaskIntentKind.LIST_TASKS,
        TaskIntentKind.LIST_PROJECT_TASKS,
        TaskIntentKind.LIST_ALL_TASKS,
        TaskIntentKind.PEOPLE_TASKS_QUERY,
    }
)


class TaskIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: TaskIntentKind = Field(description="User command type")
    task_query: str | None = Field(
        default=None,
        description="Natural-language reference to an existing task",
    )
    project_query: str | None = Field(
        default=None,
        description="Project name or alias when listing tasks in one project",
    )
    status_filter: str | None = Field(
        default=None,
        description="Optional status filter: done, waiting, or null for open tasks",
    )
    person_query: str | None = Field(
        default=None,
        description="Person name when listing tasks related to someone",
    )
    new_deadline: str | None = Field(
        default=None,
        description="New deadline as YYYY-MM-DD when postponing a task",
    )
    waiting_for: str | None = Field(
        default=None,
        description="Person we are waiting on, or null",
    )
    task_index: int | None = Field(
        default=None,
        description="1-based index into the last listed tasks",
    )
    deadline_on: str | None = Field(
        default=None,
        description="Filter listed tasks to this YYYY-MM-DD deadline",
    )
    exclude_person: str | None = Field(
        default=None,
        description="Person name to exclude after a correction",
    )
    broader_search: bool = Field(
        default=False,
        description="Include title/outcome mentions in people search",
    )
