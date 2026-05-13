"""
Audit log writer + reader.

MVP writes to the local insight.audit_log table. The integration with
openg2p-audit-manager (CloudEvents over Kafka) is a follow-up.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from openg2p_fastapi_common.context import dbengine
from openg2p_fastapi_common.service import BaseService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..models.audit_log import AuditLog
from .cost_gate_service import CostReport

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class AuditService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)

    async def record_query(
        self,
        username: Optional[str],
        question: str,
        sql,
        cost: CostReport,
        rows: int,
    ) -> None:
        engine = dbengine.get()
        async with AsyncSession(engine) as session:
            entry = AuditLog(
                username=username or "anonymous",
                action="query",
                question=question,
                sql=str(sql),
                cost_level=cost.level.value,
                estimated_rows=cost.estimated_rows,
                actual_rows=rows,
            )
            session.add(entry)
            await session.commit()

    async def list(
        self, username: Optional[str] = None, limit: int = 100, offset: int = 0
    ) -> List[dict]:
        engine = dbengine.get()
        async with AsyncSession(engine) as session:
            stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit).offset(offset)
            if username:
                stmt = stmt.where(AuditLog.username == username)
            result = await session.execute(stmt)
            return [self._to_dict(r) for r in result.scalars().all()]

    @staticmethod
    def _to_dict(entry: AuditLog) -> dict:
        return {
            "id": entry.id,
            "username": entry.username,
            "action": entry.action,
            "question": entry.question,
            "sql": entry.sql,
            "cost_level": entry.cost_level,
            "estimated_rows": entry.estimated_rows,
            "actual_rows": entry.actual_rows,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
