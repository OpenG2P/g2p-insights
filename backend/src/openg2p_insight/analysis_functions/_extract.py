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
from openg2p_fastapi_common.context import dbengine
from sqlalchemy import text


async def extract(
    entity: str,
    columns: List[str],
    filters: list[dict],
    sample_size: int | None = None,
) -> pd.DataFrame:
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

    engine = dbengine.get()
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
