"""Pydantic schemas for the hierarchical drill-down endpoints.

Deliberately free of any country, level name or fixed depth: a "level" is
whatever Master Data Service says it is, and the hierarchy is discovered by
walking `parent_level_id`.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class DrillMeasure(BaseModel):
    """What to aggregate at each level.

    `column` is ignored for COUNT. Both table and column are validated against
    the catalog allow-list before any SQL is built.
    """

    agg: str = Field(default="count", description="count | sum | avg | min | max")
    column: Optional[str] = None
    label: Optional[str] = None


class DrillRequest(BaseModel):
    #: Entity to aggregate. Must appear in the catalog's `safe_views`.
    table: str
    measure: DrillMeasure = DrillMeasure()
    #: Level to group by. When omitted the root level of the hierarchy is used,
    #: so callers never need to know what the top level is called.
    level_id: Optional[str] = None
    #: Restrict to one parent node (the value clicked at the level above).
    parent_level_value_id: Optional[str] = None
    #: Ordered ancestor chain, used to build the WHERE clause and breadcrumb.
    #: Each entry is a {level_id, level_value_id} pair.
    path: List["DrillPathItem"] = Field(default_factory=list)
    limit: int = 500


class DrillPathItem(BaseModel):
    level_id: str
    level_value_id: str
    label: Optional[str] = None


class DrillLevel(BaseModel):
    level_id: str
    level_mnemonic: str
    parent_level_id: Optional[str] = None
    #: True when this level has no children — the UI stops offering drill.
    is_leaf: bool = False
    #: False when the catalog has no `geo_columns` mapping for this level, i.e.
    #: the data cannot be grouped this deep even though the hierarchy defines it.
    is_queryable: bool = True


class DrillRow(BaseModel):
    #: MDS level_value_id when it could be resolved, else the raw DB value.
    code: str
    label: str
    value: Optional[float] = None
    #: True when this row can be drilled into further.
    drillable: bool = False


class DrillResponse(BaseModel):
    level: DrillLevel
    next_level: Optional[DrillLevel] = None
    breadcrumb: List[DrillPathItem] = Field(default_factory=list)
    rows: List[DrillRow] = Field(default_factory=list)
    #: SQL is always surfaced, consistent with the Ask flow.
    sql: str = ""
    k_suppressions: int = 0
    measure_label: str = ""


class HierarchyResponse(BaseModel):
    """The full level chain for this deployment, root first."""

    levels: List[DrillLevel] = Field(default_factory=list)


DrillRequest.model_rebuild()
