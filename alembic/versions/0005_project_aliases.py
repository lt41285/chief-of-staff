"""add project aliases and per-user project ownership

Revision ID: 0005_project_aliases
Revises: 0004_reminder_leases
Create Date: 2026-08-30
"""

from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0005_project_aliases"
down_revision: Union[str, None] = "0004_reminder_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_owner_user_id",
        "projects",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"])

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE projects AS p
            SET owner_user_id = sub.owner_user_id
            FROM (
                SELECT DISTINCT ON (project_id) project_id, owner_user_id
                FROM tasks
                WHERE owner_user_id IS NOT NULL
                ORDER BY project_id, created_at
            ) AS sub
            WHERE p.id = sub.project_id
            """
        )
    )
    extra = conn.execute(
        sa.text(
            """
            SELECT DISTINCT t.project_id, t.owner_user_id, p.name, p.name_normalized,
                   p.description, p.status
            FROM tasks AS t
            JOIN projects AS p ON p.id = t.project_id
            WHERE t.owner_user_id IS NOT NULL
              AND p.owner_user_id IS DISTINCT FROM t.owner_user_id
            """
        )
    ).fetchall()
    for project_id, owner_id, name, name_normalized, description, status in extra:
        new_id = uuid4()
        conn.execute(
            sa.text(
                """
                INSERT INTO projects (
                    id, name, name_normalized, description, status, owner_user_id,
                    created_at, updated_at
                )
                VALUES (
                    :id, :name, :name_normalized, :description, :status, :owner_id,
                    now(), now()
                )
                """
            ),
            {
                "id": new_id,
                "name": name,
                "name_normalized": name_normalized,
                "description": description,
                "status": status,
                "owner_id": owner_id,
            },
        )
        conn.execute(
            sa.text(
                """
                UPDATE tasks
                SET project_id = :new_id
                WHERE project_id = :old_id AND owner_user_id = :owner_id
                """
            ),
            {"new_id": new_id, "old_id": project_id, "owner_id": owner_id},
        )

    op.drop_constraint("uq_projects_name_normalized", "projects", type_="unique")
    op.create_unique_constraint(
        "uq_projects_owner_name",
        "projects",
        ["owner_user_id", "name_normalized"],
    )

    op.create_table(
        "project_aliases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(255), nullable=False),
        sa.Column("normalized_alias", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("owner_user_id", "normalized_alias", name="uq_project_aliases_owner_alias"),
    )
    op.create_index("ix_project_aliases_project_id", "project_aliases", ["project_id"])
    op.create_index("ix_project_aliases_owner_user_id", "project_aliases", ["owner_user_id"])


def downgrade() -> None:
    op.drop_table("project_aliases")
    op.drop_constraint("uq_projects_owner_name", "projects", type_="unique")
    op.create_unique_constraint("uq_projects_name_normalized", "projects", ["name_normalized"])
    op.drop_constraint("fk_projects_owner_user_id", "projects", type_="foreignkey")
    op.drop_index("ix_projects_owner_user_id", table_name="projects")
    op.drop_column("projects", "owner_user_id")
