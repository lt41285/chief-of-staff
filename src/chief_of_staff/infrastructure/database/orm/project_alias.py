"""Project aliases — alternative names for a canonical project."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.common import uuid_pk


class ProjectAliasRow(Base):
    __tablename__ = "project_aliases"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "normalized_alias",
            name="uq_project_aliases_owner_alias",
        ),
    )

    id = uuid_pk()
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
