"""Shared extraction helper for analysis functions."""

from __future__ import annotations

from typing import List

import pandas as pd
from openg2p_fastapi_common.context import dbengine
from sqlalchemy import text


async def extract(entity: str, columns: List[str], filters: list[dict], sample_size: int | None = None) -> pd.DataFrame:
    """Pull a working set into a DataFrame. Schema-safe identifier checks happen
    upstream in the SQL planner; for the analytics path we trust the function-
    registry callers to pass entity/column names that came from the catalog."""
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
        return pd.DataFrame(result.mappings().all())
