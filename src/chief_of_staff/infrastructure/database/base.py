"""Declarative base shared by ORM tables and Alembic metadata."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Root for all mapped tables."""
