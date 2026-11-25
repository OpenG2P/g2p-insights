"""
Hierarchical drill-down engine.

This is the one drill mechanic Apache Superset does not provide natively
(it ships cross-filter, drill-by and drill-to-detail). Everything here is
driven by Master Data Service metadata plus the deployment catalog:

  * the hierarchy is DISCOVERED by walking `parent_level_id` — no assumed
    depth, no assumed level names ("region"/"woreda" are one country's words);
  * the DB column for a level comes from the catalog's `geo_columns` mapping
    (level_mnemonic -> column), so onboarding a country is data, not code;
  * the same engine serves non-geographic hierarchies (payment funnel,
    programme, time) — geography is simply the renderer that also needs
    boundary polygons.

Safety: the table must be in the catalog allow-list, the aggregate is chosen
from a fixed set, identifiers are validated against the catalog, and parent
filters are passed as bound parameters. The generated SQL then goes through
the normal validator + executor (read-only role, statement timeout,
k-anonymity) exactly like AI-generated SQL.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..schemas.drill import (
    DrillLevel,
    DrillMeasure,
    DrillPathItem,
    DrillRequest,
    DrillResponse,
    DrillRow,
)
from .masterdata_service import MasterDataService
from .query_executor_service import QueryExecutorService
from .schema_service import SchemaService
from .sql_validator_service import SqlValidatorService

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)

# Aggregates we are willing to emit. Anything else is rejected outright rather
# than interpolated into SQL.
_ALLOWED_AGGS = {"count", "sum", "avg", "min", "max"}

# Conservative identifier guard — the catalog is trusted, but this stops a
# malformed catalog entry from becoming an injection vector.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DrillError(ValueError):
    """Raised for a caller/config problem — surfaced as HTTP 400."""


class DrillService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._mds = MasterDataService.get_component()
        self._schema = SchemaService.get_component()
        self._validator = SqlValidatorService.get_component()
        self._exec = QueryExecutorService.get_component()

    # ------------------------------------------------------------- hierarchy

    async def get_hierarchy(self) -> List[DrillLevel]:
        """Ordered level chain, root first.

        Order is derived from `parent_level_id`, never from a naming
        convention or an assumed count.
        """
        raw = await self._mds.get_levels()
        return self._order_levels(raw)

    def _order_levels(self, raw: List[dict]) -> List[DrillLevel]:
        by_parent: Dict[Optional[str], List[dict]] = {}
        for lv in raw:
            by_parent.setdefault(lv.get("parent_level_id") or None, []).append(lv)

        geo_map = self._geo_column_map()
        ordered: List[DrillLevel] = []
        # Walk from the root down. If the data is malformed (cycle, orphan) we
        # stop rather than loop forever.
        cursor: Optional[str] = None
        seen: set[str] = set()
        while True:
            children = by_parent.get(cursor) or []
            if not children:
                break
            node = children[0]  # single chain; siblings at a level are values
            lid = str(node.get("level_id"))
            if lid in seen:
                _logger.warning("Cycle detected in geo levels at %s — stopping", lid)
                break
            seen.add(lid)
            mnemonic = str(node.get("level_mnemonic") or lid)
            ordered.append(
                DrillLevel(
                    level_id=lid,
                    level_mnemonic=mnemonic,
                    parent_level_id=node.get("parent_level_id"),
                    is_queryable=mnemonic in geo_map,
                )
            )
            cursor = lid
        if ordered:
            ordered[-1].is_leaf = True
        return ordered

    def _geo_column_map(self) -> Dict[str, str]:
        """level_mnemonic -> DB column, from the deployment catalog."""
        return {
            gc.level_mnemonic: gc.column
            for gc in getattr(self._schema.catalog, "geo_columns", []) or []
        }

    # ----------------------------------------------------------------- drill

    async def drill(self, req: DrillRequest) -> DrillResponse:
        levels = await self.get_hierarchy()
        if not levels:
            raise DrillError(
                "No geo levels available from Master Data Service — cannot drill."
            )

        target = self._resolve_target_level(levels, req)
        idx = next(i for i, lv in enumerate(levels) if lv.level_id == target.level_id)
        next_level = levels[idx + 1] if idx + 1 < len(levels) else None

        geo_map = self._geo_column_map()
        group_col = geo_map.get(target.level_mnemonic)
        if not group_col:
            raise DrillError(
                f"The catalog has no geo_columns mapping for level "
                f"'{target.level_mnemonic}'. Add it under geo_columns to drill here."
            )

        table = self._validate_table(req.table)
        self._validate_identifier(group_col, "geo column")
        select_expr, measure_label = self._build_measure(req.measure)

        where_sql, params = self._build_parent_filter(req, geo_map, levels)

        sql = (
            f"SELECT {group_col} AS level_value, {select_expr} AS value\n"
            f"FROM {table}\n"
            f"{where_sql}"
            f"GROUP BY {group_col}\n"
            f"ORDER BY value DESC NULLS LAST\n"
            f"LIMIT {int(req.limit)}"
        )

        # NOTE: the validator's second argument is the PII placeholder map, NOT
        # SQL bind parameters. Our ancestor filters use real bind params
        # (`:drill_p0`, …), so we validate the SQL on its own and then merge our
        # params into the ValidatedSql before execution.
        validated = self._validator.validate(sql)
        validated.params.update(params)
        # The validator re-renders with the Postgres dialect, which emits named
        # binds in pyformat (`%(drill_p0)s`); SQLAlchemy's text() expects colon
        # style (`:drill_p0`). Convert our own known, safe bind names back so
        # the parameters actually bind (otherwise KeyError at execution).
        for pname in params:
            validated.sql = validated.sql.replace(f"%({pname})s", f":{pname}")
        result = await self._exec.run(validated)

        rows = await self._label_rows(result.rows, target, req)
        drillable = bool(next_level and next_level.is_queryable)
        for r in rows:
            r.drillable = drillable

        return DrillResponse(
            level=target,
            next_level=next_level,
            breadcrumb=list(req.path),
            rows=rows,
            sql=validated.sql,
            k_suppressions=result.k_suppressions,
            measure_label=measure_label,
        )

    # ------------------------------------------------------------- internals

    def _resolve_target_level(
        self, levels: List[DrillLevel], req: DrillRequest
    ) -> DrillLevel:
        """Explicit level wins; otherwise drill one below the deepest item in
        the path; otherwise start at the root. The caller never needs to know
        the country's level names."""
        if req.level_id:
            for lv in levels:
                if lv.level_id == req.level_id:
                    return lv
            raise DrillError(f"Unknown level_id {req.level_id!r}")

        if req.path:
            deepest = req.path[-1].level_id
            for i, lv in enumerate(levels):
                if lv.level_id == deepest:
                    if i + 1 >= len(levels):
                        raise DrillError(
                            f"Level {deepest!r} is the leaf — nothing to drill into."
                        )
                    return levels[i + 1]
            raise DrillError(f"Unknown level_id {deepest!r} in path")

        return levels[0]

    def _validate_table(self, table: str) -> str:
        allowed = set(self._schema.catalog.safe_table_names())
        if table not in allowed:
            # Fail closed — mirrors the AI planner's allow-list behaviour.
            raise DrillError(
                f"Table {table!r} is not in the catalog allow-list (safe_views)."
            )
        # Allow a schema-qualified name, validating each part.
        for part in table.split("."):
            self._validate_identifier(part, "table")
        return table

    @staticmethod
    def _validate_identifier(ident: str, what: str) -> None:
        if not _IDENT_RE.match(ident):
            raise DrillError(f"Unsafe {what} identifier: {ident!r}")

    def _build_measure(self, m: DrillMeasure) -> Tuple[str, str]:
        agg = (m.agg or "count").lower()
        if agg not in _ALLOWED_AGGS:
            raise DrillError(
                f"Unsupported aggregate {m.agg!r}. Allowed: {sorted(_ALLOWED_AGGS)}"
            )
        if agg == "count":
            return "COUNT(*)", m.label or "Count"
        if not m.column:
            raise DrillError(f"Aggregate {agg!r} requires a column.")
        self._validate_identifier(m.column, "measure column")
        return f"{agg.upper()}({m.column})", m.label or f"{agg.upper()}({m.column})"

    def _build_parent_filter(
        self,
        req: DrillRequest,
        geo_map: Dict[str, str],
        levels: List[DrillLevel],
    ) -> Tuple[str, Dict[str, str]]:
        """Constrain the query to the clicked ancestor chain.

        Every ancestor in `path` contributes an equality filter on its own geo
        column, so drilling into district X of region Y cannot leak rows from
        another region. Values are bound parameters, never interpolated.
        """
        params: Dict[str, str] = {}
        clauses: List[str] = []
        mnemonic_by_id = {lv.level_id: lv.level_mnemonic for lv in levels}

        path = list(req.path)
        # Convenience for simple callers that pass only the immediate parent:
        # infer its level as the one directly above the target.
        if not path and req.parent_level_value_id and req.level_id:
            idx = next(
                (i for i, lv in enumerate(levels) if lv.level_id == req.level_id), None
            )
            if idx is not None and idx > 0:
                path = [
                    DrillPathItem(
                        level_id=levels[idx - 1].level_id,
                        level_value_id=req.parent_level_value_id,
                    )
                ]

        for i, item in enumerate(path):
            mnemonic = mnemonic_by_id.get(item.level_id)
            col = geo_map.get(mnemonic) if mnemonic else None
            if not col:
                # An ancestor level with no column mapping cannot be filtered.
                # Skipping would silently widen the result set, so refuse.
                raise DrillError(
                    f"Cannot filter by ancestor level {item.level_id!r}: no "
                    f"geo_columns mapping for '{mnemonic}'."
                )
            self._validate_identifier(col, "geo column")
            pname = f"drill_p{i}"
            clauses.append(f"{col} = :{pname}")
            params[pname] = _leaf_of(item.level_value_id)

        where_sql = f"WHERE {' AND '.join(clauses)}\n" if clauses else ""
        return where_sql, params

    async def _label_rows(
        self, raw_rows: List[dict], level: DrillLevel, req: DrillRequest
    ) -> List[DrillRow]:
        """Attach human labels from MDS where the DB value matches a known
        level value; otherwise fall back to the raw value so nothing is
        silently dropped."""
        parent_value_id = req.path[-1].level_value_id if req.path else None
        try:
            mds_values = await self._mds.get_level_values(
                level_id=level.level_id, parent_level_value_id=parent_value_id
            )
        except Exception as exc:  # noqa: BLE001 — labels are best-effort
            _logger.warning("Could not fetch level values for labelling: %s", exc)
            mds_values = []

        # Prefer pcode as the join key once master-data-service supplies it —
        # name matching is the known-unreliable fallback. Both indexes are
        # built so a partially-backfilled deployment still resolves what it can.
        by_pcode = {
            str(v["pcode"]).lower(): v for v in mds_values if v.get("pcode")
        }
        by_mnemonic = {
            str(v.get("level_value_mnemonic", "")).lower(): v for v in mds_values
        }

        out: List[DrillRow] = []
        for r in raw_rows:
            raw_value = r.get("level_value")
            if raw_value is None:
                continue
            key = str(raw_value).lower()
            match = by_pcode.get(key) or by_mnemonic.get(key)
            out.append(
                DrillRow(
                    code=str(match.get("pcode") or match.get("level_value_id"))
                    if match
                    else str(raw_value),
                    # Presentable label when MDS has one, else the raw value.
                    label=str(
                        (match or {}).get("display_name") or raw_value
                    ),
                    value=_as_float(r.get("value")),
                )
            )
        return out


def _leaf_of(level_value_id: str) -> str:
    """MDS ids are path-style (`country/region/district`). Registry columns
    store the leaf token, so compare on the last segment.

    NOTE: this is a name-based join and is the known weak point — see the
    Master Data Service change list. Once MDS carries a pcode, both sides
    should join on that instead.
    """
    return str(level_value_id).rsplit("/", 1)[-1]


def _as_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
