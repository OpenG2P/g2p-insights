"""NL → Superset chart authoring — Flow B from the README."""

from __future__ import annotations

from openg2p_fastapi_common.controller import BaseController

from ..schemas.superset import (
    AuthorChartRequest,
    AuthorChartResponse,
    SaveChartRequest,
)
from ..services.cost_gate_service import CostGateService
from ..services.llm_service import LlmService
from ..services.pii_service import PiiService
from ..services.query_executor_service import QueryExecutorService
from ..services.schema_service import SchemaService
from ..services.sql_planner_service import SqlPlannerService
from ..services.superset_service import SupersetService


class DashboardController(BaseController):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.router.tags = ["dashboards"]

        self._pii = PiiService.get_component()
        self._schema = SchemaService.get_component()
        self._llm = LlmService.get_component()
        self._planner = SqlPlannerService.get_component()
        self._cost = CostGateService.get_component()
        self._exec = QueryExecutorService.get_component()
        self._superset = SupersetService.get_component()

        @self.router.post("/dashboards/charts/preview", response_model=AuthorChartResponse)
        async def preview(req: AuthorChartRequest) -> AuthorChartResponse:
            sanitized = self._pii.sanitize(req.question)
            schemas = await self._schema.retrieve_relevant(sanitized.text)
            intent = await self._llm.plan_chart(sanitized.text, schemas)
            sql = self._planner.build(intent, sanitized.placeholder_map)
            cost = await self._cost.assess(sql)
            preview_rows = await self._exec.run(sql, group_by=intent.group_by, preview=True)
            form_data = self._superset.build_form_data(intent)
            return AuthorChartResponse(
                status="preview",
                cost=cost,
                form_data=form_data,
                viz_spec=intent.viz,
                preview_rows=preview_rows.rows,
                assumptions=intent.assumptions,
                sql=sql,
            )

        @self.router.post("/dashboards/charts/save", response_model=AuthorChartResponse)
        async def save(req: SaveChartRequest) -> AuthorChartResponse:
            chart_id = await self._superset.create_chart(
                name=req.name, form_data=req.form_data, dataset=req.dataset
            )
            if req.dashboard_id:
                await self._superset.attach_chart_to_dashboard(chart_id, req.dashboard_id)
            return AuthorChartResponse(status="saved", chart_id=chart_id)
