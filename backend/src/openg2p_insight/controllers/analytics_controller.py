"""Expert analytics — Flow C from the README. Dispatches to ANALYSIS_REGISTRY."""

from __future__ import annotations

from openg2p_fastapi_common.controller import BaseController

from ..schemas.analytics import AnalyticsRequest, AnalyticsResponse
from ..services.analytics_service import AnalyticsService


class AnalyticsController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["analytics"]

        self._analytics = AnalyticsService.get_component()

        @self.router.get("/analytics/functions")
        async def list_functions() -> dict:
            return {"functions": self._analytics.list_functions()}

        @self.router.post("/analytics/{function}", response_model=AnalyticsResponse)
        async def run(function: str, req: AnalyticsRequest) -> AnalyticsResponse:
            return await self._analytics.run(function, req)
