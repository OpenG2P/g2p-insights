"""
Query Executor.

Runs the validator's SQL against Postgres using the read-only `insight_ai`
role, applies k-anonymity suppression on grouped results, and strips the
internal `_k_count` column from the response.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from openg2p_fastapi_common.context import dbengine
from openg2p_fastapi_common.service import BaseService
from sqlalchemy import text

from ..config import Settings
from ..utils.k_anonymity import K_COUNT_COL, apply_k_anonymity
from .sql_validator_service import ValidatedSql

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
        validated: ValidatedSql,
        preview: bool = False,
    ) -> ExecResult:
        sql = validated.sql
        if preview and "limit" not in sql.lower():
            sql = f"{sql} LIMIT 100"

        engine = dbengine.get()
        async with engine.connect() as conn:
            result = await conn.execute(text(sql), validated.params)
            rows = [dict(r._mapping) for r in result]

        suppressions = 0
        if validated.has_k_count and validated.group_by_columns:
            rows, suppressions = apply_k_anonymity(
                rows,
                group_by=validated.group_by_columns,
                k=_config.k_anonymity.default_k,
            )

        # Always strip the validator-internal count column from the response.
        for r in rows:
            r.pop(K_COUNT_COL, None)

        return ExecResult(rows=rows, k_suppressions=suppressions)
