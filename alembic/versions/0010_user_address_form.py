"""add users.address_form

Revision ID: 0010_user_address_form
Revises: 0009_person_aliases
Create Date: 2026-09-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_user_address_form"
down_revision: Union[str, None] = "0009_person_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("address_form", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "address_form")
