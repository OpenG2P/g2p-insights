"""Health / ping / feature-capability controller."""

from openg2p_fastapi_common.controller import BaseController

from .. import __version__
from ..config import Settings

_config = Settings.get_config(strict=False)


class HealthController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["health"]

        @self.router.get("/health")
        async def health() -> dict:
            return {"status": "ok", "version": __version__}

        @self.router.get("/features")
        async def features() -> dict:
            """Capability flags the UI uses to decide which tabs to render.

            Deliberately free of secrets — the frontend shell calls this on
            boot to hide features that are switched off for this deployment.
            """
            return {
                # False => no outbound AI calls are possible; UI hides "Ask".
                "ai_enabled": bool(_config.llm.enabled),
                # Dashboards tab is only useful when a Superset URL is set.
                "dashboards_enabled": bool(_config.superset.url),
                # Maps tab embeds an Evidence.dev site; hidden if no URL set.
                "maps_enabled": bool(_config.maps.evidence_url),
                "version": __version__,
            }
