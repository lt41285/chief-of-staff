"""create task reminders and task owners

Revision ID: 0003_task_reminders
Revises: 0002_task_entities
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0003_task_reminders"
down_revision: Union[str, None] = "0002_task_entities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("tasks", sa.Column("owner_user_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_tasks_owner_user_id", "tasks", ["owner_user_id"])
    op.create_foreign_key(
        "fk_tasks_owner_user_id",
        "tasks",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "task_reminders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reminder_type", sa.String(32), nullable=False),
        sa.Column("occurrence_date", sa.Date(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "task_id",
            "reminder_type",
            "occurrence_date",
            name="uq_task_reminders_logical",
        ),
        sa.CheckConstraint(
            "reminder_type IN ('day_before', 'due_today', 'overdue')",
            name="ck_task_reminders_type",
        ),
    )
    op.create_index("ix_task_reminders_task_id", "task_reminders", ["task_id"])
    op.create_index(
        "ix_task_reminders_due",
        "task_reminders",
        ["scheduled_for", "sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_reminders_due", table_name="task_reminders")
    op.drop_index("ix_task_reminders_task_id", table_name="task_reminders")
    op.drop_table("task_reminders")
    op.drop_constraint("fk_tasks_owner_user_id", "tasks", type_="foreignkey")
    op.drop_index("ix_tasks_owner_user_id", table_name="tasks")
    op.drop_column("tasks", "owner_user_id")
    op.drop_column("users", "telegram_chat_id")
