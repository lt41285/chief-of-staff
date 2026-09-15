"""add reminder claim leases

Revision ID: 0004_reminder_leases
Revises: 0003_task_reminders
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_reminder_leases"
down_revision: Union[str, None] = "0003_task_reminders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("task_reminders", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "task_reminders",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_task_reminders_claim",
        "task_reminders",
        ["sent_at", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_reminders_claim", table_name="task_reminders")
    op.drop_column("task_reminders", "lease_expires_at")
    op.drop_column("task_reminders", "claimed_at")
