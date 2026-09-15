"""backfill null task owners from project owner

Revision ID: 0007_backfill_task_owners
Revises: 0006_project_name_normalization
Create Date: 2026-08-30

Legacy tasks created before user-scoping have owner_user_id NULL.
Project lists counted them; task lists inner-joined users and hid them.

Assigns those rows to the project owner. Does not merge or delete projects.
Tasks on projects with no owner are left unchanged.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_backfill_task_owners"
down_revision: Union[str, None] = "0006_project_name_normalization"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE tasks AS t
            SET owner_user_id = p.owner_user_id
            FROM projects AS p
            WHERE t.project_id = p.id
              AND t.owner_user_id IS NULL
              AND p.owner_user_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    pass
