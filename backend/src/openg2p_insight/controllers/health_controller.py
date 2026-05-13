"""Health / ping controller."""

from openg2p_fastapi_common.controller import BaseController

from .. import __version__


class HealthController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["health"]

        @self.router.get("/health")
        async def health() -> dict:
            return {"status": "ok", "version": __version__}
