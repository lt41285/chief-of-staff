"""add person aliases

Revision ID: 0009_person_aliases
Revises: 0008_task_waiting_for_person
Create Date: 2026-09-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009_person_aliases"
down_revision: Union[str, None] = "0008_task_waiting_for_person"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "person_aliases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("person_id", UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("normalized_alias", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("owner_user_id", "normalized_alias", name="uq_person_aliases_owner_alias"),
    )
    op.create_index("ix_person_aliases_person_id", "person_aliases", ["person_id"])
    op.create_index("ix_person_aliases_owner_user_id", "person_aliases", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_person_aliases_owner_user_id", table_name="person_aliases")
    op.drop_index("ix_person_aliases_person_id", table_name="person_aliases")
    op.drop_table("person_aliases")
