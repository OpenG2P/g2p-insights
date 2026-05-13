"""Pydantic schemas for /dashboards endpoints."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from ..services.cost_gate_service import CostReport
from ..services.llm_service import VizSpec


class AuthorChartRequest(BaseModel):
    question: str
    user: Optional[str] = None
    dashboard_id: Optional[int] = None


class SaveChartRequest(BaseModel):
    name: str
    form_data: dict
    dataset: str
    dashboard_id: Optional[int] = None
    user: Optional[str] = None


class AuthorChartResponse(BaseModel):
    status: str
    chart_id: Optional[int] = None
    cost: Optional[CostReport] = None
    form_data: dict | None = None
    viz_spec: VizSpec | None = None
    preview_rows: List[dict] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    sql: Optional[str] = None
