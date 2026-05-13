"""Geo hierarchy endpoints — proxy to OpenG2P Master Data Service with cache."""

from __future__ import annotations

from typing import Optional

from openg2p_fastapi_common.controller import BaseController

from ..services.masterdata_service import MasterDataService


class GeoController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["geo"]

        self._mds = MasterDataService.get_component()

        @self.router.get("/geo/levels")
        async def levels(parent_level_id: Optional[str] = None) -> dict:
            return {"levels": await self._mds.get_levels(parent_level_id)}

        @self.router.get("/geo/values")
        async def values(level_id: str, parent_level_value_id: Optional[str] = None) -> dict:
            return {
                "values": await self._mds.get_level_values(
                    level_id=level_id, parent_level_value_id=parent_level_value_id
                )
            }
