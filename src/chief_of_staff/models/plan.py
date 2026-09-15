"""Daily planning domain types. Ranking stays in Python."""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from chief_of_staff.models.task import Importance, Urgency


@dataclass(frozen=True)
class PlanCandidate:
    id: UUID
    title: str
    project: str
    people: tuple[str, ...]
    deadline: date
    importance: Importance
    urgency: Urgency
    estimated_minutes: int
    status: str
    project_id: UUID | None = None
    desired_outcome: str = ""
    actual_minutes: int | None = None
    waiting_for: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class PlanConstraints:
    exclude_projects: tuple[str, ...] = ()
    exclude_people: tuple[str, ...] = ()
    extra_reserve_minutes: int | None = None
    prefer_short_tasks: bool = False


@dataclass(frozen=True)
class DailyPlan:
    available_minutes: int
    reserve_minutes: int
    unplanned_minutes: int
    selected: tuple[PlanCandidate, ...]
    overflow: tuple[PlanCandidate, ...]


class ReplanConstraintsSchema(BaseModel):
    """Structured replan preference. Task picking is not done by the model."""

    model_config = ConfigDict(extra="forbid")

    exclude_projects: list[str] = Field(
        default_factory=list,
        description="Project names to leave out of today's plan",
    )
    exclude_people: list[str] = Field(
        default_factory=list,
        description="People to avoid working with today",
    )
    extra_reserve_minutes: int | None = Field(
        default=None,
        description="Extra unused minutes to keep free, or null",
    )
    prefer_short_tasks: bool = Field(
        default=False,
        description="True if the user wants more short tasks",
    )
