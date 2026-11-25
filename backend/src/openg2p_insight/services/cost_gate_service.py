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

from openg2p_fastapi_common.service import BaseService
from pydantic import BaseModel
from sqlalchemy import text

from ..config import Settings
from ..utils.sql_safety import StaticIssue, static_analyze
from .registry_db import registry_engine
from .sql_validator_service import ValidatedSql

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
        self._max_scan_cost = _config.cost_gate.max_scan_cost

    async def assess(self, planned: ValidatedSql) -> CostReport:
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
        # Against the REGISTRY database — that is where the reporting views are.
        # Pricing the query on the app's own database would fail on every table
        # name in the catalog, which is exactly how this was found.
        engine = registry_engine()
        if engine is None:
            return CostReport(
                level=CostLevel.RED,
                static_issues=[i.message for i in static_issues],
                bottleneck="no_registry_database",
                suggestion="No registry database is configured for Insight, so "
                           "queries cannot be planned or run. Set registryDB in "
                           "the chart.",
            )
        try:
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

        # Stage 3: classify.
        #
        # Against the cost of READING the inputs, not against a fixed budget.
        # A fixed budget prices the size of the table rather than the difficulty
        # of the question — see CostGateConfig for how that made COUNT(*) on the
        # register, one row out, look expensive.
        floor = self._scan_floor(plan)
        ratio = cost / floor if floor > 0 else 1.0

        if floor > self._max_scan_cost:
            # Efficient, but there is simply a great deal to read.
            level = CostLevel.YELLOW if ratio <= self._yellow.work_ratio else CostLevel.RED
        elif rows <= self._green.rows and ratio <= self._green.work_ratio:
            level = CostLevel.GREEN
        elif rows <= self._yellow.rows and ratio <= self._yellow.work_ratio:
            level = CostLevel.YELLOW
        else:
            level = CostLevel.RED

        return CostReport(
            level=level,
            estimated_rows=rows,
            estimated_cost=cost,
            static_issues=[i.message for i in static_issues],
            # Only worth naming when the query is actually being held back.
            bottleneck=(self._find_bottleneck(plan)
                        if level is not CostLevel.GREEN else None),
        )

    @staticmethod
    def _scan_floor(plan: dict) -> float:
        """Cost of the plan's leaf scans — the work no plan could avoid.

        Every question about 100,000 farmers has to read 100,000 farmers. That
        part is not the query being expensive; it is the register being large.
        What the gate should react to is what a plan does ON TOP of it.
        """
        total = 0.0

        def walk(node: dict) -> None:
            nonlocal total
            children = node.get("Plans") or []
            if not children:
                total += float(node.get("Total Cost") or 0.0)
            for child in children:
                walk(child)

        walk(plan)
        return total

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
        # Aggregate, Sort and the join nodes carry no Relation Name of their
        # own, so the message read "Aggregate on ?" and named nothing the reader
        # could act on. Borrow the name from the scan underneath.
        rel = top.get("Relation Name")
        if not rel:
            def under(node: dict) -> Optional[str]:
                if node.get("Relation Name"):
                    return node["Relation Name"]
                for child in node.get("Plans") or []:
                    found = under(child)
                    if found:
                        return found
                return None

            rel = under(top)
        return (f"{top.get('Node Type')}"
                f"{f' on {rel}' if rel else ''} (cost {top.get('Total Cost')})")
