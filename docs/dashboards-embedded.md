# Embedded Superset dashboards

Insight embeds Superset dashboards in-place inside the `/dashboards` page —
users stay in Insight while interacting with full Superset dashboards
(filters, drill-downs, exports). Authoring still happens in Superset, but
viewing and using dashboards never requires leaving the app.

This is the standard Superset embedding pattern: short-lived guest tokens
minted by our backend, rendered by `@superset-ui/embedded-sdk` in an iframe.

---

## How it fits together

```
Insight UI                    Insight backend             Superset
──────────                    ───────────────             ────────
List dashboards
  GET /dashboards   ────────► SupersetService
                              .list_dashboards()  ──────► GET /api/v1/dashboard/
                                                         GET /api/v1/dashboard/{id}/embedded
                              ◄─────────────────────────────  list + embedded_uuid

Click "Open here"
  SupersetEmbed renders
  fetchGuestToken hook fires
  POST /dashboards/{uuid}/
       embed-token        ─►  SupersetService
                              .mint_guest_token()  ────►  POST /api/v1/security/guest_token/
                                                                {resources, user, rls}
                              ◄──────────────────────────  {token: "..."}
  ◄──────────────  {token}
  iframe loaded by SDK
  using the token            (browser ──►  Superset directly with the token)
```

Notes:
- Listing always goes via the backend (so the Superset admin password
  stays server-side).
- Once the SDK has the guest token, the iframe talks to Superset directly
  via CORS. The token expires after ~5 minutes; the SDK auto-refreshes by
  calling our `/embed-token` endpoint again.

---

## One-time setup per dashboard

Each Superset dashboard must have **embedding enabled** before it shows
correctly in Insight. Two paths:

### From inside Insight (recommended)

1. Open `/dashboards` in Insight. You'll see every dashboard the service
   account can see, tagged either **Embed-ready** or **Embed not enabled**.
2. For an "Embed not enabled" dashboard, click **Enable embedding**.
3. Insight calls `POST /api/v1/dashboard/{id}/embedded` under the hood,
   captures the new `embedded_uuid`, and navigates straight to the embedded
   view.

### From Superset directly

1. Go to the Superset dashboard.
2. Menu (`…`) → **Embed dashboard** → toggle on.
3. Optionally add `allowed_domains` (the origin of your Insight frontend).
4. Copy the embedded UUID — it's then visible in the Insight dashboard list
   on next refresh.

---

## Backend endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/v1/insight/dashboards` | List dashboards visible to the configured Superset service account. Returns `[{id, title, uuid, embedded_uuid, status, …}]`. |
| `POST` | `/v1/insight/dashboards/{dashboard_id}/enable-embedding` | Idempotently enable embedding on a dashboard. Returns `{embedded_uuid}`. |
| `POST` | `/v1/insight/dashboards/{embedded_uuid}/embed-token` | Mint a short-lived guest token scoped to one embedded dashboard. Returns `{token, embedded_uuid}`. Called by the SDK on its own schedule. |

All three are thin wrappers over `SupersetService` in
[backend/src/openg2p_insight/services/superset_service.py](../backend/src/openg2p_insight/services/superset_service.py).

---

## Frontend pieces

| File | What it does |
|------|--------------|
| [components/SupersetEmbed.tsx](../frontend/src/components/SupersetEmbed.tsx) | Wraps `@superset-ui/embedded-sdk`'s `embedDashboard`. Mounts the iframe, wires `fetchGuestToken` to our backend, handles errors. |
| [pages/Dashboards.tsx](../frontend/src/pages/Dashboards.tsx) | List view (`/dashboards`) + embedded detail view (`/dashboards/:embeddedUuid`). Existing "Add chart from NL" flow lives here too. |
| [api/client.ts](../frontend/src/api/client.ts) | `listDashboards()`, `enableEmbedding(id)`, `fetchEmbedToken(uuid)`. |

---

## Configuration

### Superset side (one-time, per deployment)

`test-stack/superset/superset_config.py` already enables embedding for dev:

```python
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_RBAC": True,
}
ENABLE_CORS = True
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "origins": ["*"],          # tighten in production
}

# Guest tokens map to the "Public" role. Public has NO permissions by
# default — embedded dashboards load the layout but every chart 403s.
# In dev we mirror Gamma's read-only perms onto Public:
PUBLIC_ROLE_LIKE = "Gamma"

# Lets the embed iframe set its session cookie across origins in dev.
TALISMAN_ENABLED = False
SESSION_COOKIE_SAMESITE = None
SESSION_COOKIE_SECURE = False
GUEST_ROLE_NAME = "Public"
GUEST_TOKEN_JWT_EXP_SECONDS = 300
```

**After changing any of the above, re-derive role permissions:**

```bash
docker exec insight-test-superset superset init
```

That re-applies `PUBLIC_ROLE_LIKE` and any other role config to the
metadata DB. Without this, the Public role stays empty even though the
config says otherwise.

For production:
- Set `CORS_OPTIONS["origins"]` to the explicit Insight frontend origin(s).
- Don't use `PUBLIC_ROLE_LIKE = "Gamma"` — give Public only the specific
  datasets you want embeddable. Or use a dedicated `"Embed"` role and set
  `GUEST_ROLE_NAME = "Embed"`.
- Re-enable `SESSION_COOKIE_SECURE = True` (HTTPS-only).

### Insight side

`backend/.env` (or Helm values for K8s) — same vars we already use for
chart authoring:

```bash
INSIGHT_SUPERSET__URL=http://localhost:8088
INSIGHT_SUPERSET__USERNAME=admin
INSIGHT_SUPERSET__PASSWORD=admin
```

The frontend reads `VITE_SUPERSET_URL` to know which Superset host the
embed iframe should talk to.

---

## Row-Level Security (deferred to Keycloak)

The backend's `mint_guest_token` accepts `rls_rules: list[dict]` — empty
today. When Keycloak integration lands, the backend will look up the
logged-in user's regions/roles and pass them in:

```python
await self._superset.mint_guest_token(
    embedded_uuid,
    user_info={"username": kc_user.email, ...},
    rls_rules=[
        {"clause": f"region_code IN ({','.join(kc_user.allowed_regions)})"}
    ],
)
```

Until then, every embedded view shows everything the dashboard's underlying
datasets contain — i.e. the same data anyone with the Superset URL could see.
This matches the existing single-tenant security model.

---

## Common issues

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Iframe shows "Could not load embedded dashboard" | Embedding not enabled on that dashboard | Click **Enable embedding** in the list, or enable it from Superset |
| Iframe is blank with `403` in browser network tab | CORS blocked the request | Add your Insight frontend origin to Superset's `CORS_OPTIONS.origins` |
| `Guest token request failed` | The Superset service account doesn't have permission to mint guest tokens | In Superset → Settings → List Roles → ensure the admin role has `can guest token on Security` |
| Embedded dashboard renders but charts show "Cannot load data" | Anonymous role doesn't have access to the underlying datasets | In Superset → Settings → List Roles → ensure the "Public" role can `can read on Dataset` for the relevant tables (or use RLS instead) |
| Some dashboards missing from the list | The service account doesn't have permission to view them | Add the dashboards to the service account's owned/shared list, or give the role broader read access |
| Backend `GET /v1/insight/dashboards` returns `{"dashboards": [], "error": "..."}` | Superset is down, mis-configured URL, or wrong credentials | Check `INSIGHT_SUPERSET__*` env vars; verify `curl http://<superset>/health` |

---

## What we deliberately don't do

- **We don't reimplement Superset's UI.** Filters, time pickers, drill-down
  menus all come from Superset's iframe. We're trying to keep the user in
  Insight; we're not trying to replace Superset's BI surface.
- **We don't expose the Superset admin password to the browser.** Listing
  and token minting both go through the backend, which has the password as
  an env var.
- **We don't store guest tokens.** They're minted per-iframe-load and
  forgotten. The SDK manages refresh.
- **We don't proxy chart data through Insight.** Once the SDK has a token,
  the iframe talks to Superset directly. Insight is just the bootstrap.

---

## Future work

- **RLS per Keycloak user** — see the section above; deferred to v2.
- **Per-dashboard role gating** — currently every Insight user can see every
  dashboard the service account sees. Role-based filtering can be added when
  Keycloak lands.
- **In-page edit** — Superset 4.x has an `EMBEDDED_INTERACTIVE_DASHBOARDS`
  flag that lets some edits happen through the iframe. We don't enable it
  today; users go to Superset for authoring.
- **Dashboard search / pinning** — for deployments with many dashboards, a
  quick search and "pinned" section in the list would help. The current list
  is unfiltered.
