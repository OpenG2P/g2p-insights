"""NL → Superset chart authoring — Flow B from the README."""

from __future__ import annotations

from fastapi import HTTPException
from openg2p_fastapi_common.controller import BaseController

from ..schemas.superset import (
    AuthorChartRequest,
    AuthorChartResponse,
    SaveChartRequest,
)
from ..services.cost_gate_service import CostGateService
from ..services.llm_service import Clarification, DirectAnswer, ExpertRedirect, LlmService, SqlPlan
from ..services.pii_service import PiiService
from ..services.query_executor_service import QueryExecutorService
from ..services.schema_service import SchemaService
from ..services.sql_validator_service import SqlValidationError, SqlValidatorService
from ..services.superset_service import SupersetService


class DashboardController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["dashboards"]

        self._pii = PiiService.get_component()
        self._schema = SchemaService.get_component()
        self._llm = LlmService.get_component()
        self._validator = SqlValidatorService.get_component()
        self._cost = CostGateService.get_component()
        self._exec = QueryExecutorService.get_component()
        self._superset = SupersetService.get_component()

        @self.router.post("/dashboards/charts/preview", response_model=AuthorChartResponse)
        async def preview(req: AuthorChartRequest) -> AuthorChartResponse:
            sanitized = self._pii.sanitize(req.question)
            schemas = await self._schema.retrieve_relevant(sanitized.text)
            plan = await self._llm.plan(sanitized.text, schemas)

            # Chart authoring only flows through `propose_sql`; if the LLM
            # picked any other tool we surface a 400 rather than guess.
            if isinstance(plan, (Clarification, DirectAnswer, ExpertRedirect)):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "The question doesn't translate to a chart query. "
                        "Try a more concrete data question (e.g. 'pie chart of gender')."
                    ),
                )
            sql_plan: SqlPlan = plan

            try:
                validated = self._validator.validate(sql_plan.sql, sanitized.placeholder_map)
            except SqlValidationError as exc:
                raise HTTPException(status_code=400, detail=f"Generated SQL was rejected: {exc}")

            cost = await self._cost.assess(validated)
            preview_rows = await self._exec.run(validated, preview=True)
            form_data = self._superset.build_form_data(validated.sql, sql_plan.viz)
            return AuthorChartResponse(
                status="preview",
                cost=cost,
                form_data=form_data,
                viz_spec=sql_plan.viz,
                preview_rows=preview_rows.rows,
                assumptions=sql_plan.assumptions,
                sql=validated.sql,
            )

        @self.router.post("/dashboards/charts/save", response_model=AuthorChartResponse)
        async def save(req: SaveChartRequest) -> AuthorChartResponse:
            chart_id = await self._superset.create_chart(
                name=req.name, form_data=req.form_data, dataset=req.dataset
            )
            if req.dashboard_id:
                await self._superset.attach_chart_to_dashboard(chart_id, req.dashboard_id)
            return AuthorChartResponse(status="saved", chart_id=chart_id)

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
