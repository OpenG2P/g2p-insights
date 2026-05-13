"""Local audit log table."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = {"schema": "insight"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # `username` (not `user`) — `user` is a reserved word in Postgres and SQL
    # generation around it gets brittle. We pay one rename for permanent safety.
    username: Mapped[str] = mapped_column(String(256), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    estimated_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
