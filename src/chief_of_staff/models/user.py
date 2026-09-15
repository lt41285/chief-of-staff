"""User identity as known to the application layer (not the ORM table)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class User(BaseModel):
    """A Telegram user registered with Chief of Staff.

    Task entities will be added later; this is the identity skeleton only.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    telegram_id: int = Field(..., description="Telegram user id")
    created_at: datetime
