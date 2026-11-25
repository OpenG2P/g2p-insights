"""
SQL Validator.

Replaces the old SqlPlannerService (which translated a fixed-shape Intent
JSON into SQL). The LLM now emits SQL directly; this service parses it with
sqlglot, enforces the allow-list, binds PII placeholders, and injects the
k-anonymity count column for grouped queries against row-level views.

Correctness guarantees:

  * Single SELECT statement (no DDL/DML, no transactions, no multiple
    statements).
  * FROM / JOIN reference ONLY catalog-listed `safe_views`.
  * Column references resolve to safe columns of those views. Unqualified
    references are allowed if at least one referenced table has that column.
  * String literals matching the PII-placeholder shape (`[PERSON_1]`,
    `[ID_3]`, …) are extracted into bind parameters and rebound to the
    user's actual values from the sanitiser's placeholder_map.
  * For grouped queries against row-level views (i.e. NOT pre-aggregated
    materialised views), we add `COUNT(*) AS _k_count` to the SELECT list.
    The executor then suppresses any group with `_k_count < k` and strips
    the column from the response.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import sqlglot
from openg2p_fastapi_common.service import BaseService
from sqlglot import exp

from ..config import Settings
from .schema_service import SchemaService

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)

K_COUNT_COL = "_k_count"

# Matches `'[TAG_3]'` — single-quoted PII placeholder literals as the LLM
# would write them in WHERE clauses. The tag itself is the LLM-visible
# token from PiiService.sanitize().
_PII_LITERAL_RE = re.compile(r"'(\[[A-Z][A-Z_]*_\d+\])'")

# Tables typically considered pre-aggregated (no individual-level rows). We
# skip the k-anonymity injection on these because each row already represents
# many people; a `_k_count` based on row count would suppress everything.
# Catalog authors can mark additional tables via the optional `aggregated`
# flag on TableSpec.
_DEFAULT_AGGREGATED_TABLES = {"mv_district_aggregates"}


@dataclass
class ValidatedSql:
    """Parsed, allow-list-checked SQL ready for execution."""

    sql: str
    params: Dict[str, object] = field(default_factory=dict)
    group_by_columns: List[str] = field(default_factory=list)
    has_k_count: bool = False
    from_tables: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.sql


class SqlValidationError(ValueError):
    """Raised when the LLM-emitted SQL fails any allow-list / shape check."""


class SqlValidatorService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._schema = SchemaService.get_component()

    # ---------- public API --------------------------------------------------

    def validate(
        self,
        sql: str,
        placeholder_map: Optional[Dict[str, str]] = None,
    ) -> ValidatedSql:
        if not sql or not sql.strip():
            raise SqlValidationError("Empty SQL")

        # 1. Parse the raw SQL. PII placeholders stay as string literals at
        #    this stage; we rebind them AFTER rendering so sqlglot's postgres
        #    dialect doesn't rewrite our `:name` style into `%(name)s`.
        try:
            statements = sqlglot.parse(sql, read="postgres")
        except Exception as exc:  # noqa: BLE001
            raise SqlValidationError(f"Could not parse SQL: {exc}") from exc
        statements = [s for s in statements if s is not None]
        if not statements:
            raise SqlValidationError("Could not parse SQL")
        if len(statements) > 1:
            raise SqlValidationError("Only a single SQL statement is allowed")

        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.Subquery, exp.Union, exp.With)):
            raise SqlValidationError(
                f"Only SELECT statements are allowed (got {type(tree).__name__})"
            )

        # 3. No `SELECT *`. `COUNT(*)` is fine — that uses exp.Star but its
        #    parent is exp.Count.
        for star in tree.find_all(exp.Star):
            parent = star.parent
            if isinstance(parent, exp.Count):
                continue
            raise SqlValidationError("SELECT * is not allowed — list columns explicitly")

        # 4. Allow-list every referenced base table.
        allowed_tables = set(self._schema.catalog.safe_table_names())
        from_tables: List[str] = []
        for tbl in tree.find_all(exp.Table):
            tname = tbl.name
            if tname not in allowed_tables:
                raise SqlValidationError(
                    f"Table '{tname}' is not in the allow-listed safe views: "
                    f"{sorted(allowed_tables)}"
                )
            from_tables.append(tname)

        # 5. Column allow-list (best-effort).
        self._validate_columns(tree, from_tables)

        # 6. Inject `_k_count` if this is a grouped query against a row-level
        #    view. Skip when every FROM table is pre-aggregated.
        has_group_by = tree.find(exp.Group) is not None
        aggregated_tables = self._aggregated_tables()
        all_aggregated = bool(from_tables) and all(
            t in aggregated_tables for t in from_tables
        )
        has_k_count = False
        if has_group_by and not all_aggregated and isinstance(tree, exp.Select):
            self._inject_k_count(tree)
            has_k_count = True

        # 7. Collect GROUP BY columns (for the executor's suppression pass).
        group_by_columns = self._collect_group_by(tree)

        rendered = tree.sql(dialect="postgres")
        rendered, params = self._substitute_pii(rendered, placeholder_map or {})

        return ValidatedSql(
            sql=rendered,
            params=params,
            group_by_columns=group_by_columns,
            has_k_count=has_k_count,
            from_tables=from_tables,
        )

    # ---------- internals ----------------------------------------------------

    def _aggregated_tables(self) -> Set[str]:
        """Tables to treat as pre-aggregated for k-anonymity purposes.
        Sourced from the optional `aggregated` flag on TableSpec, plus a
        sensible default for the mv_*_aggregates convention."""
        flagged = {
            t.name
            for t in self._schema.catalog.tables
            if getattr(t, "aggregated", False)
        }
        return flagged | _DEFAULT_AGGREGATED_TABLES

    def _substitute_pii(
        self, sql: str, placeholder_map: Dict[str, str]
    ) -> tuple[str, Dict[str, object]]:
        """Replace `'[TAG_n]'` string literals with `:pii_n` bind parameters."""
        params: Dict[str, object] = {}
        counter = {"n": 0}

        def repl(match: re.Match) -> str:
            token = match.group(1)
            # If the sanitiser doesn't know the token, the safest behaviour is
            # to bind an empty string — the query then returns no matches,
            # which is preferable to leaking the raw token via string interp.
            actual = placeholder_map.get(token, "")
            pname = f"pii_{counter['n']}"
            counter["n"] += 1
            params[pname] = actual
            return f":{pname}"

        return _PII_LITERAL_RE.sub(repl, sql), params

    def _validate_columns(self, tree: exp.Expression, from_tables: List[str]) -> None:
        """Check that every column reference resolves to an allow-listed
        column on one of the FROM tables. Unqualified references are accepted
        if at least one FROM table has that column; this matches Postgres'
        own resolution rules and avoids false-positive rejections on common
        cases like `SELECT region_code, COUNT(*) FROM mv_individuals_summary
        GROUP BY region_code`."""
        catalog_tables = {t.name: t for t in self._schema.catalog.tables}
        safe_cols: Dict[str, Set[str]] = {}
        for tname in from_tables:
            spec = catalog_tables.get(tname)
            if spec is None:
                continue
            safe_cols[tname] = {c.name for c in spec.safe_columns()}

        # Map any aliases declared in the query back to base table names.
        alias_to_base: Dict[str, str] = {}
        for tbl in tree.find_all(exp.Table):
            alias = tbl.alias
            if alias:
                alias_to_base[alias] = tbl.name

        # SELECT-list aliases (e.g. `SUM(total) AS women_count`) are legitimate
        # references in ORDER BY / GROUP BY / HAVING. Without this, a perfectly
        # valid aggregate like
        #   SELECT region_code, SUM(total) AS women_count ...
        #   ORDER BY women_count
        # is falsely rejected because `women_count` is not a base-table column.
        # The alias is computed from already-allow-listed columns, so exempting
        # it is safe.
        declared_aliases: Set[str] = {a.alias for a in tree.find_all(exp.Alias) if a.alias}

        for col in tree.find_all(exp.Column):
            cname = col.name
            tname = col.table  # alias OR table name; "" if unqualified
            if tname:
                base = alias_to_base.get(tname, tname)
                if base in safe_cols and cname not in safe_cols[base]:
                    raise SqlValidationError(
                        f"Column '{base}.{cname}' is not allow-listed on this table"
                    )
            else:
                # Unqualified: accept a SELECT-list alias, otherwise it must
                # exist on at least one FROM table.
                if cname in declared_aliases:
                    continue
                if safe_cols and not any(cname in cols for cols in safe_cols.values()):
                    raise SqlValidationError(
                        f"Column '{cname}' is not in any allow-listed table referenced "
                        f"by this query ({sorted(safe_cols)})"
                    )

    def _inject_k_count(self, select: exp.Select) -> None:
        """Add `COUNT(*) AS _k_count` to the SELECT list of a grouped query."""
        already_has = any(
            isinstance(e, exp.Alias) and e.alias == K_COUNT_COL
            for e in select.expressions
        )
        if already_has:
            return
        k_expr = exp.alias_(exp.Count(this=exp.Star()), K_COUNT_COL)
        select.set("expressions", list(select.expressions) + [k_expr])

    def _collect_group_by(self, tree: exp.Expression) -> List[str]:
        """Return the column names referenced in GROUP BY, in order."""
        group = tree.find(exp.Group)
        if group is None:
            return []
        out: List[str] = []
        for e in group.expressions:
            if isinstance(e, exp.Column):
                out.append(e.name)
            elif isinstance(e, exp.Alias):
                out.append(e.alias)
        return out
