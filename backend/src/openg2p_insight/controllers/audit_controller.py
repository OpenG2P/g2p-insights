"""Audit log read endpoints."""

from __future__ import annotations

from typing import Optional

from openg2p_fastapi_common.controller import BaseController

from ..services.audit_service import AuditService


class AuditController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["audit"]

        self._audit = AuditService.get_component()

        @self.router.get("/audit")
        async def list_audit(
            username: Optional[str] = None, limit: int = 100, offset: int = 0
        ) -> dict:
            return {"entries": await self._audit.list(username=username, limit=limit, offset=offset)}
