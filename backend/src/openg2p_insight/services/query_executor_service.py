"""
Query Executor.

Runs the planner's SQL against Postgres using the read-only `insight_ai` role,
then applies k-anonymity suppression on results and strips the planner's
internal `_k_count` column from the response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from openg2p_fastapi_common.context import dbengine
from openg2p_fastapi_common.service import BaseService
from sqlalchemy import text

from ..config import Settings
from ..utils.k_anonymity import K_COUNT_COL, apply_k_anonymity
from .sql_planner_service import PlannedSql

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


@dataclass
class ExecResult:
    rows: List[dict] = field(default_factory=list)
    k_suppressions: int = 0


class QueryExecutorService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)

    async def run(
        self,
        planned: PlannedSql,
        group_by: Optional[List[str]] = None,
        preview: bool = False,
    ) -> ExecResult:
        sql = planned.sql
        if preview and "limit" not in sql.lower():
            sql = f"{sql} LIMIT 100"

        engine = dbengine.get()
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), planned.params)
            rows = [dict(r._mapping) for r in result]

        suppressions = 0
        if group_by and planned.has_k_count:
            rows, suppressions = apply_k_anonymity(
                rows, group_by=group_by, k=_config.k_anonymity.default_k
            )

        # Always strip the planner-internal count column from the response.
        for r in rows:
            r.pop(K_COUNT_COL, None)

        return ExecResult(rows=rows, k_suppressions=suppressions)
