"""People table."""

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from chief_of_staff.infrastructure.database.base import Base
from chief_of_staff.infrastructure.database.orm.common import TimestampMixin, uuid_pk


class PersonRow(Base, TimestampMixin):
    __tablename__ = "people"
    __table_args__ = (UniqueConstraint("name_normalized", name="uq_people_name_normalized"),)

    id = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_normalized: Mapped[str] = mapped_column(String(255), nullable=False)
    organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
