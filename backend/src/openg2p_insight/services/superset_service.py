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
        # Session cookies from form-based login. We tried JWT Bearer auth via
        # /api/v1/security/login but in Superset 4.x the JWT-authenticated
        # request is silently demoted to anonymous on list endpoints (every
        # `/api/v1/{resource}/` returns count=0 with a valid admin JWT,
        # while /api/v1/me/ returns 401). Form-login + cookie works.
        self._cookies: Optional[dict] = None

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
                cookies=self._cookies,
                headers={"X-CSRFToken": await self._csrf(client), "Referer": _config.superset.url},
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
                cookies=self._cookies,
                headers={"X-CSRFToken": await self._csrf(client), "Referer": _config.superset.url},
                json={"slices": [chart_id]},
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Superset attach chart {r.status_code}: {r.text}"
                )

    # ---------- internals ----------------------------------------------------

    async def _ensure_auth(self) -> None:
        if self._cookies:
            return
        async with httpx.AsyncClient(base_url=_config.superset.url, follow_redirects=False) as client:
            r = await client.post(
                "/login/",
                data={
                    "username": _config.superset.username,
                    "password": _config.superset.password,
                },
            )
            # FAB returns 302 on success, 200 on failure (re-rendered login).
            if r.status_code != 302:
                raise RuntimeError(f"Superset login failed: {r.status_code}")
            self._cookies = dict(r.cookies)

    async def _csrf(self, client: httpx.AsyncClient) -> str:
        r = await client.get("/api/v1/security/csrf_token/", cookies=self._cookies)
        if r.status_code >= 400:
            return ""
        return r.json().get("result", "")

    # ---------- embedding API -----------------------------------------------

    async def list_dashboards(self) -> list[dict]:
        """Return dashboards visible to the service account, including their
        embedded-UUID if embedding has been enabled. We list every dashboard;
        embed-status is shown so the UI can render "go to Superset to enable
        embedding" for ones that aren't yet embeddable."""
        await self._ensure_auth()
        async with httpx.AsyncClient(base_url=_config.superset.url) as client:
            r = await client.get(
                "/api/v1/dashboard/",
                cookies=self._cookies,
                params={"q": "(page_size:200)"},
            )
            r.raise_for_status()
            dashboards = r.json().get("result", [])

            out: list[dict] = []
            for d in dashboards:
                embedded_uuid = await self._get_embedded_uuid(d["id"], client)
                out.append({
                    "id": d["id"],
                    "uuid": d.get("uuid"),
                    "title": d.get("dashboard_title") or d.get("slug") or f"Dashboard {d['id']}",
                    "slug": d.get("url_slug"),
                    "status": d.get("status", "published"),
                    "embedded_uuid": embedded_uuid,
                    "changed_on": d.get("changed_on_delta_humanized"),
                })
            return out

    async def _get_embedded_uuid(
        self, dashboard_id: int, client: httpx.AsyncClient
    ) -> str | None:
        """Look up the embedded-UUID for a dashboard. Returns None if embedding
        hasn't been enabled."""
        r = await client.get(
            f"/api/v1/dashboard/{dashboard_id}/embedded",
            cookies=self._cookies,
        )
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            return None
        return r.json().get("result", {}).get("uuid")

    async def enable_embedding(
        self, dashboard_id: int, allowed_domains: list[str] | None = None
    ) -> str:
        """Toggle embedding on for a dashboard. Returns the embedded-UUID
        used by the SDK on the frontend."""
        await self._ensure_auth()
        async with httpx.AsyncClient(base_url=_config.superset.url) as client:
            r = await client.post(
                f"/api/v1/dashboard/{dashboard_id}/embedded",
                cookies=self._cookies,
                headers={"X-CSRFToken": await self._csrf(client), "Referer": _config.superset.url},
                json={"allowed_domains": allowed_domains or []},
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Superset enable-embedding {r.status_code}: {r.text}")
            return r.json()["result"]["uuid"]

    async def mint_guest_token(
        self,
        embedded_uuid: str,
        user_info: dict | None = None,
        rls_rules: list[dict] | None = None,
    ) -> str:
        """Mint a short-lived guest token scoped to a single embedded dashboard.

        The SDK calls our /embed-token endpoint, which in turn calls this.
        The token expires after a few minutes — the SDK auto-refreshes.

        `rls_rules` is the place to enforce per-user row-level scoping when
        Keycloak integration lands (e.g. {clause: "region_code = 'ET-OR'"}).
        Empty by default — every guest sees everything in the dashboard.
        """
        await self._ensure_auth()
        body = {
            "user": user_info or {
                "username": "insight-embed-guest",
                "first_name": "Insight",
                "last_name": "Embed",
            },
            "resources": [{"type": "dashboard", "id": embedded_uuid}],
            "rls": rls_rules or [],
        }
        async with httpx.AsyncClient(base_url=_config.superset.url) as client:
            r = await client.post(
                "/api/v1/security/guest_token/",
                cookies=self._cookies,
                headers={"X-CSRFToken": await self._csrf(client), "Referer": _config.superset.url},
                json=body,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Superset guest_token {r.status_code}: {r.text}")
            return r.json()["token"]

    # ---------- internals (chart authoring) ---------------------------------

    async def _resolve_dataset_id(self, dataset: str, client: httpx.AsyncClient) -> int:
        r = await client.get(
            "/api/v1/dataset/",
            cookies=self._cookies,
            params={"q": f'(filters:!((col:table_name,opr:eq,value:{dataset})))'},
        )
        r.raise_for_status()
        rows = r.json().get("result") or []
        if not rows:
            raise ValueError(f"Dataset '{dataset}' not found in Superset")
        return rows[0]["id"]
