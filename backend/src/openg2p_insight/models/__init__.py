"""SQLAlchemy models for the insight schema (audit_log, chat_session, …)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for insight-owned tables."""

    metadata_schema = "insight"
