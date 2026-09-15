"""nullable waiting_for_person_id on tasks

Revision ID: 0008_task_waiting_for_person
Revises: 0007_backfill_task_owners
Create Date: 2026-08-30

Stores who a waiting task is blocked on. Independent of task_people.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_task_waiting_for_person"
down_revision: Union[str, None] = "0007_backfill_task_owners"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "waiting_for_person_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("people.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_tasks_waiting_for_person_id",
        "tasks",
        ["waiting_for_person_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_waiting_for_person_id", table_name="tasks")
    op.drop_column("tasks", "waiting_for_person_id")
