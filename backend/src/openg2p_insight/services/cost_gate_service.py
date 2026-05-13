"""
Cost gate.

Combines:
  1. Static analysis on the planned SQL (single SELECT, no banned constructs).
  2. EXPLAIN (no ANALYZE) against Postgres for planner row + cost estimates.
  3. Classification into GREEN / YELLOW / RED based on configured thresholds.
"""

from __future__ import annotations

import enum
import json
import logging
from typing import Optional

from openg2p_fastapi_common.context import dbengine
from openg2p_fastapi_common.service import BaseService
from pydantic import BaseModel
from sqlalchemy import text

from ..config import Settings
from ..utils.sql_safety import StaticIssue, static_analyze
from .sql_planner_service import PlannedSql

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class CostLevel(str, enum.Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class CostReport(BaseModel):
    level: CostLevel
    estimated_rows: Optional[int] = None
    estimated_cost: Optional[float] = None
    bottleneck: Optional[str] = None
    suggestion: Optional[str] = None
    static_issues: list[str] = []


class CostGateService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._green = _config.cost_gate.green
        self._yellow = _config.cost_gate.yellow

    async def assess(self, planned: PlannedSql) -> CostReport:
        # Stage 1: static
        static_issues: list[StaticIssue] = static_analyze(planned.sql)
        blockers = [i for i in static_issues if i.blocker]
        if blockers:
            return CostReport(
                level=CostLevel.RED,
                static_issues=[i.message for i in static_issues],
                bottleneck=blockers[0].message,
            )

        # Stage 2: EXPLAIN (no execution). If this fails the planner couldn't
        # even price the query — refuse to run it.
        try:
            engine = dbengine.get()
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(f"EXPLAIN (FORMAT JSON) {planned.sql}"), planned.params
                )
                row = result.scalar_one()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("EXPLAIN failed; classifying as RED: %s", exc)
            return CostReport(
                level=CostLevel.RED,
                static_issues=[i.message for i in static_issues],
                bottleneck=f"explain_failed: {exc!s}",
                suggestion="The query could not be planned. Reformulate or check column names.",
            )

        # Some drivers return EXPLAIN JSON as a string; parse defensively.
        if isinstance(row, str):
            try:
                row = json.loads(row)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("EXPLAIN JSON unparseable; classifying as RED: %s", exc)
                return CostReport(
                    level=CostLevel.RED,
                    static_issues=[i.message for i in static_issues],
                    bottleneck="explain_json_unparseable",
                )

        try:
            plan = row[0]["Plan"] if isinstance(row, list) else row["Plan"]
            rows = int(plan.get("Plan Rows") or 0)
            cost = float(plan.get("Total Cost") or 0.0)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("EXPLAIN JSON unexpected shape; classifying as RED: %s", exc)
            return CostReport(
                level=CostLevel.RED,
                static_issues=[i.message for i in static_issues],
                bottleneck="explain_json_unexpected_shape",
            )

        # Stage 3: classify
        if rows <= self._green.rows and cost <= self._green.cost:
            level = CostLevel.GREEN
        elif rows <= self._yellow.rows and cost <= self._yellow.cost:
            level = CostLevel.YELLOW
        else:
            level = CostLevel.RED

        return CostReport(
            level=level,
            estimated_rows=rows,
            estimated_cost=cost,
            static_issues=[i.message for i in static_issues],
            bottleneck=self._find_bottleneck(plan),
        )

    @staticmethod
    def _find_bottleneck(plan: dict) -> Optional[str]:
        nodes = []

        def walk(node: dict) -> None:
            nodes.append(node)
            for child in node.get("Plans") or []:
                walk(child)

        walk(plan)
        nodes.sort(key=lambda n: n.get("Total Cost", 0.0), reverse=True)
        if not nodes:
            return None
        top = nodes[0]
        return f"{top.get('Node Type')} on {top.get('Relation Name', '?')} (cost {top.get('Total Cost')})"
