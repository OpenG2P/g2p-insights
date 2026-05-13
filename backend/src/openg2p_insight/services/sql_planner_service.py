"""
SQL Planner.

Converts an Intent JSON (from LlmService) + a PII placeholder map into a safe,
parameterised SQL string targeting only allow-listed views / materialised views.

The LLM never writes raw SQL; the planner emits known-safe shapes. Two
correctness guarantees worth knowing:

  • For metrics other than `count`, `metric_column` MUST be specified —
    otherwise the planner refuses to build SQL (rather than silently emitting
    `SUM(*)`-style garbage).

  • Every GROUP BY query also projects `_k_count = COUNT(*)`. The executor's
    k-anonymity layer uses this column, then strips it from the response.
    This keeps small groups suppressed even when the user asked for AVG / SUM
    rather than COUNT — which would otherwise leak averages over tiny cohorts.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from openg2p_fastapi_common.service import BaseService

from .llm_service import Filter, Intent
from .schema_service import SchemaService

_AGG_FUNCS = {"count", "count_distinct", "sum", "avg", "min", "max"}
_OPS = {
    "eq": "=",
    "neq": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
    "in": "IN",
    "like": "LIKE",
}

# Internal column the planner always projects for grouped queries. The executor
# strips this from the response after k-anonymity is enforced.
K_COUNT_COL = "_k_count"


class PlannedSql:
    """SQL + bind params + the originating Intent, plus the planner-projected
    column list so the executor can identify the `_k_count` column even if a
    grouped query has no other COUNT()."""

    def __init__(self, sql: str, params: Dict[str, object], intent: Intent, has_k_count: bool):
        self.sql = sql
        self.params = params
        self.intent = intent
        self.has_k_count = has_k_count

    def __str__(self) -> str:
        return self.sql


class SqlPlannerService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._schema = SchemaService.get_component()

    def build(self, intent: Intent, placeholder_map: Dict[str, str] | None = None) -> PlannedSql:
        placeholder_map = placeholder_map or {}
        self._validate_allowed_entity(intent.entity)

        select_clause, has_k_count = self._select_clause(intent)
        where_clause, params = self._where_clause(intent.filters, placeholder_map)
        group_clause = self._group_clause(intent.group_by)
        order_clause = self._order_clause(intent.order_by, metric=intent.metric)
        limit_clause = f"LIMIT {int(intent.limit)}" if intent.limit else ""

        sql = " ".join(
            part
            for part in [
                f"SELECT {select_clause}",
                f"FROM {self._safe_ident(intent.entity)}",
                where_clause,
                group_clause,
                order_clause,
                limit_clause,
            ]
            if part
        )
        return PlannedSql(sql=sql.strip(), params=params, intent=intent, has_k_count=has_k_count)

    # ---------- internals ----------------------------------------------------

    def _validate_allowed_entity(self, entity: str) -> None:
        allowed = set(self._schema.catalog.safe_table_names())
        if entity not in allowed:
            raise ValueError(
                f"Entity '{entity}' is not in the allow-listed safe views: {sorted(allowed)}"
            )

    def _select_clause(self, intent: Intent) -> Tuple[str, bool]:
        """Return (clause, has_k_count).

        Shape:
          - no GROUP BY:    `<AGG> AS metric`
          - with GROUP BY:  `<cols>, <AGG> AS metric, COUNT(*) AS _k_count`
            (unless the AGG is itself COUNT, in which case we reuse it for k-anonymity)
        """
        metric = (intent.metric or "count").lower()
        if metric not in _AGG_FUNCS:
            raise ValueError(f"Unsupported metric: {metric}")

        if metric == "count":
            agg = "COUNT(*) AS total"
        elif metric == "count_distinct":
            if not intent.metric_column:
                raise ValueError(
                    "metric='count_distinct' requires metric_column to be set on the Intent"
                )
            col = self._safe_ident(intent.metric_column)
            agg = f"COUNT(DISTINCT {col}) AS total"
        else:
            if not intent.metric_column:
                raise ValueError(
                    f"metric='{metric}' requires metric_column to be set on the Intent"
                )
            col = self._safe_ident(intent.metric_column)
            agg = f"{metric.upper()}({col}) AS metric_value"

        if intent.group_by:
            cols = ", ".join(self._safe_ident(c) for c in intent.group_by)
            if metric == "count":
                # COUNT(*) AS total IS the k-anonymity count too — alias it.
                return f"{cols}, COUNT(*) AS total, COUNT(*) AS {K_COUNT_COL}", True
            # For count_distinct / sum / avg / min / max, the metric column
            # is different from the k-anonymity row count, so emit both.
            return f"{cols}, {agg}, COUNT(*) AS {K_COUNT_COL}", True

        return agg, False

    def _where_clause(
        self, filters: List[Filter], placeholder_map: Dict[str, str]
    ) -> Tuple[str, Dict[str, object]]:
        if not filters:
            return "", {}
        parts: List[str] = []
        params: Dict[str, object] = {}
        for i, f in enumerate(filters):
            op = _OPS.get(f.op.lower())
            if op is None:
                raise ValueError(f"Unsupported op: {f.op}")
            value = f.val
            if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
                value = placeholder_map.get(value, value)
            col = self._safe_ident(f.col)
            pname = f"p{i}"

            if op == "IN":
                if not isinstance(value, (list, tuple)):
                    raise ValueError("IN filter requires a list/tuple value")
                if not value:
                    # An empty IN is impossible — emit `false` so the query still parses.
                    parts.append("FALSE")
                    continue
                placeholders: List[str] = []
                for j, v in enumerate(value):
                    pn = f"{pname}_{j}"
                    placeholders.append(f":{pn}")
                    params[pn] = v
                parts.append(f"{col} IN ({', '.join(placeholders)})")
            else:
                parts.append(f"{col} {op} :{pname}")
                params[pname] = value
        return "WHERE " + " AND ".join(parts), params

    @staticmethod
    def _group_clause(group_by: List[str]) -> str:
        if not group_by:
            return ""
        return "GROUP BY " + ", ".join(SqlPlannerService._safe_ident(c) for c in group_by)

    @staticmethod
    def _order_clause(order_by: List[str], metric: str = "count") -> str:
        """Render ORDER BY, normalising LLM-emitted metric names to our SELECT
        aliases. The planner aliases the metric as `total` (for count) or
        `metric_value` (for sum/avg/min/max), but the LLM tends to refer to
        it by natural names — `count`, `value`, `metric`. We translate those
        rather than reject the query."""
        if not order_by:
            return ""
        # Build the alias the metric was projected under.
        metric_alias = "total" if metric == "count" else "metric_value"
        # Names the LLM might use to mean "order by the aggregate value".
        natural_metric_aliases = {
            "count", "counts", "n", "total", "totals",
            "metric", "metric_value", "value", "values", "aggregate",
            "sum", "avg", "average", "min", "max", "mean",
        }

        parts: List[str] = []
        for raw in order_by:
            tokens = raw.strip().split()
            if not tokens:
                continue
            col = tokens[0]
            direction = ""
            if len(tokens) > 1 and tokens[1].lower() in ("asc", "desc"):
                direction = " " + tokens[1].upper()
            if col.lower() in natural_metric_aliases:
                parts.append(metric_alias + direction)
            else:
                parts.append(SqlPlannerService._safe_ident(col) + direction)
        return "ORDER BY " + ", ".join(parts) if parts else ""

    @staticmethod
    def _safe_ident(ident: str) -> str:
        # Allow only [A-Za-z0-9_.] — letters / digits / underscore, plus a single
        # dot for schema.table.col references. No spaces, no operators, nothing
        # that could escape the identifier.
        if not ident:
            raise ValueError("Empty identifier")
        for ch in ident:
            if not (ch.isalnum() or ch in "_."):
                raise ValueError(f"Unsafe identifier: {ident!r}")
        return ident
