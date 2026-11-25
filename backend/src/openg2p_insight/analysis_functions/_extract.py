"""Shared extraction helper for analysis functions.

Loads a working set into a DataFrame and **normalises numeric types**. Postgres
`NUMERIC` / `DECIMAL` columns are returned by asyncpg as Python `Decimal`
objects, which pandas stores as `object` dtype. That breaks downstream code
that checks `is_numeric_dtype` (e.g. correlation's cardinality guard, which
otherwise treats PMT scores as a categorical with thousands of "values").
We coerce them to floats here once, so every analysis function downstream
sees clean dtypes.
"""

from __future__ import annotations

import decimal
from typing import List

import pandas as pd
from ..services.registry_db import registry_engine
from sqlalchemy import text

from ..services.schema_service import SchemaService


class InvalidAnalyticsTarget(ValueError):
    """Raised when an entity / column requested by an analysis isn't in the
    catalog allow-list. Surfaces as a clean `status: error` from the
    analytics service rather than a raw Postgres UndefinedColumnError 500."""


def _validate_target(entity: str, columns: List[str], filters: list[dict]) -> None:
    """Check the entity and every referenced column against the catalog.

    Two jobs:
      1. Friendly errors — "column 'pmt_score' is not in
         v_g2p_register_individuals_safe; available: …" instead of a 500.
      2. SQL-injection defence — `extract` interpolates entity + column names
         straight into the SELECT (they can't be bind params). Restricting
         them to the catalog's allow-listed names is what makes that safe.
    """
    schema = SchemaService.get_component()
    catalog = schema.catalog

    allowed_tables = set(catalog.safe_table_names())
    if entity not in allowed_tables:
        raise InvalidAnalyticsTarget(
            f"Entity '{entity}' is not an allow-listed view. "
            f"Available: {sorted(allowed_tables)}"
        )

    table = next((t for t in catalog.tables if t.name == entity), None)
    if table is None:
        raise InvalidAnalyticsTarget(f"Entity '{entity}' not found in catalog.")
    valid_cols = {c.name for c in table.safe_columns()}

    referenced = list(columns or [])
    referenced += [f["col"] for f in (filters or []) if "col" in f]
    bad = [c for c in referenced if c not in valid_cols]
    if bad:
        raise InvalidAnalyticsTarget(
            f"Column(s) {bad} not in {entity}. "
            f"Available columns: {sorted(valid_cols)}"
        )


async def extract(
    entity: str,
    columns: List[str],
    filters: list[dict],
    sample_size: int | None = None,
) -> pd.DataFrame:
    # Allow-list check BEFORE building SQL — friendly errors + injection guard.
    _validate_target(entity, columns, filters)

    cols_sql = ", ".join(columns) if columns else "*"
    where = ""
    params: dict = {}
    if filters:
        clauses = []
        for i, f in enumerate(filters):
            clauses.append(f"{f['col']} {f.get('op', '=')} :p{i}")
            params[f"p{i}"] = f["val"]
        where = "WHERE " + " AND ".join(clauses)

    limit = f"LIMIT {int(sample_size)}" if sample_size else ""
    sql = f"SELECT {cols_sql} FROM {entity} {where} {limit}".strip()

    # The REGISTRY database. The reporting views live there; the app's own
    # database holds sessions and the audit log and none of the registry's data.
    #
    # Expert Lab listed every view correctly — the catalog it reads is a file —
    # and then every function failed with `relation "fr_rpt_farmer" does not
    # exist`, which reads as a missing view rather than as the wrong connection.
    # The Ask tab's executor and cost gate were moved for the same reason; see
    # services/registry_db.py.
    engine = registry_engine()
    if engine is None:
        raise RuntimeError(
            "No registry database is configured for Insight — there is nothing "
            "to analyse. Set registryDB in the chart."
        )
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        df = pd.DataFrame(result.mappings().all())

    return _normalise_numeric(df)


def _normalise_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce object-typed columns whose non-null values are Decimal/int/float
    into proper numeric pandas dtypes. Leaves real categorical columns
    (strings, enums, booleans) alone."""
    if df.empty:
        return df
    for col in df.columns:
        if df[col].dtype != "object":
            continue
        sample = df[col].dropna().head(20)
        if sample.empty:
            continue
        if all(isinstance(v, (decimal.Decimal, int, float)) and not isinstance(v, bool) for v in sample):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
