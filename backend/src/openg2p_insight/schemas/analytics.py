"""Pydantic schemas for /analytics endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AnalyticsRequest(BaseModel):
    entity: str
    column: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    filters: List[dict] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)
    sample_size: Optional[int] = None


class AnalyticsResponse(BaseModel):
    status: str
    result: Dict[str, Any] = Field(default_factory=dict)
    viz_spec: Dict[str, Any] = Field(default_factory=dict)
    assumptions: List[str] = Field(default_factory=list)
    error: Optional[str] = None
