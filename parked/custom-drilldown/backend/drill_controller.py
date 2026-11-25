"""
Hierarchical drill-down endpoints — the drill mechanic Superset lacks.

  GET  /drill/hierarchy   — the deployment's level chain, root first
  POST /drill             — aggregate one level, optionally scoped to an
                            ancestor path

Country-agnostic by construction: levels, their names and their depth all come
from Master Data Service; the level -> column mapping comes from the catalog.
"""

from __future__ import annotations

from fastapi import HTTPException
from openg2p_fastapi_common.controller import BaseController

from ..schemas.drill import DrillRequest, DrillResponse, HierarchyResponse
from ..services.drill_service import DrillError, DrillService


class DrillController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["drill"]

        self._drill = DrillService.get_component()

        @self.router.get("/drill/hierarchy", response_model=HierarchyResponse)
        async def hierarchy() -> HierarchyResponse:
            """Level chain for this deployment, root first.

            The UI uses this to label breadcrumbs and to know when to stop
            offering drill — it must not assume any particular depth.
            """
            try:
                return HierarchyResponse(levels=await self._drill.get_hierarchy())
            except DrillError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

        @self.router.post("/drill", response_model=DrillResponse)
        async def drill(req: DrillRequest) -> DrillResponse:
            try:
                return await self._drill.drill(req)
            except DrillError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
