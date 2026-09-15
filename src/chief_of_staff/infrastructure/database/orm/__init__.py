"""ORM table mappings. Import this module from Alembic so metadata is complete."""

from chief_of_staff.infrastructure.database.orm.person import PersonRow
from chief_of_staff.infrastructure.database.orm.person_alias import PersonAliasRow
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.project_alias import ProjectAliasRow
from chief_of_staff.infrastructure.database.orm.project import ProjectRow
from chief_of_staff.infrastructure.database.orm.project_alias import ProjectAliasRow
from chief_of_staff.infrastructure.database.orm.reminder import TaskReminderRow
from chief_of_staff.infrastructure.database.orm.task import TaskPersonRow, TaskRow
from chief_of_staff.infrastructure.database.orm.user import UserRow

__all__ = [
    "PersonAliasRow",
    "PersonRow",
    "ProjectAliasRow",
    "ProjectRow",
    "TaskPersonRow",
    "TaskReminderRow",
    "TaskRow",
    "UserRow",
]
