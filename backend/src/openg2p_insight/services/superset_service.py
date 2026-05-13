"""
Superset REST client.

Authenticates against the configured Superset instance and creates / attaches
charts based on Intent-derived form_data.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from .llm_service import Intent

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


_VIZ_TO_SUPERSET = {
    "table": "table",
    "bar": "dist_bar",
    "line": "line",
    "pie": "pie",
    "donut": "pie",
    "map": "deck_polygon",
    "histogram": "histogram",
    "metric_card": "big_number_total",
}


class SupersetService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._token: Optional[str] = None

    # ---------- public API ---------------------------------------------------

    def build_form_data(self, intent: Intent) -> dict:
        viz_type = _VIZ_TO_SUPERSET.get(intent.viz.type, "table")
        return {
            "viz_type": viz_type,
            "datasource_type": "table",
            "groupby": intent.group_by,
            "metric": intent.metric,
            "row_limit": intent.limit or 1000,
            "x_axis": intent.viz.x,
            "y_axis": intent.viz.y,
            "title": intent.viz.title,
            **intent.viz.extra,
        }

    async def create_chart(self, name: str, form_data: dict, dataset: str) -> int:
        await self._ensure_auth()
        async with httpx.AsyncClient(base_url=_config.superset.url) as client:
            # Superset's /api/v1/chart/ wants `params` as a JSON-stringified
            # blob (Marshmallow validates it as a string field), not as a
            # nested dict. Drop None values — they trip up the validator too.
            cleaned = {k: v for k, v in form_data.items() if v is not None}
            payload = {
                "slice_name": name,
                "viz_type": form_data.get("viz_type", "table"),
                "datasource_id": await self._resolve_dataset_id(dataset, client),
                "datasource_type": "table",
                "params": json.dumps(cleaned),
            }
            r = await client.post(
                "/api/v1/chart/",
                headers={**self._auth_header(), "X-CSRFToken": await self._csrf(client)},
                json=payload,
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Superset chart create {r.status_code}: {r.text}"
                )
            return r.json()["id"]

    async def attach_chart_to_dashboard(self, chart_id: int, dashboard_id: int) -> None:
        await self._ensure_auth()
        async with httpx.AsyncClient(base_url=_config.superset.url) as client:
            r = await client.put(
                f"/api/v1/dashboard/{dashboard_id}",
                headers={**self._auth_header(), "X-CSRFToken": await self._csrf(client)},
                json={"slices": [chart_id]},
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Superset attach chart {r.status_code}: {r.text}"
                )

    # ---------- internals ----------------------------------------------------

    async def _ensure_auth(self) -> None:
        if self._token:
            return
        async with httpx.AsyncClient(base_url=_config.superset.url) as client:
            r = await client.post(
                "/api/v1/security/login",
                json={
                    "username": _config.superset.username,
                    "password": _config.superset.password,
                    "provider": _config.superset.auth_provider,
                    "refresh": True,
                },
            )
            r.raise_for_status()
            self._token = r.json()["access_token"]

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    async def _csrf(self, client: httpx.AsyncClient) -> str:
        r = await client.get("/api/v1/security/csrf_token/", headers=self._auth_header())
        if r.status_code >= 400:
            return ""
        return r.json().get("result", "")

    async def _resolve_dataset_id(self, dataset: str, client: httpx.AsyncClient) -> int:
        r = await client.get(
            "/api/v1/dataset/",
            headers=self._auth_header(),
            params={"q": f'(filters:!((col:table_name,opr:eq,value:{dataset})))'},
        )
        r.raise_for_status()
        rows = r.json().get("result") or []
        if not rows:
            raise ValueError(f"Dataset '{dataset}' not found in Superset")
        return rows[0]["id"]
