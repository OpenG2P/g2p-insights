"""Saved chart metadata — local pointer to a Superset chart we authored."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


class SavedChart(Base):
    __tablename__ = "saved_chart"
    __table_args__ = {"schema": "insight"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256))
    superset_chart_id: Mapped[int] = mapped_column(Integer, index=True)
    superset_dashboard_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    form_data: Mapped[dict] = mapped_column(JSON)
    authored_by: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
