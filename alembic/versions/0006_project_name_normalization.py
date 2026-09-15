"""recalculate project matching keys for acronyms

Revision ID: 0006_project_name_normalization
Revises: 0005_project_aliases
Create Date: 2026-08-30

Recomputes projects.name_normalized and project_aliases.normalized_alias
with acronym-aware folding (BG / B G / B.G. → bg).

Does not merge projects. If two of the same owner's rows would share a key,
the existing unique value is left unchanged so the user can merge explicitly.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from chief_of_staff.services.names import normalize_project_name

revision: str = "0006_project_name_normalization"
down_revision: Union[str, None] = "0005_project_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    projects = conn.execute(
        sa.text(
            """
            SELECT id, owner_user_id, name, name_normalized
            FROM projects
            """
        )
    ).fetchall()
    taken: dict[tuple[object, str], object] = {}
    for project_id, owner_id, name, current in projects:
        taken[(owner_id, current)] = project_id

    skipped_projects: list[str] = []
    for project_id, owner_id, name, current in projects:
        new_key = normalize_project_name(name)
        if new_key == current:
            continue
        clash_id = taken.get((owner_id, new_key))
        if clash_id is not None and clash_id != project_id:
            skipped_projects.append(f"{name!r} ({project_id}) would collide with {clash_id}")
            continue
        conn.execute(
            sa.text(
                """
                UPDATE projects
                SET name_normalized = :new_key
                WHERE id = :id
                """
            ),
            {"new_key": new_key, "id": project_id},
        )
        taken.pop((owner_id, current), None)
        taken[(owner_id, new_key)] = project_id

    aliases = conn.execute(
        sa.text(
            """
            SELECT a.id, a.owner_user_id, a.project_id, a.alias, a.normalized_alias,
                   p.name_normalized
            FROM project_aliases AS a
            JOIN projects AS p ON p.id = a.project_id
            """
        )
    ).fetchall()
    alias_taken: dict[tuple[object, str], object] = {
        (owner_id, key): alias_id for alias_id, owner_id, _pid, _alias, key, _pn in aliases
    }
    skipped_aliases: list[str] = []
    for alias_id, owner_id, project_id, alias, current, project_key in aliases:
        new_key = normalize_project_name(alias)
        if new_key == project_key:
            conn.execute(sa.text("DELETE FROM project_aliases WHERE id = :id"), {"id": alias_id})
            alias_taken.pop((owner_id, current), None)
            continue
        if new_key == current:
            continue
        clash = alias_taken.get((owner_id, new_key))
        if clash is not None and clash != alias_id:
            skipped_aliases.append(f"{alias!r} ({alias_id}) would collide with {clash}")
            continue
        canonical = conn.execute(
            sa.text(
                """
                SELECT id FROM projects
                WHERE owner_user_id IS NOT DISTINCT FROM :owner
                  AND name_normalized = :key
                """
            ),
            {"owner": owner_id, "key": new_key},
        ).scalar()
        if canonical is not None and canonical != project_id:
            skipped_aliases.append(
                f"{alias!r} ({alias_id}) would collide with canonical project {canonical}"
            )
            continue
        conn.execute(
            sa.text(
                """
                UPDATE project_aliases
                SET normalized_alias = :new_key
                WHERE id = :id
                """
            ),
            {"new_key": new_key, "id": alias_id},
        )
        alias_taken.pop((owner_id, current), None)
        alias_taken[(owner_id, new_key)] = alias_id

    if skipped_projects or skipped_aliases:
        print("project normalization collisions (not merged):")
        for line in skipped_projects + skipped_aliases:
            print(f"  - {line}")


def downgrade() -> None:
    # Matching keys are derived from display names; rolling back would be lossy.
    pass
