"""Central utterance interpretation. Execution stays in Python."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class QueryRelation(StrEnum):
    CONTINUE_QUERY = "continue_query"
    REFINE_QUERY = "refine_query"
    CORRECT_QUERY = "correct_query"
    BROADEN_QUERY = "broaden_query"
    NEW_QUERY = "new_query"
    NEW_INTENT = "new_intent"
    GENERAL_CHAT = "general_chat"


class RouterKind(StrEnum):
    CREATE_TASK = "create_task"
    PEOPLE_TASKS_QUERY = "people_tasks_query"
    LIST_PROJECT_TASKS = "list_project_tasks"
    LIST_ALL_TASKS = "list_all_tasks"
    COMPLETE_TASK = "complete_task"
    COMPLETE_STATEMENT = "complete_statement"
    POSTPONE_TASK = "postpone_task"
    WAITING_TASK = "waiting_task"
    RESUME_TASK = "resume_task"
    CREATE_PROJECT = "create_project"
    LIST_PROJECTS = "list_projects"
    ARCHIVE_PROJECT = "archive_project"
    LIST_ARCHIVED_PROJECTS = "list_archived_projects"
    RESTORE_PROJECT = "restore_project"
    DELETE_PROJECT_PERMANENTLY = "delete_project_permanently"
    PROVIDE_PENDING_VALUE = "provide_pending_value"
    CANCEL_PENDING = "cancel_pending"
    CONTINUE_PENDING = "continue_pending"
    RETRY_PREVIOUS = "retry_previous"
    CORRECT_ENTITY = "correct_entity"
    GENERAL_CHAT = "general_chat"
    INSPECT_LISTED = "inspect_listed"
    SUM_ESTIMATES = "sum_estimates"
    FIT_MINUTES = "fit_minutes"
    LIST_PERIOD = "list_period"
    REFINE_PREVIOUS = "refine_previous"
    CONFIRM_ENTITY_EQUIVALENCE = "confirm_entity_equivalence"
    UNCLEAR = "unclear"


class UtteranceInterpretation(BaseModel):
    """One structured model for intent + entities + optional new-task fields."""

    model_config = ConfigDict(extra="forbid")

    kind: RouterKind = Field(description="Primary user intent")
    inherit_context: bool = Field(
        default=False,
        description="True if this turn refines the previous query (pronouns, 'а тільки', 'друга')",
    )
    query_relation: QueryRelation | None = Field(
        default=None,
        description=(
            "How this utterance relates to the previous structured query: "
            "continue_query | refine_query | correct_query | broaden_query | "
            "new_query | new_intent | general_chat. "
            "Python carries filters only for continue/refine/correct."
        ),
    )
    clear_person_filter: bool = Field(
        default=False,
        description="Drop the previous person filter and rerun globally",
    )
    address_form: str | None = Field(
        default=None,
        description="informal (ти) | formal (ви) when the user sets a speaking preference",
    )
    person_query: str | None = Field(
        default=None,
        description="Person name as the user means it now; do not expand Хома to Тарас Хома",
    )
    project_query: str | None = None
    task_query: str | None = None
    task_index: int | None = Field(
        default=None,
        description="1-based index into the last listed tasks ('друга' → 2)",
    )
    status_filter: str | None = Field(
        default=None,
        description="waiting | done | null for open tasks",
    )
    discuss: bool = False
    deadline_on: str | None = Field(
        default=None,
        description="Filter or postpone date as YYYY-MM-DD in Europe/Kyiv",
    )
    waiting_for: str | None = None
    is_completed_past_action: bool = False
    task_title: str | None = None
    project: str | None = None
    people: list[str] = Field(default_factory=list)
    deadline: str | None = None
    estimated_minutes: int | None = None
    desired_outcome: str | None = None
    retry_previous: bool = Field(
        default=False,
        description="Re-run the previous query, usually more broadly",
    )
    continue_previous: bool = Field(
        default=False,
        description="This message continues the previous request",
    )
    broader_search: bool = Field(
        default=False,
        description="Include title/outcome mentions, not only structured person links",
    )
    replace_person: str | None = Field(
        default=None,
        description="Correct working person to this name as stated (e.g. Хома)",
    )
    exclude_person: str | None = Field(
        default=None,
        description="Person this is explicitly not (e.g. Тарас Хома)",
    )
    chat_reply: str | None = Field(
        default=None,
        description="Optional short greeting/thanks when kind=general_chat",
    )
    period: str | None = Field(
        default=None,
        description="today | tomorrow | this_week | next_week | this_month",
    )
    exclude_overdue: bool = False
    include_overdue: bool = False
    available_minutes: int | None = None
    use_listed_ids: bool = Field(
        default=False,
        description="Operate on the last Python-listed task IDs, not a new search",
    )
    exclude_indexes: list[int] = Field(
        default_factory=list,
        description="1-based indexes to drop from the last listed set",
    )
    follow_up_tool: str | None = Field(
        default=None,
        description="Optional second Python tool this turn: fit_minutes | sum_estimates",
    )
    advice: bool = Field(
        default=False,
        description="True if the user wants a recommendation, not only a fact dump",
    )
    entity_type: str | None = Field(default=None, description="person | project")
    canonical: str | None = Field(
        default=None,
        description="Canonical entity name when confirming equivalence",
    )
    alias: str | None = Field(
        default=None,
        description="Alias that should resolve to canonical",
    )
    pending_action: str | None = Field(
        default=None,
        description=(
            "How this utterance relates to a pending session: "
            "provide_requested_value | cancel_pending_action | correct_pending_action | "
            "switch_intent | continue_pending_action | general_chat"
        ),
    )
    provided_value: str | None = Field(
        default=None,
        description="Value for the awaited field when pending_action=provide_requested_value",
    )
    skip_actual_minutes: bool = Field(
        default=False,
        description="Complete without recording actual_minutes",
    )


class PhraseReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(description="Short Ukrainian reply using only provided facts")


class GroundedReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(description="Short Ukrainian reply using only FACTS")
    referenced_task_ids: list[str] = Field(default_factory=list)
    stated_count: int | None = None
    stated_total_minutes: int | None = None
