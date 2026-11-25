# Connecting Insight to the new registry

You just finished onboarding (whether via [`insight-onboard`](insight-onboard.md)
or the [manual runbook](onboarding-new-registry.md)). Now you need to take a
backend that's been running against the test-stack DB (or has never run
against this registry) and point it at the new safe layer. This doc covers
both paths:

| Path | When | Doc section |
|------|------|-------------|
| **Local-dev** — `dev_run.sh` on your laptop | Recommended first: smoke-test the catalog + role + DB connectivity before deploying anything | [§ Local-dev](#local-dev-path) |
| **K8s** — Helm-deployed backend | Production / staging deployment | [§ K8s](#kubernetes-path) |

Both paths need the same three artefacts from onboarding:

| Artefact | Where it came from |
|---|---|
| `catalog.yaml` | `insight-onboard apply --catalog-out catalog.yaml` (or hand-written if you took the manual path) |
| `values-onboard.yaml` | `insight-onboard apply --helm-out values-onboard.yaml` |
| `insight_ai` role + password | Printed once to stdout by `insight-onboard apply` — save it now if you haven't |

If you don't have these, run `insight-onboard apply` first, or jump to the
[manual runbook](onboarding-new-registry.md).

---

## What "connecting to the new registry" actually means

The backend uses a single Postgres connection. That connection talks to a DB
that has two schemas the backend cares about:

| Schema | Owner | Purpose | Created by |
|--------|-------|---------|------------|
| `insight_safe` (default; configurable in the proposal) | the DBA who ran `apply` | Holds the PII-safe views the LLM-generated SQL queries | `insight-onboard apply` |
| `insight` | the Postgres role the backend runs as | Holds chat sessions, audit log, saved-chart metadata | Alembic on first backend startup |

So "connecting to the new registry" means three concrete things:

1. The backend's `DB_*` env vars point at **the registry Postgres** (where
   `insight_safe` was just created), not at the test stack.
2. The backend reads the **operational catalog** that
   `insight-onboard apply` wrote (so `safe_views`, enum values, mixin
   attributions, geo columns, etc. match what's actually in the DB).
3. The Postgres role the backend connects as can both **SELECT from
   `insight_safe.*`** (that's the `insight_ai` role onboarding made for
   you) **and CREATE/INSERT in the `insight` schema** (which the role
   doesn't have today — see the [known gap below](#known-gap-insight_ai-cant-write-to-the-insight-schema)).

---

## Known gap — `insight_ai` can't write to the `insight` schema

The `insight_ai` role that `insight-onboard apply` creates is intentionally
read-only: it has `USAGE` on `insight_safe` and `SELECT` on each safe view.
That's exactly right for analytical queries.

But the backend ALSO needs to write its own state (chat sessions, audit
trail, saved charts) into the `insight` schema. With only the onboarding
grants, Alembic's first migration fails on `CREATE SCHEMA insight` —
`insight_ai` doesn't have schema-create privilege.

**Two workarounds until the onboarding tool handles this directly:**

A. **Bootstrap as the admin role first, then run as `insight_ai`** (recommended):

```bash
# Step 1: run Alembic ONCE as the registry admin.
cd backend
DB_HOST=<registry-host> DB_PORT=5432 DB_NAME=<registry_db> \
  DB_USER=<dba-user> DB_PASSWORD=<dba-password> \
  .venv/bin/alembic upgrade head

# Step 2: grant insight_ai full access to the now-created insight schema.
psql "postgresql://<dba-user>:<dba-password>@<registry-host>:5432/<registry_db>" <<'SQL'
GRANT USAGE ON SCHEMA insight TO insight_ai;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA insight TO insight_ai;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA insight TO insight_ai;
ALTER DEFAULT PRIVILEGES IN SCHEMA insight
  GRANT ALL PRIVILEGES ON TABLES TO insight_ai;
ALTER DEFAULT PRIVILEGES IN SCHEMA insight
  GRANT USAGE, SELECT ON SEQUENCES TO insight_ai;
SQL
```

After that one-time bootstrap, the backend runs as `insight_ai` normally
and Alembic future migrations work because the role now has the right
privileges.

B. **Run the backend as the admin role** (simpler but loses the PII
boundary at the role level — only the validator stops you from querying
raw PII tables). Use only in dev / when you trust your validator and
prompt-injection defences.

This whole step is a TODO for the onboarding tool — it should emit the
`insight` schema bootstrap + grants as part of `apply`. Tracked in
[insight-onboard.md § Limitations / roadmap](insight-onboard.md#limitations--roadmap).

---

## Local-dev path

Use this to smoke-test the catalog + DB connectivity from your laptop
before pushing to K8s. Assumes `dev_run.sh` already works against the
test stack.

### TL;DR — three terminals

The backend reads its catalog from a **directory** named by
`INSIGHT_CATALOG_PATH`. That directory must contain a file called
`schema.yaml` (the backend looks for that exact name; the onboarding
tool's `--catalog-out` flag lets you write the file with the right name
in the right place — no rename needed):

```bash
# One-time, per registry — write the catalog directly into the dir you'll
# point INSIGHT_CATALOG_PATH at. Pick any directory name you like; the
# filename inside MUST be schema.yaml.
mkdir -p $HOME/insight-catalogs/<country>
python -m insight_onboard apply \
    --registry-db postgresql://dba:***@<registry-host>:5432/<registry_db> \
    --catalog-out $HOME/insight-catalogs/<country>/schema.yaml \
    --helm-out    $HOME/insight-catalogs/<country>/values-insight.yaml \
    /tmp/proposal.yaml

# Terminal 1: backend, pointed at your registry.
cd backend
cat > .env <<EOF
# === switch from test-stack → your registry ===
DB_HOST=<registry-host>
DB_PORT=5432
DB_NAME=<registry_db>
DB_USER=insight_ai
DB_PASSWORD=<insight_ai-password-from-onboarding>
INSIGHT_CATALOG_PATH=$HOME/insight-catalogs/<country>

# === (everything below: LLM, Superset, etc. — copy from .env.example) ===
INSIGHT_LLM__PROVIDER=openai_compatible
INSIGHT_LLM__BASE_URL=https://openrouter.ai/api/v1
INSIGHT_LLM__API_KEY=sk-or-...
INSIGHT_LLM__MODEL=qwen/qwen3-32b
EOF

./scripts/dev_run.sh            # backend up on http://localhost:8000

# Terminal 2: frontend.
cd frontend
npm run dev                     # UI on http://localhost:5173

# Terminal 3: smoke-test.
curl http://localhost:8000/v1/insight/catalog | jq '.tables | length'   # should match safe_views count
open http://localhost:5173      # use the Ask tab against your registry
```

One-time prereqs you only do the first time you switch to a new
registry: the `insight` schema bootstrap (see
[Known gap](#known-gap-insight_ai-cant-write-to-the-insight-schema)).

**Why a directory and not a file?** The backend also looks in the same
dir for two optional files: `pii_recognizers.yaml` (country-specific
Presidio patterns) and `viz_templates.yaml` (visualisation defaults).
If you don't have them, the backend just runs without them — no error.

The rest of this section is the same five steps spelled out, with
options + troubleshooting.

### 1. Put the catalog where the backend will read it

The backend reads a directory named by `INSIGHT_CATALOG_PATH`. Inside
that directory it expects:

| File | Required? | What it is |
|------|-----------|------------|
| `schema.yaml` | yes | The operational catalog (tables, columns, PII tags, enum values, safe_views allow-list) |
| `pii_recognizers.yaml` | no | Country-specific Presidio regex patterns. Backend runs without it. |
| `viz_templates.yaml` | no | Visualisation template defaults. Backend runs without it. |

By default `dev_run.sh` sets `INSIGHT_CATALOG_PATH` to
`test-stack/postgres/catalog/`, which is why the test stack works out of
the box. For your registry, pick a fresh directory and have onboarding
write directly into it — no rename:

```bash
mkdir -p ~/insight-catalogs/<country>
python -m insight_onboard apply \
    --catalog-out ~/insight-catalogs/<country>/schema.yaml \
    --helm-out    ~/insight-catalogs/<country>/values-insight.yaml \
    /tmp/proposal.yaml \
    --registry-db postgresql://dba:***@<registry-host>:5432/<registry_db>
```

You'll point `INSIGHT_CATALOG_PATH` at `~/insight-catalogs/<country>`
in step 2 below. Switching between registries later is just an env-var
change — no need to overwrite the test-stack baseline.

> **Why does the filename have to be `schema.yaml` specifically?**
> The backend hardcodes that filename in
> [services/schema_service.py](../backend/src/openg2p_insight/services/schema_service.py)
> (`Path(catalog_path) / "schema.yaml"`). The onboarding tool's
> `--catalog-out` flag is just an output path — pass the full path
> including the filename to skip the copy/rename step that earlier
> versions of this doc described.

### 2. Override DB + catalog env vars in `backend/.env`

`dev_run.sh` already sources `backend/.env` if present. The lines you
need (most are commented out in `.env.example` — uncomment + fill):

```bash
# ---- DB / catalog (this is the "switch to new registry" block) -----
DB_HOST=<registry-host>            # e.g. localhost if SSH-tunneled, or the actual hostname
DB_PORT=5432
DB_NAME=<registry_db>              # the registry database name
DB_USER=insight_ai                 # role created by insight-onboard apply
DB_PASSWORD=<insight_ai-password>  # printed once by insight-onboard apply
INSIGHT_CATALOG_PATH=/Users/you/insight-catalogs/<country>
```

The full env var reference is in [`.env.example`](../backend/.env.example).

### 3. Bootstrap the `insight` schema (one-time)

See the [Known gap section above](#known-gap-insight_ai-cant-write-to-the-insight-schema).
TL;DR: run Alembic once as the admin, then grant `insight_ai` write on
the `insight` schema. Skip this only if you're going with workaround B
(running as admin).

### 4. Restart `dev_run.sh`

```bash
cd backend
./scripts/dev_run.sh
```

Verify the startup line shows the new DB and catalog:

```
[dev_run] catalog: /Users/you/insight-catalogs/<country>
[dev_run] db:      <registry-host>:5432/<registry_db>
[dev_run] starting uvicorn with --reload (Ctrl-C to stop)
```

### 5. Smoke-test against the running backend

```bash
# 1. Catalog loaded?
curl http://localhost:8000/v1/insight/catalog | jq '.tables | length'
# Should print the number of safe views the catalog declares.

# 2. Trivial ask works?
curl -X POST http://localhost:8000/v1/insight/ask \
    -H 'content-type: application/json' \
    -d '{"question":"How many individuals?"}'
# Should return rows. SQL in the response references insight_safe.v_*_safe.

# 3. PII really gated?
curl -X POST http://localhost:8000/v1/insight/ask \
    -H 'content-type: application/json' \
    -d '{"question":"Show me individuals with names containing John"}'
# Should clarify or refuse. `full_name` is not in any safe_view.
```

If any of these fail, jump to [§ Troubleshooting](#troubleshooting).

### 6. (Optional) Switching back to the test stack

Comment out the `DB_*` and `INSIGHT_CATALOG_PATH` lines in `backend/.env`
and re-run `dev_run.sh`. The defaults bring you back to the test stack
on localhost:5433.

---

## Kubernetes path

This applies the `values-onboard.yaml` overlay that
`insight-onboard apply --helm-out` wrote, layered on top of the umbrella
`values.yaml`.

> **Heads-up — Helm chart gap.** [helm/g2p-insight/values.yaml](../helm/g2p-insight/values.yaml)
> declares `global.registryDBHost/registryDBPort/registryDBName` but the
> backend's `envVars.DB_*` actually consume `global.insightDB*` (the
> chart's own DB-provisioning block). So an overlay that ONLY sets
> `registryDBHost` is silently ignored. Until the chart is fixed, you
> have to override the `insightDB*` vars to mean "the registry DB" — see
> the values block below. Tracked as a follow-up.

### 1. Make sure the catalog + Helm overlay are in your deployment repo

`insight-onboard apply` wrote two files; both go into your country-specific
values directory next to where you keep other Helm overlays:

```
deploy-<country>/
  catalog.yaml             # operational catalog (was --catalog-out)
  values-insight.yaml      # Helm overlay (was --helm-out, with edits below)
  ... (other component overlays)
```

### 2. Edit `values-insight.yaml` for the Helm-chart workaround

Paste the catalog body into `insightBackend.appCatalog.schemaYaml`, and
override the *misleadingly-named* `insightDB*` globals so the backend
connects to the registry DB:

```yaml
# values-insight.yaml
global:
  insightHostname: insight.<env>.openg2p.org

  # === Chart-gap workaround: these vars are named "insightDB*" but they
  # === control what the BACKEND connects to. Point them at the registry DB.
  postgresqlHost: <registry-postgres-host>
  insightDB:     <registry_db>               # the registry database name
  insightDBPort: 5432
  insightDBUser: insight_ai                  # the role from `insight-onboard apply`
  insightDBSecret: g2p-insight-registry-creds
  insightDBUserPasswordKey: insight-ai-password

  # External services
  supersetUrl: https://superset.<env>.openg2p.org
  masterDataServiceUrl: https://masterdata.<env>.openg2p.org

  # LLM
  llmModel: anthropic/claude-sonnet-4.5                  # or qwen3-32b for sovereign deploys
  anthropicApiKeySecret: g2p-insight-llm
  anthropicApiKeyKey: anthropic-api-key

# Disable the chart's own DB-provisioning block — the registry DB already
# exists; we just connect to it.
postgres-init:
  enabled: false

insightBackend:
  appCatalog:
    schemaYaml: |
      # PASTE the contents of catalog.yaml here
    piiRecognizersYaml: |
      recognizers:
        # Country-specific PII patterns (extend default_pii_rules.yaml if needed)
        - entity: FAYDA_TOKEN
          patterns:
            - {name: fayda_numeric, regex: '\b\d{12}\b', score: 0.6}
          context: [fayda, token]
```

### 3. Create the K8s secret with the `insight_ai` password

```bash
kubectl create secret generic g2p-insight-registry-creds \
    --namespace insight-<country> \
    --from-literal=insight-ai-password='<the-password-from-onboarding>'
```

Plus the LLM key:

```bash
kubectl create secret generic g2p-insight-llm \
    --namespace insight-<country> \
    --from-literal=anthropic-api-key='sk-ant-...'
```

### 4. Bootstrap the `insight` schema in the registry DB

See [Known gap](#known-gap-insight_ai-cant-write-to-the-insight-schema).
Easiest from a bastion host that can reach the registry Postgres:

```bash
# Run Alembic once as the registry admin
git clone <this repo> && cd g2p-insight/backend
pip install -e .
DB_HOST=<registry-host> DB_PORT=5432 DB_NAME=<registry_db> \
  DB_USER=<dba-user> DB_PASSWORD=<dba-password> \
  alembic upgrade head

# Grant insight_ai write on the now-existing insight schema
psql "postgresql://<dba>:<pw>@<host>:5432/<registry_db>" -c "
  GRANT USAGE ON SCHEMA insight TO insight_ai;
  GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA insight TO insight_ai;
  GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA insight TO insight_ai;
  ALTER DEFAULT PRIVILEGES IN SCHEMA insight GRANT ALL PRIVILEGES ON TABLES TO insight_ai;
  ALTER DEFAULT PRIVILEGES IN SCHEMA insight GRANT USAGE, SELECT ON SEQUENCES TO insight_ai;
"
```

### 5. Deploy

```bash
helm upgrade --install g2p-insight ./helm/g2p-insight \
    -f deploy-<country>/values-insight.yaml \
    --namespace insight-<country> --create-namespace
```

Wait for the rollout:

```bash
kubectl -n insight-<country> rollout status deploy/insight-backend
```

### 6. Smoke-test the live service

Same three checks as local-dev, just against the deployed hostname:

```bash
HOST=insight.<env>.openg2p.org

curl https://$HOST/v1/insight/catalog | jq '.tables | length'

curl -X POST https://$HOST/v1/insight/ask \
    -H 'content-type: application/json' \
    -d '{"question":"How many individuals?"}'

curl -X POST https://$HOST/v1/insight/ask \
    -H 'content-type: application/json' \
    -d '{"question":"Show me individuals with names containing John"}'
```

### 7. Final audit

Re-run `insight-onboard audit` (from anywhere with network access to the
registry) to confirm `insight_ai` still can't see PII:

```bash
python -m insight_onboard audit \
    --registry-db postgresql://<dba>:<pw>@<registry-host>:5432/<registry_db> \
    --proposal deploy-<country>/proposal.yaml
```

Wire this into CI to re-check after every deploy.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Backend startup log: `schema.yaml not found at … — running with empty catalog` | `INSIGHT_CATALOG_PATH` points at a dir that doesn't contain `schema.yaml` | Make sure you copied `catalog.yaml` to `<INSIGHT_CATALOG_PATH>/schema.yaml` (note the rename — backend looks for that exact filename) |
| `curl /v1/insight/catalog` returns `tables: []` | Catalog loaded but empty, or backend hasn't picked up the new file | Check the backend log line `Loaded catalog with N tables`. If N=0, the YAML is malformed or empty. If N>0 but the API returns 0, the backend is still running with the old catalog — restart |
| `psycopg.OperationalError: connection failed` on startup | `DB_*` wrong, or registry DB unreachable | `psql "postgresql://insight_ai:<pw>@<host>:<port>/<db>" -c "SELECT 1"` from the backend host; fix whichever bit doesn't work |
| Alembic crashes on first startup: `permission denied for schema public` or `permission denied to create schema "insight"` | `insight_ai` lacks privileges to bootstrap the insight schema | Run the workaround in [Known gap](#known-gap-insight_ai-cant-write-to-the-insight-schema): bootstrap Alembic as admin once, then grant |
| Backend up, but `/ask` returns `Table 'mv_individuals_summary' is not in the allow-listed safe views` | Catalog's `safe_views` doesn't match what the SQL validator sees. Often: the LLM emitted a table name that isn't in this deployment's catalog | Check the catalog's `safe_views` list matches the views actually created by `apply`. Rerun `insight-onboard scan` if the registry changed since onboarding |
| Backend up, `/ask` returns rows but they all say `<suppressed>` | k-anonymity is suppressing — typical when grouping by a high-cardinality column (kebele) with little data | Either lower `k_anonymity` in the catalog (regulatory-permitting) or have the user group at a coarser level |
| Backend up, but `/ask` returns `permission denied for view v_individuals_safe` | `insight_ai` was created in a different schema, or grants didn't apply to the view the catalog references | Re-run `insight-onboard apply` (it `DROP ROLE; CREATE ROLE` + re-grants), or hand-add the missing GRANT |
| K8s rollout stuck on `postgresCheckerInit` initContainer | The init container is trying `psql` against the chart's old `insightDB` defaults | You overrode the chart-gap workaround correctly above? Check `kubectl describe pod` and verify `DB_HOST/DB_NAME/DB_USER` point at your registry |
| First-time deploy works, but a later registry migration added new columns and they're not visible in Insight | Catalog is stale | Re-run `insight-onboard scan` (extension-led catches new columns automatically), regenerate `catalog.yaml`, `helm upgrade` to update the ConfigMap |

---

## See also

- [insight-onboard.md](insight-onboard.md) — the CLI that produces the
  catalog and Helm overlay you just deployed
- [onboarding-new-registry.md](onboarding-new-registry.md) — the full
  end-to-end runbook this doc picks up where step 7 left off
- [pii-strategy.md](pii-strategy.md) — the layered PII model that the
  `insight_safe` schema + `insight_ai` role + catalog allow-list together
  enforce
- [`helm/g2p-insight/values.yaml`](../helm/g2p-insight/values.yaml) — the
  full chart values reference (mind the `insightDB*` naming gap noted above)
