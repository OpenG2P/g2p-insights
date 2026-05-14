"""
Expert analytics dispatcher.

Holds a registry of named statistical functions, dispatches incoming requests
with a hard wall-clock timeout, logs step-level progress, and packages the
result with a viz_spec.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List

from openg2p_fastapi_common.service import BaseService

from ..analysis_functions import ANALYSIS_REGISTRY
from ..config import Settings
from ..schemas.analytics import AnalyticsRequest, AnalyticsResponse

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)

# Wall-clock cap on any single analysis function. The DB role's
# statement_timeout (30s) bounds the SQL extract; this bounds the whole
# end-to-end including pandas / scipy work.
_ANALYSIS_TIMEOUT_SECONDS = 60


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
        _logger.info(
            "[analytics] start function=%s entity=%s columns=%s params=%s",
            function, req.entity, req.columns or req.column, req.params,
        )
        started = time.time()

        try:
            async with asyncio.timeout(_ANALYSIS_TIMEOUT_SECONDS):
                result = await fn(req)
        except asyncio.TimeoutError:
            elapsed = time.time() - started
            _logger.warning(
                "[analytics] TIMEOUT function=%s after %.1fs", function, elapsed,
            )
            return AnalyticsResponse(
                status="error",
                error=(
                    f"Analysis '{function}' exceeded the {_ANALYSIS_TIMEOUT_SECONDS}s "
                    f"timeout (ran {elapsed:.1f}s). Try reducing the column count, "
                    "narrowing filters, or setting a smaller sample_size."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - started
            _logger.exception("[analytics] failed function=%s after %.1fs", function, elapsed)
            return AnalyticsResponse(
                status="error",
                error=f"{function} failed: {exc!s}",
            )

        elapsed = time.time() - started
        _logger.info("[analytics] done  function=%s in %.2fs", function, elapsed)
        return AnalyticsResponse(status="ok", **result)
