"""Superset dashboard embedding.

The natural-language chart-authoring routes (/dashboards/charts/preview and
/save) were removed along with the "Add chart from natural language" button.
Authoring a chart worth keeping needs the full Superset editor — metric
definitions, cross-filters, time grains — and a one-shot LLM translation
produced charts that looked plausible and then had to be rebuilt by hand. The
SupersetService methods they used (build_form_data, create_chart,
attach_chart_to_dashboard) are deliberately kept: they still work, and bringing
the feature back is a controller change rather than a rewrite.

Ad-hoc natural-language querying still exists — that is the Ask tab, which
answers questions directly instead of persisting a chart.
"""

from __future__ import annotations

from openg2p_fastapi_common.controller import BaseController

from ..services.superset_service import SupersetService


class DashboardController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["dashboards"]

        self._superset = SupersetService.get_component()

        # -------- Embed-in-place endpoints ----------------------------------

        @self.router.get("/dashboards")
        async def list_dashboards() -> dict:
            """List dashboards visible to the configured Superset service
            account. Each item includes `embedded_uuid` (or null if embedding
            hasn't been enabled — see /dashboards/{id}/enable-embedding)."""
            try:
                items = await self._superset.list_dashboards()
            except Exception as exc:  # noqa: BLE001
                return {"dashboards": [], "error": str(exc)}
            return {"dashboards": items}

        @self.router.post("/dashboards/{dashboard_id}/enable-embedding")
        async def enable_embedding(dashboard_id: int) -> dict:
            """Idempotently enable embedding on a Superset dashboard. Returns
            the embedded-UUID the frontend SDK needs. Operator typically does
            this once per dashboard; this endpoint exists so the UI can do it
            with a click instead of forcing a trip to Superset."""
            allowed_domains: list[str] = []  # empty = open; tighten in K8s deploys
            embedded_uuid = await self._superset.enable_embedding(
                dashboard_id, allowed_domains=allowed_domains
            )
            return {"embedded_uuid": embedded_uuid}

        @self.router.post("/dashboards/{embedded_uuid}/embed-token")
        async def embed_token(embedded_uuid: str) -> dict:
            """Mint a short-lived Superset guest token scoped to one embedded
            dashboard. The frontend's @superset-ui/embedded-sdk calls this on
            its own schedule (tokens have ~5-min lifetime)."""
            token = await self._superset.mint_guest_token(embedded_uuid)
            return {"token": token, "embedded_uuid": embedded_uuid}
