"""Structured project-management commands. Execution stays in Python."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ProjectIntentKind(StrEnum):
    CREATE_TASK = "create_task"
    CREATE_PROJECT = "create_project"
    LIST_PROJECTS = "list_projects"
    RENAME_PROJECT = "rename_project"
    MERGE_PROJECTS = "merge_projects"
    PROJECT_TASKS = "project_tasks"
    LINK_ALIAS = "link_alias"
    ARCHIVE_PROJECT = "archive_project"
    LIST_ARCHIVED_PROJECTS = "list_archived_projects"
    RESTORE_PROJECT = "restore_project"
    DELETE_PROJECT_PERMANENTLY = "delete_project_permanently"


class ProjectIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProjectIntentKind = Field(description="User command type")
    keep_name: str | None = Field(
        default=None,
        description="Canonical project to keep after merge/rename target display",
    )
    merge_names: list[str] = Field(
        default_factory=list,
        description="Project names to merge into keep_name",
    )
    old_name: str | None = Field(default=None, description="Project to rename")
    new_name: str | None = Field(default=None, description="New canonical name")
    project_query: str | None = Field(
        default=None,
        description="Project name or alias for listing that project's tasks",
    )
    alias_name: str | None = Field(
        default=None,
        description="Name that should become an alias of keep_name",
    )
