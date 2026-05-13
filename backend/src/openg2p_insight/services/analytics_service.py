"""
Expert analytics dispatcher.

Holds a registry of named statistical functions, dispatches incoming requests,
and packages the result with a viz_spec. The functions themselves are in
analysis_functions/.
"""

from __future__ import annotations

import logging
from typing import List

from openg2p_fastapi_common.service import BaseService

from ..analysis_functions import ANALYSIS_REGISTRY
from ..config import Settings
from ..schemas.analytics import AnalyticsRequest, AnalyticsResponse

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class AnalyticsService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)

    def list_functions(self) -> List[dict]:
        return [
            {"name": name, "doc": fn.__doc__ or ""}
            for name, fn in ANALYSIS_REGISTRY.items()
        ]

    async def run(self, function: str, req: AnalyticsRequest) -> AnalyticsResponse:
        if function not in ANALYSIS_REGISTRY:
            return AnalyticsResponse(
                status="error",
                error=f"Unknown function '{function}'. Available: {list(ANALYSIS_REGISTRY)}",
            )
        fn = ANALYSIS_REGISTRY[function]
        result = await fn(req)
        return AnalyticsResponse(status="ok", **result)
