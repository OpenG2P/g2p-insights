"""
Superset REST client.

Authenticates against the configured Superset instance and creates / attaches
charts derived from the SQL the LLM emitted.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx
import sqlglot
from openg2p_fastapi_common.service import BaseService
from sqlglot import exp

from ..config import Settings
from .llm_service import VizSpec

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


# Superset 6.x dropped the legacy NVD3 plugin, which is where `dist_bar` and
# `line` came from. A chart saved with one of those viz types still creates
# fine and its /chart/data call still returns rows — it just renders blank,
# because the frontend has no plugin registered under that key. Map onto the
# ECharts equivalents, which are what 6.x actually ships.
_VIZ_TO_SUPERSET = {
    "table": "table",
    "bar": "echarts_timeseries_bar",
    "line": "echarts_timeseries_line",
    "pie": "pie",
    "donut": "pie",
    "map": "deck_polygon",
    "histogram": "histogram",
    "metric_card": "big_number_total",
}

# The ECharts viz types take `metrics` (a list) plus an explicit `x_axis`,
# where the NVD3 ones took a single `metric` + `groupby`. Handing an ECharts
# chart the legacy shape produces an empty chart, so the two need different
# form_data — see build_form_data below.
_ECHARTS_VIZ = {"echarts_timeseries_bar", "echarts_timeseries_line"}


def _as_metric(metric: str):
    """Wrap an extracted aggregate so Superset can actually resolve it.

    A bare string in form_data is looked up against the dataset's *saved*
    metrics. Datasets get exactly one of those by default (`count`), so an
    expression pulled off the SQL — "sum(total)", "avg(monthly_income)" —
    resolves to nothing and /chart/data 400s. Anything other than the `count`
    fallback therefore has to go as an adhoc metric."""
    if not metric or metric == "count":
        return "count"
    return {
        "expressionType": "SQL",
        "sqlExpression": metric,
        "label": metric,
        "hasCustomLabel": False,
    }


class _Auth:
    """How we are authenticated to Superset: a JWT, a session cookie, or both.

    Which mechanism is even *routed* depends on Superset's AUTH_TYPE, so both
    are supported and tried in order:

    * **JWT** from ``POST /api/v1/security/login``. Works under every AUTH_TYPE,
      because that endpoint authenticates against Superset's own user table and
      never consults an external IdP. It is the only option under AUTH_OAUTH.
    * **Session cookie** from ``POST /login/`` — the FAB login form. Present
      only under AUTH_DB. Under AUTH_OAUTH that view is GET-only (it redirects
      to the identity provider) and a POST to it returns **405**.

    The consequence worth spelling out: under AUTH_OAUTH the account configured
    here must be a Superset-LOCAL service account. A Keycloak user, however
    privileged, cannot authenticate a machine client — `provider=db` does not
    look in Keycloak, and FAB only turns an OAuth authorization *code* into a
    session through the browser callback, so an IdP access token is not accepted
    by the REST API either.
    """

    def __init__(self) -> None:
        self.token: Optional[str] = None
        self.cookies: Optional[dict] = None

    def headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if extra:
            h.update(extra)
        return h


class SupersetService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._auth: Optional[_Auth] = None

    # ---------- public API ---------------------------------------------------

    def build_form_data(self, sql: str, viz: VizSpec) -> dict:
        """Best-effort translation of an LLM-emitted SELECT + viz spec into a
        Superset chart form_data dict. We parse the SQL with sqlglot to pull
        out GROUP BY columns and the first aggregate expression — enough for
        bar / pie / metric_card charts on a single dataset.

        Charts that need richer specs (multiple metrics, cross-filters, time
        windows) are still best authored in Superset directly."""
        chart = _extract_chart_shape(sql)
        viz_type = _VIZ_TO_SUPERSET.get(viz.type, "table")
        form = {
            "viz_type": viz_type,
            "datasource_type": "table",
            "row_limit": chart["limit"] or 1000,
            "title": viz.title,
        }

        metric = _as_metric(chart["metric"])

        if viz_type in _ECHARTS_VIZ:
            # x_axis is the category column; anything left in groupby would be
            # read as a series split, so drop the column we've used for the
            # axis to avoid charting each bar as its own series.
            x_axis = viz.x or (chart["groupby"][0] if chart["groupby"] else None)
            form.update({
                "x_axis": x_axis,
                "metrics": [metric],
                "groupby": [g for g in chart["groupby"] if g != x_axis],
            })
        else:
            form.update({
                "groupby": chart["groupby"],
                "metric": metric,
                "x_axis": viz.x,
                "y_axis": viz.y,
            })

        form.update(viz.extra)
        return form

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
            # The CSRF token MUST be fetched before the cookies are read.
            #
            # Arguments are evaluated left to right, so writing
            #     cookies=self._ck,
            #     headers=self._hdr({"X-CSRFToken": await self._csrf(client)}),
            # reads self._ck FIRST — before _csrf() has run. And _csrf() is
            # precisely what obtains the session this token is bound to: Superset
            # sets it on the csrf_token response. So the request went out with a
            # valid token and the cookies from BEFORE it existed, and Superset
            # answered "400 The CSRF session token is missing" — a message about
            # the token, for a fault in the cookie, caused by the order two
            # keyword arguments happen to be written in.
            csrf = await self._csrf(client)
            r = await client.post(
                "/api/v1/chart/",
                cookies=self._ck,
                headers=self._hdr({"X-CSRFToken": csrf,
                                    "Referer": _config.superset.url}),
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
            # Fetched first: see mint_guest_token. `cookies=self._ck` is
            # evaluated before the headers, so an inline _csrf() call sends
            # the token with the cookies from before its session existed.
            csrf = await self._csrf(client)
            r = await client.put(
                f"/api/v1/dashboard/{dashboard_id}",
                cookies=self._ck,
                headers=self._hdr({"X-CSRFToken": csrf,
                                   "Referer": _config.superset.url}),
                json={"slices": [chart_id]},
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Superset attach chart {r.status_code}: {r.text}"
                )

    # ---------- internals ----------------------------------------------------

    async def _ensure_auth(self, force: bool = False) -> None:
        """Authenticate, reusing the cached credentials unless `force`.

        Superset's access token EXPIRES. Caching it for the life of the process
        meant the Dashboards tab worked for the first few minutes after a restart
        and then answered 401 for ever, which reads as a permissions problem and
        is not one. `force=True` discards the cached token so the caller can
        retry once — see `_retry_once_on_401`.
        """
        if self._auth and not force:
            return
        if force:
            self._auth = None
        auth = _Auth()
        cfg = _config.superset
        async with httpx.AsyncClient(base_url=cfg.url, follow_redirects=False) as client:
            api = await client.post(
                "/api/v1/security/login",
                json={
                    "username": cfg.username,
                    "password": cfg.password,
                    "provider": cfg.auth_provider,
                    "refresh": True,
                },
            )
            if api.status_code == 200:
                auth.token = api.json().get("access_token")
                if await self._token_is_effective(client, auth):
                    self._auth = auth
                    return
                # A token that authenticates but is demoted to anonymous is
                # worse than no token: list endpoints answer 200 with count=0,
                # so the UI shows an empty dashboard list and nothing anywhere
                # reports an error. Fall through to the session flow instead.
                _logger.warning(
                    "Superset accepted the API login but the JWT is not effective "
                    "(/api/v1/me/ rejects it); falling back to session login"
                )
                auth.token = None

            form = await client.post(
                "/login/",
                data={"username": cfg.username, "password": cfg.password},
            )
            # FAB returns 302 on success, 200 on failure (re-rendered login).
            if form.status_code == 302:
                auth.cookies = dict(form.cookies)
                self._auth = auth
                return

            raise RuntimeError(self._auth_failure_hint(api, form))

    @staticmethod
    def _auth_failure_hint(api: httpx.Response, form: httpx.Response) -> str:
        """Explain *why* login failed, rather than surfacing a bare status code.

        The 405/401 pair is the signature of a Superset behind OAuth being
        handed credentials it cannot use, which is not something a reader would
        guess from "login failed: 405".
        """
        if form.status_code == 405:
            detail = (
                "POST /login/ returned 405, which means Superset is not using "
                "AUTH_DB — under AUTH_OAUTH the login view only redirects to the "
                "identity provider. "
            )
            if api.status_code == 401:
                detail += (
                    "The API login was also rejected (401). Note that "
                    "/api/v1/security/login authenticates against Superset's OWN "
                    "user table, not the identity provider: configure "
                    "INSIGHT_SUPERSET__USERNAME/PASSWORD with a Superset-local "
                    "service account (see the dashboards job's svc-importer), not "
                    "a Keycloak user."
                )
            else:
                detail += f"The API login returned {api.status_code}: {api.text[:200]}"
            return "Superset login failed. " + detail
        return (
            f"Superset login failed: API login {api.status_code}, "
            f"form login {form.status_code}: {form.text[:200]}"
        )

    async def _token_is_effective(self, client: httpx.AsyncClient, auth: _Auth) -> bool:
        """Confirm the JWT actually carries the service account's identity.

        Superset 4.x has been observed accepting a valid admin JWT and then
        treating the request as anonymous on list endpoints — every
        /api/v1/{resource}/ answers 200 with count=0 while /api/v1/me/ returns
        401. Probing /api/v1/me/ distinguishes "authenticated" from "silently
        anonymous" before any real call trusts the token.
        """
        r = await client.get("/api/v1/me/", headers=auth.headers())
        if r.status_code == 200:
            return True
        # /api/v1/me/ is not conclusive on its own. Some Superset configurations
        # reject it for a JWT that is perfectly good on the resource endpoints —
        # observed on 4.0.1 under AUTH_OAUTH, where /api/v1/me/ answers 401 while
        # /api/v1/dashboard/ answers 200 with the real count. Treating that as
        # "token not effective" sent every call down the session-login fallback,
        # which cannot work over an in-cluster http URL because Superset sets
        # SESSION_COOKIE_SECURE — so the Dashboards tab showed nothing at all.
        #
        # So confirm against a resource endpoint before giving up. A 401 here is
        # a real rejection; a 200 means the token works, whatever /me/ says.
        d = await client.get("/api/v1/dashboard/", headers=auth.headers())
        return d.status_code == 200

    @property
    def _ck(self) -> Optional[dict]:
        return self._auth.cookies if self._auth else None

    def _hdr(self, extra: Optional[dict] = None) -> dict:
        return self._auth.headers(extra) if self._auth else (extra or {})

    async def _csrf(self, client: httpx.AsyncClient) -> str:
        r = await client.get(
            "/api/v1/security/csrf_token/", headers=self._hdr(), cookies=self._ck
        )
        if r.status_code >= 400:
            return ""
        # Superset binds the CSRF token to the session it sets on this response,
        # so that cookie has to travel with the request that spends the token —
        # otherwise every mutating call fails CSRF validation.
        if r.cookies and self._auth:
            self._auth.cookies = {**(self._auth.cookies or {}), **dict(r.cookies)}
        return r.json().get("result", "")

    # ---------- embedding API -----------------------------------------------

    async def list_dashboards(self) -> list[dict]:
        """Return dashboards visible to the service account, including their
        embedded-UUID if embedding has been enabled. We list every dashboard;
        embed-status is shown so the UI can render "go to Superset to enable
        embedding" for ones that aren't yet embeddable."""
        await self._ensure_auth()
        async with httpx.AsyncClient(base_url=_config.superset.url) as client:
            async def _fetch():
                return await client.get(
                    "/api/v1/dashboard/",
                    cookies=self._ck,
                    headers=self._hdr(),
                    params={"q": "(page_size:200)"},
                )

            r = await _fetch()
            if r.status_code == 401:
                # The cached token has expired. Re-authenticate once and retry
                # rather than surfacing a 401 the operator cannot act on — it
                # looks like a permissions problem and is only a stale token.
                await self._ensure_auth(force=True)
                r = await _fetch()
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
            cookies=self._ck,
            headers=self._hdr(),
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
            # Fetched first: see mint_guest_token. `cookies=self._ck` is
            # evaluated before the headers, so an inline _csrf() call sends
            # the token with the cookies from before its session existed.
            csrf = await self._csrf(client)
            r = await client.post(
                f"/api/v1/dashboard/{dashboard_id}/embedded",
                cookies=self._ck,
                headers=self._hdr({"X-CSRFToken": csrf,
                                   "Referer": _config.superset.url}),
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
            # The CSRF token MUST be fetched before the cookies are read.
            #
            # Arguments are evaluated left to right, so writing
            #     cookies=self._ck,
            #     headers=self._hdr({"X-CSRFToken": await self._csrf(client)}),
            # reads self._ck FIRST — before _csrf() has run. And _csrf() is
            # precisely what obtains the session this token is bound to: Superset
            # sets it on the csrf_token response. So the request went out with a
            # valid token and the cookies from BEFORE it existed, and Superset
            # answered "400 The CSRF session token is missing" — a message about
            # the token, for a fault in the cookie, caused by the order two
            # keyword arguments happen to be written in.
            csrf = await self._csrf(client)
            r = await client.post(
                "/api/v1/security/guest_token/",
                cookies=self._ck,
                headers=self._hdr({"X-CSRFToken": csrf,
                                    "Referer": _config.superset.url}),
                json=body,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"Superset guest_token {r.status_code}: {r.text}")
            return r.json()["token"]

    # ---------- internals (chart authoring) ---------------------------------

    async def _resolve_dataset_id(self, dataset: str, client: httpx.AsyncClient) -> int:
        r = await client.get(
            "/api/v1/dataset/",
            cookies=self._ck,
            headers=self._hdr(),
            params={"q": f'(filters:!((col:table_name,opr:eq,value:{dataset})))'},
        )
        r.raise_for_status()
        rows = r.json().get("result") or []
        if not rows:
            raise ValueError(f"Dataset '{dataset}' not found in Superset")
        return rows[0]["id"]


_AGG_FN = (exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count)


def _extract_chart_shape(sql: str) -> dict:
    """Pull GROUP BY columns, the first aggregate metric, and LIMIT out of
    a SELECT for Superset form_data. Returns sensible empties when sqlglot
    can't parse — the chart can still save, just less informatively."""
    out = {"groupby": [], "metric": "count", "limit": None}
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(tree, exp.Select):
        return out

    group = tree.find(exp.Group)
    if group is not None:
        out["groupby"] = [e.name for e in group.expressions if isinstance(e, exp.Column)]

    for expr in tree.expressions:
        inner = expr.this if isinstance(expr, exp.Alias) else expr
        if isinstance(inner, _AGG_FN):
            # e.g. "AVG(monthly_income)" → just record the function name; the
            # caller stores the SQL verbatim for the source of truth.
            out["metric"] = inner.sql(dialect="postgres").lower()
            break

    if tree.args.get("limit") is not None:
        try:
            out["limit"] = int(tree.args["limit"].expression.this)
        except Exception:  # noqa: BLE001
            pass
    return out
