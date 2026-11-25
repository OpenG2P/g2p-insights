#!/usr/bin/env python3
"""
Seed a minimal, working, EMBEDDED dashboard into the local Superset so the
Insight "Dashboards" tab has something to show.

AUTH NOTE: Superset's JWT Bearer auth returns an *anonymous* identity for the
REST API here (POST/GET behave as the Public role — writes get no owner and
rolled back, lists return count 0). The web form login at /login/ sets a real
admin session cookie that DOES work. The backend's superset_service.py hit the
same wall and uses the same workaround. So we form-login, then use the session
cookie + CSRF header for every API call — exactly like the backend.

Creates (owned by admin): registry DB (sync queries, no Celery needed), a
dataset on the synthetic aggregate view `mv_district_aggregates`, two charts,
one dashboard, then enables embedding. Pass --clean to first delete any
existing dashboards/charts/datasets/dbs so the demo starts from a clean slate.

Usage: python seed_superset.py [--clean] [base_url] [admin] [password]
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

argv = [a for a in sys.argv[1:] if not a.startswith("--")]
CLEAN = "--clean" in sys.argv
BASE = (argv[0] if len(argv) > 0 else "http://localhost:8088").rstrip("/")
USER = argv[1] if len(argv) > 1 else "admin"
PASS = argv[2] if len(argv) > 2 else "admin"

# Registry DB as reachable from INSIDE the Superset container (compose network).
REGISTRY_URI = "postgresql+psycopg2://postgres:postgres@postgres:5432/registry"
DATASET_TABLE = "mv_district_aggregates"   # aggregate, no PII


class SS:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            _NoRedirect(),
        )
        self.csrf = None

    def _raw(self, method, path, body=None, headers=None, form=False):
        url = f"{BASE}{path}"
        if body is None:
            data = None
        elif form:
            data = urllib.parse.urlencode(body).encode()
        else:
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        if form:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        elif data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            return self.opener.open(req)
        except urllib.error.HTTPError as e:
            # FAB signals a successful form login with a 302 to "/". The cookie
            # jar has already captured Set-Cookie, so a redirect is not an error.
            if e.code in (301, 302):
                return e
            raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()[:400]}")

    def _json(self, method, path, body=None):
        h = {"Referer": BASE}
        if self.csrf:
            h["X-CSRFToken"] = self.csrf
        resp = self._raw(method, path, body, headers=h)
        return json.loads(resp.read().decode() or "{}")

    def login(self):
        # Form login -> real admin session cookie (302 on success).
        resp = self._raw("POST", "/login/", {"username": USER, "password": PASS}, form=True)
        if resp.status not in (302, 200):
            raise RuntimeError(f"login status {resp.status}")
        if not any(c.name == "session" for c in self.jar):
            raise RuntimeError("no session cookie after /login/ — bad credentials?")
        self.csrf = json.loads(
            self._raw("GET", "/api/v1/security/csrf_token/").read().decode()
        )["result"]
        print("logged in (session cookie + csrf)")

    def _list_ids(self, kind):
        r = self._json("GET", f"/api/v1/{kind}/?q=(page_size:200)")
        return [row["id"] for row in r.get("result", [])]

    def clean(self):
        for kind in ("dashboard", "chart", "dataset", "database"):
            ids = self._list_ids(kind)
            for i in ids:
                try:
                    self._json("DELETE", f"/api/v1/{kind}/{i}")
                except RuntimeError as e:
                    print(f"  (skip {kind} {i}: {e})")
            print(f"cleaned {kind}: {len(ids)}")

    def db(self):
        r = self._json("POST", "/api/v1/database/", {
            "database_name": "registry",
            "sqlalchemy_uri": REGISTRY_URI,
            "expose_in_sqllab": True,
            "allow_run_async": False,
        })
        print("database id", r["id"])
        return r["id"]

    def dataset(self, db_id):
        r = self._json("POST", "/api/v1/dataset/", {
            "database": db_id, "schema": "public", "table_name": DATASET_TABLE,
        })
        ds = r["id"]
        # Ensure columns are introspected so metric-based charts validate.
        self._json("PUT", f"/api/v1/dataset/{ds}/refresh", {})
        print("dataset id", ds)
        return ds

    def chart(self, ds_id, name, viz, params):
        params = {**params, "datasource": f"{ds_id}__table", "viz_type": viz}
        r = self._json("POST", "/api/v1/chart/", {
            "slice_name": name, "viz_type": viz,
            "datasource_id": ds_id, "datasource_type": "table",
            "params": json.dumps(params),
        })
        print("chart", name, "->", r["id"])
        return r["id"]

    def dashboard(self, title, chart_ids):
        pos = {
            "DASHBOARD_VERSION_KEY": "v2",
            "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
            "GRID_ID": {"type": "GRID", "id": "GRID_ID",
                        "children": [f"ROW-{i}" for i in range(len(chart_ids))],
                        "parents": ["ROOT_ID"]},
        }
        for i, cid in enumerate(chart_ids):
            row, ch = f"ROW-{i}", f"CHART-{i}"
            pos[row] = {"type": "ROW", "id": row, "children": [ch],
                        "parents": ["ROOT_ID", "GRID_ID"],
                        "meta": {"background": "BACKGROUND_TRANSPARENT"}}
            pos[ch] = {"type": "CHART", "id": ch, "children": [],
                       "parents": ["ROOT_ID", "GRID_ID", row],
                       "meta": {"chartId": cid, "width": 6, "height": 50, "uuid": f"chart-{cid}"}}
        r = self._json("POST", "/api/v1/dashboard/", {
            "dashboard_title": title, "published": True,
            "position_json": json.dumps(pos),
        })
        dash_id = r["id"]
        # position_json alone doesn't link the slices — associate each chart with
        # the dashboard, else the embedded renderer shows "no chart definition".
        for cid in chart_ids:
            self._json("PUT", f"/api/v1/chart/{cid}", {"dashboards": [dash_id]})
        print("dashboard id", dash_id)
        return dash_id

    def enable_embed(self, dash_id):
        r = self._json("POST", f"/api/v1/dashboard/{dash_id}/embedded", {"allowed_domains": []})
        uuid = r.get("result", {}).get("uuid")
        print("embedded uuid", uuid)
        return uuid


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface 302 instead of following it


def main():
    ss = SS()
    ss.login()
    if CLEAN:
        ss.clean()
    db = ss.db()
    ds = ss.dataset(db)
    c1 = ss.chart(ds, "Beneficiaries by region", "echarts_timeseries_bar", {
        "x_axis": "region_code",
        "metrics": [{"expressionType": "SIMPLE",
                     "column": {"column_name": "total"}, "aggregate": "SUM",
                     "label": "beneficiaries"}],
        "groupby": [], "row_limit": 100,
    })
    c2 = ss.chart(ds, "Beneficiaries by gender", "pie", {
        "groupby": ["gender"],
        "metric": {"expressionType": "SIMPLE",
                   "column": {"column_name": "total"}, "aggregate": "SUM",
                   "label": "beneficiaries"},
        "row_limit": 100,
    })
    dash = ss.dashboard("Registry Overview", [c1, c2])
    ss.enable_embed(dash)
    print("\nDONE — 'Registry Overview' created + embedded (dashboard id "
          f"{dash}).")


if __name__ == "__main__":
    main()
