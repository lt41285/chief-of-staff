"""Domain models for task capture (no persistence)."""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Importance(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Urgency(StrEnum):
    URGENT = "urgent"
    NOT_URGENT = "not_urgent"


# Written to NOT NULL DB columns for new tasks. Not used in UX, ranking, or reminders.
DEFAULT_IMPORTANCE = Importance.MEDIUM
DEFAULT_URGENCY = Urgency.NOT_URGENT

OPEN_TASK_STATUSES: tuple[str, ...] = ("inbox", "next", "today", "waiting")
CLOSED_TASK_STATUSES: tuple[str, ...] = ("done", "cancelled")


class RequiredField(StrEnum):
    TASK_TITLE = "task_title"
    DESIRED_OUTCOME = "desired_outcome"
    PROJECT = "project"
    DEADLINE = "deadline"
    ESTIMATED_MINUTES = "estimated_minutes"


class TaskDraft(BaseModel):
    """Accumulated task fields. Missing values are None — never invented."""

    model_config = ConfigDict(extra="forbid")

    task_title: str | None = None
    project: str | None = None
    people: tuple[str, ...] = ()
    deadline: date | None = None
    importance: Importance | None = None
    urgency: Urgency | None = None
    estimated_minutes: int | None = None
    desired_outcome: str | None = None


class TaskExtractionSchema(BaseModel):
    """Structured output schema for the OpenAI Responses API.

    All fields are nullable so the model can omit anything not stated.
    """

    model_config = ConfigDict(extra="forbid")

    task_title: str | None = Field(
        default=None,
        description="Actionable task title as stated, or null if not stated",
    )
    project: str | None = Field(
        default=None,
        description="Project or workstream name, or null if not stated",
    )
    people: list[str] = Field(
        default_factory=list,
        description="Person names or tags mentioned; empty if none",
    )
    deadline: str | None = Field(
        default=None,
        description="Deadline as YYYY-MM-DD in Europe/Kyiv, or null if not stated",
    )
    estimated_minutes: int | None = Field(
        default=None,
        description="Effort estimate in minutes if stated; otherwise null",
    )
    desired_outcome: str | None = Field(
        default=None,
        description="What done looks like, or null if not stated",
    )
