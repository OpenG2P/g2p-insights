"""Chat session table — stores conversation context for multi-turn /ask flows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class ChatSession(Base):
    __tablename__ = "chat_session"
    __table_args__ = {"schema": "insight"}

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # `username` rather than `user` — `user` is reserved in Postgres.
    username: Mapped[str] = mapped_column(String(256), index=True)

    # Original (raw) NL question — kept verbatim for audit.
    original_question: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Multi-turn message log: [{role, content, kind?}, ...]
    messages: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Session lifecycle: "clarifying" → "ready" → "executed" (or "rejected")
    status: Mapped[str] = mapped_column(String(24), default="clarifying", index=True)

    # Cached planning artifacts.
    last_intent: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    placeholder_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
