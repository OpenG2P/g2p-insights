# Onboarding a new registry

End-to-end recipe for pointing Insight at a new registry database. You have
two paths to the same outcome:

| Path | When to pick | Time | Doc |
|------|--------------|------|-----|
| **Automated** — `insight-onboard` CLI | Default. Works for any Postgres registry; OpenG2P registries get extra automation when you point at the extension models. | ~30 min | [docs/insight-onboard.md](insight-onboard.md) |
| **Manual** — DBA runbook (this doc) | When the CLI doesn't fit (highly non-standard schema, no Postgres admin available, want to learn what the CLI does under the hood, or need to review every generated SQL line by hand for compliance sign-off) | Half a day per registry | this document |

Both paths produce the same artifacts: PII-safe views in a dedicated
`insight_safe` schema, an `insight_ai` Postgres role with SELECT-only
grants, an operational catalog YAML, and a Helm values overlay.

If you're not sure which path to take, **start with the automated CLI**.
Run `insight-onboard scan` and review the proposal YAML in your editor
before applying — you get the safety of full visibility without writing
SQL by hand.

The rest of this document is the hand-execution path. Read it top to
bottom; each step has copy-pasteable SQL or YAML. Roles needed: one DBA
with admin on the registry Postgres, one platform engineer to deploy
Helm.

---

## Side-by-side: the CLI vs. the manual steps

Each manual step below has a one-line CLI equivalent. If you're using the
CLI but want to know exactly what it's doing, the mapping is:

| Manual step | CLI subcommand | What the CLI does for you |
|-------------|----------------|---------------------------|
| 1. Inventory PII columns | `insight-onboard scan` | Heuristic + (optional) extension + (optional) LLM classification of every column; writes a reviewable proposal YAML |
| 2. Create PII-safe views | `insight-onboard apply` (DDL section) | Emits `CREATE SCHEMA insight_safe`, then `CREATE VIEW insight_safe.v_<table>_safe AS SELECT <safe cols> FROM public.<table>` |
| 3. Create `insight_ai` role + grants | `insight-onboard apply` (role section) | Generates role with random 32-char password, grants USAGE on `insight_safe` only, `SELECT` per view, sets `search_path` |
| 4. Write the catalog YAML | `insight-onboard apply --catalog-out` | Emits a fully populated `catalog.yaml` from the proposal — enum values from the extension when provided |
| 5. Configure Helm | `insight-onboard apply --helm-out` | Emits a Helm values overlay scaffold; you fill in `registryDBHost` and run `helm upgrade` |
| 6. Smoke-test | manual | curl probes are the same either way |
| 7. Document the deployment | manual | catalog YAML + Helm overlay are your record |
| Final PII audit | `insight-onboard audit` | `SET ROLE insight_ai`, list every visible column, diff against the proposal's PII list |

---

## Pre-requisites (both paths)

- The new registry's Postgres reachable from the K8s cluster.
- The registry's table schema (DDL or SQLAlchemy models).
- For OpenG2P registries: read access to the extension source tree
  (e.g. `nsr-extension`) and `openg2p-registry-core` — the CLI's
  `--registry-extension` flag uses these.
- A list — even a partial one — of which columns hold PII vs which are
  safe. (The CLI builds this draft for you; the manual path has you
  build it from scratch.)
- (Optional) An OpenG2P Master Data Service URL for that country's geo
  hierarchy. Skip if your registry already has region/zone/woreda codes
  baked in.

---

## Step 1 — Inventory PII columns

Walk the registry's columns and tag each one:

| Tag | Meaning | Example |
|-----|---------|---------|
| `pii`   | Directly identifies a person | full_name, phone, fayda_token, national_id, exact_dob, address, gps_coords |
| `quasi` | Cannot identify alone, but combines with others to re-identify | age (exact), gender, ethnicity, religion, marital_status, kebele-level location |
| `safe`  | Pure analytical fields | region_code, age_bracket, pmt_score, livelihood_type, monthly_income, head_count |

### → CLI shortcut

```bash
# Heuristic-only (any Postgres registry)
python -m insight_onboard scan \
    --registry-db postgresql://dba:***@registry-host:5432/registry \
    --out /tmp/proposal.yaml

# Recommended for OpenG2P registries — adds enum values + authoritative
# analytical-entity list from the extension's SQLAlchemy models
python -m insight_onboard scan \
    --registry-db postgresql://dba:***@registry-host:5432/registry \
    --registry-extension /path/to/nsr-extension/src/openg2p_registry_nsr_extension/register_domain/models \
    --registry-core      /path/to/openg2p-registry-core/src/openg2p_registry_core/models \
    --out /tmp/proposal.yaml
```

The proposal lists every column with a classification + reason. You
review it in your editor, override anything wrong, resolve any `pii: '?'`
entries, then move on to step 2. See [insight-onboard.md](insight-onboard.md)
for the full flag reference and review workflow.

### → Manual path

Hand-review every column, especially the boundary cases — `dob`
(probably `pii` if exact, `quasi` if year-only), `kebele_code` (probably
`quasi` — granular enough to re-identify), `livelihood_type` (typically
`safe`).

Output of this step: a complete column-by-column classification, ready
to drive the view DDL in step 2.

---

## Step 2 — Create PII-safe views in the registry DB

The safe layer lives in a **dedicated schema** (`insight_safe`), not the
registry's `public` schema. This separates analytical objects from the
source data, lets us grant the `insight_ai` role USAGE on the analytical
schema only, and means we can drop+recreate the whole safe layer without
touching the registry's tables.

| View type | When to use |
|-----------|-------------|
| Plain `CREATE VIEW` | Default. Cheap reads on small-to-medium registries (< ~5M rows), real-time freshness wanted, no storage overhead. |
| `CREATE MATERIALIZED VIEW` | Heavy reads on large registries, can tolerate ~daily staleness, willing to manage refresh scheduling. |

The CLI defaults to plain views (no MV auto-suggestion). If you want
MVs, hand-add them to the proposal YAML before `apply` — see the
declarative TODO block at the end of the generated proposal.

### → CLI shortcut

`insight-onboard apply --dry-run /tmp/proposal.yaml` prints exactly the
DDL shown below, derived from your reviewed proposal. Drop `--dry-run`
to execute.

### → Manual path

Connect to the registry Postgres **as an admin**:

#### 2a. Create the target schema

```sql
CREATE SCHEMA IF NOT EXISTS insight_safe;
```

#### 2b. A flat PII-safe view per main entity

```sql
-- One per top-level entity (individuals, households, …)
CREATE OR REPLACE VIEW insight_safe.v_individuals_safe AS
SELECT
    id,                              -- internal id (NOT the national id)
    household_id,
    region_code, zone_code, woreda_code, kebele_code,
    gender,
    estimated_age,
    age_bracket,                     -- generalised; not exact DOB
    primary_livelihood,
    employment_status,
    coping_strategies_index,
    disability_status,
    displacement_status,
    pmt_score,
    monthly_income,
    fayda_verified,                  -- bool, OK
    created_at
    -- DROPPED: full_name, primary_phone, alternate_phone,
    --          address_descriptor, gps_latitude, gps_longitude,
    --          fayda_token, rid, contact_person_name
FROM public.g2p_register_individuals;

CREATE OR REPLACE VIEW insight_safe.v_households_safe AS
SELECT
    id, headship_type, household_size,
    region_code, zone_code, woreda_code, kebele_code,
    pmt_score, monthly_income,
    coping_strategies_index, fayda_verified, registered_at
    -- DROPPED: head_full_name, contact_phone, address_line
FROM public.g2p_register_households;
```

#### 2c. (Optional) Pre-aggregated materialised views for hot query paths

Only do this if you've measured a real perf problem on plain views. For
most registries the plain views in 2b are enough.

```sql
CREATE MATERIALIZED VIEW insight_safe.mv_individuals_summary AS
SELECT
    i.*,
    h.headship_type,
    h.household_size,
    h.monthly_income AS household_monthly_income
FROM insight_safe.v_individuals_safe i
LEFT JOIN public.g2p_register_households h ON h.id = i.household_id;

CREATE INDEX ix_mv_ind_region ON insight_safe.mv_individuals_summary(region_code);
CREATE INDEX ix_mv_ind_gender ON insight_safe.mv_individuals_summary(gender);
CREATE INDEX ix_mv_ind_pmt    ON insight_safe.mv_individuals_summary(pmt_score);
-- … index every column you'll filter or group by

-- District-level rollup (typically a few thousand rows)
CREATE MATERIALIZED VIEW insight_safe.mv_district_aggregates AS
SELECT
    region_code, zone_code, woreda_code, gender, age_bracket,
    COUNT(*)                                AS total,
    AVG(pmt_score)::NUMERIC(6,2)            AS avg_pmt,
    AVG(monthly_income)::NUMERIC(12,2)      AS avg_income,
    COUNT(*) FILTER (WHERE disability_status='YES') AS disability_count,
    COUNT(*) FILTER (WHERE displacement_status='IDP') AS idp_count
FROM insight_safe.mv_individuals_summary
GROUP BY region_code, zone_code, woreda_code, gender, age_bracket;

CREATE INDEX ix_mv_dist_region ON insight_safe.mv_district_aggregates(region_code);
```

#### 2d. (Optional) Schedule a nightly refresh

```sql
-- Use REFRESH MATERIALIZED VIEW CONCURRENTLY for non-blocking refresh.
-- Requires a unique index on the view first.
CREATE UNIQUE INDEX uq_mv_ind_summary_id ON insight_safe.mv_individuals_summary(id);
```

Then schedule via `cron`, `pg_cron`, or an external scheduler:

```sql
-- daily at 02:00
REFRESH MATERIALIZED VIEW CONCURRENTLY insight_safe.mv_individuals_summary;
REFRESH MATERIALIZED VIEW CONCURRENTLY insight_safe.mv_district_aggregates;
```

The CLI doesn't wire up refresh scheduling today — that's on the
roadmap. Whichever path you took, the cron is a separate operator
concern.

---

## Step 3 — Create the `insight_ai` role with SELECT-only grants

### → CLI shortcut

The `apply` step emits this role + grants block. It generates a fresh
32-char password and prints it once to stdout — save it into your K8s
secret immediately.

### → Manual path

```sql
CREATE ROLE insight_ai LOGIN PASSWORD '<strong-random-password>';

GRANT CONNECT ON DATABASE <registry_db_name> TO insight_ai;

-- USAGE on the target schema only — NOT on public.
-- Without USAGE on public, the role can't even reference base tables
-- by name; it would get `permission denied for schema public`.
GRANT USAGE ON SCHEMA insight_safe TO insight_ai;

-- Grant SELECT ONLY on the safe views/MVs. NEVER on raw registry tables.
GRANT SELECT ON insight_safe.v_individuals_safe TO insight_ai;
GRANT SELECT ON insight_safe.v_households_safe  TO insight_ai;
GRANT SELECT ON insight_safe.mv_individuals_summary TO insight_ai;
GRANT SELECT ON insight_safe.mv_district_aggregates TO insight_ai;

-- Default search_path so unqualified table references in LLM-emitted SQL
-- resolve to the safe layer transparently.
ALTER ROLE insight_ai SET search_path = insight_safe;

-- Hard safety nets at the role level.
ALTER ROLE insight_ai SET statement_timeout = '30s';
ALTER ROLE insight_ai SET lock_timeout = '5s';
ALTER ROLE insight_ai SET idle_in_transaction_session_timeout = '60s';
```

Verify the role really can't see PII:

```sql
-- As the insight_ai role:
SET ROLE insight_ai;
SELECT * FROM public.g2p_register_individuals LIMIT 1;   -- should ERROR: permission denied for schema public
SELECT * FROM insight_safe.v_individuals_safe LIMIT 1;   -- should work
SELECT * FROM v_individuals_safe LIMIT 1;                -- should work (search_path resolves it)
RESET ROLE;
```

---

## Step 4 — Write the catalog YAML

### → CLI shortcut

`insight-onboard apply --catalog-out /tmp/catalog.yaml` writes this file
fully populated from your reviewed proposal. With `--registry-extension`,
enum values are populated authoritatively from the SQLAlchemy
`Mapped[…Enum]` annotations — no hand-typing.

### → Manual path

Edit the draft from step 1:

```yaml
deployment:
  country_code: ET                 # ISO code
  default_currency: ETB
  k_anonymity: 5                   # raise per regulatory requirement
  master_data_service_url: ${INSIGHT_MASTERDATA__URL}

tables:
  - name: mv_individuals_summary
    schema: insight_safe
    description: "Pre-joined individuals with household-level fields. Refreshed nightly at 02:00 UTC."
    row_count_estimate: 47000000   # current size; used by the cost gate
    indexed: [region_code, zone_code, woreda_code, gender, age_bracket, pmt_score]
    columns:
      - {name: id,               type: int,    pii: pii}
      - {name: gender,           type: enum,   pii: safe,  enum: [M, F, O]}
      - {name: age_bracket,      type: enum,   pii: quasi, enum: ["0-5","6-17","18-35","36-55","55+"]}
      - {name: region_code,      type: string, pii: safe,  description: "Ethiopia admin level 1 (region) — ~5 distinct values"}
      - {name: zone_code,        type: string, pii: safe,  description: "Ethiopia admin level 2 (zone) — ~15 distinct values"}
      - {name: woreda_code,      type: string, pii: safe,  description: "Ethiopia admin level 3 (woreda) — hundreds; use for top-N rankings"}
      - {name: pmt_score,        type: float,  pii: safe}
      - {name: monthly_income,   type: float,  pii: safe,  unit: ETB}
      # … one row per column …

# The allow-list. ONLY these names can be queried by the AI agent.
# Anything not here is invisible to the LLM and rejected by the SQL validator.
safe_views:
  - mv_individuals_summary
  - mv_district_aggregates
  - v_individuals_safe
  - v_households_safe

# Audit-only — never sent to the LLM. Helps the generator and reviewers track
# which PII columns were intentionally excluded.
pii_columns_in_source:
  - {table: g2p_register_individuals, column: full_name}
  - {table: g2p_register_individuals, column: primary_phone}
  - {table: g2p_register_individuals, column: fayda_token}
  # …

# Map your geographic columns to the Master Data Service level mnemonics.
# Required only if you want geo drill-down via the Master Data Service.
geo_columns:
  - {column: region_code, level_mnemonic: region}
  - {column: zone_code,   level_mnemonic: zone}
  - {column: woreda_code, level_mnemonic: woreda}
  - {column: kebele_code, level_mnemonic: kebele}
```

**Things people forget on first onboarding:**

- **Enum values** must be the actual DB values, not pretty labels (`"M"` not `"Male"`). Wrap them in quotes — YAML parses bare `YES`/`NO`/`ON`/`OFF` as booleans.
- **Cardinality hints in column descriptions** help the LLM avoid asking for "top 10 regions" when only 5 exist.
- **Materialised view notes** like `"Refreshed nightly at 02:00 UTC"` give the LLM context for time-sensitivity questions.
- **`row_count_estimate`** is used by the cost gate. Order of magnitude is enough.

---

## Step 5 — Configure Helm values for the new deployment

### → CLI shortcut

`insight-onboard apply --helm-out /tmp/values-onboard.yaml` writes a
scaffold. You fill in `registryDBHost` / `insightHostname` /
`masterDataServiceUrl` and run `helm upgrade`.

### → Manual path

```yaml
# values-<country>.yaml
global:
  insightHostname: insight.<env>.openg2p.org
  registryDBHost: <new-registry-postgres-host>
  registryDBPort: 5432
  registryDBName: <registry_db_name>
  masterDataServiceUrl: https://masterdata.<env>.openg2p.org
  llmModel: claude-opus-4-7         # or qwen3-32b for sovereign deployments

insightBackend:
  envVars:
    DB_USER: insight_ai             # the role from step 3
  envVarsFrom:
    DB_PASSWORD:
      secretKeyRef:
        name: g2p-insight-registry-creds
        key: insight-ai-password    # the insight_ai role password from step 3

  appCatalog:
    schemaYaml: |
      <paste your catalog YAML from step 4>
    piiRecognizersYaml: |
      recognizers:
        # Country-specific PII patterns
        - entity: FAYDA_TOKEN
          patterns:
            - {name: fayda_numeric, regex: '\b\d{12}\b', score: 0.6}
          context: [fayda, token]
```

Deploy:

```bash
helm upgrade --install g2p-insight ./helm/g2p-insight \
    -f values-<country>.yaml \
    --namespace insight-<country> --create-namespace
```

---

## Step 6 — Smoke-test

Once the backend is up, three quick checks (same regardless of path):

```bash
# 1. Catalog loaded?
curl https://insight.<env>.openg2p.org/v1/insight/catalog | jq '.tables | length'

# 2. Insight_ai role works?
curl -X POST https://insight.<env>.openg2p.org/v1/insight/ask \
  -d '{"question":"How many individuals?"}'
# Should return a count

# 3. PII really gated?
curl -X POST https://insight.<env>.openg2p.org/v1/insight/ask \
  -d '{"question":"Show me individuals with names containing John"}'
# Should NOT return rows. The LLM should clarify or refuse —
# `full_name` isn't in any safe_view.
```

---

## Step 7 — Document this deployment

Per OpenG2P standard, capture in your country's operator docs:

- Which views Insight queries (your `safe_views` list)
- When materialised views refresh (cron schedule)
- Who holds the `insight_ai` role password
- What `k_anonymity` value is set (might be > 5 if regulator demands)
- Any custom PII recognizers added
- Path used: CLI or manual (the proposal YAML is your record for the
  CLI path; this runbook + your edited SQL is the record for the manual
  path)

---

## Final audit — verify no PII leaked

### → CLI shortcut

```bash
python -m insight_onboard audit \
    --registry-db postgresql://dba:***@registry-host:5432/registry \
    --proposal /tmp/proposal.yaml
```

Diffs the columns visible to `insight_ai` against the proposal's
`pii_columns_in_source` and exits non-zero on any leak. Wire into CI to
re-check after every registry schema change.

### → Manual path

```sql
-- As the insight_ai role:
SET ROLE insight_ai;

-- List every column visible to insight_ai across both schemas
SELECT table_schema, table_name, column_name
FROM information_schema.columns
WHERE table_schema IN ('public', 'insight_safe')
ORDER BY table_schema, table_name, ordinal_position;

-- Cross-reference against your `pii_columns_in_source` list.
-- Any column tagged `pii` that appears here is a leak.

RESET ROLE;
```

If you can run this and the list contains zero `pii`-tagged columns,
you're done.

---

## Common mistakes to avoid (both paths)

| Mistake | Why it's bad | Fix |
|---------|--------------|-----|
| Granting `SELECT` on raw tables to `insight_ai` "just for testing" | Bypasses the entire PII model | Don't. Use the safe views even in dev. |
| Listing raw table names in `safe_views` | Same as above, but worse — the LLM gets told to query them | Only views/MVs in `safe_views`. |
| Putting safe views in `public` schema | Loses the schema-isolation boundary; harder to drop+recreate; the role would need USAGE on `public` | Always create them in `insight_safe` (the CLI default) |
| Forgetting to refresh the materialised views | LLM returns stale data with no warning | Schedule via pg_cron or external scheduler; document the schedule in the catalog `description` so the LLM mentions it in assumptions. |
| YAML enum values like `enum: [YES, NO]` (unquoted) | YAML 1.1 parses as `[True, False]`; the catalog validator catches it but it's a confusing error | Always quote enum values: `enum: ["YES", "NO"]`. Or use the CLI with `--registry-extension` — enum values come from the Python source directly, no quoting concerns. |
| `kebele_code` tagged as `safe` | Kebele-level groups can have < 20 people — re-identification risk despite k=5 | Tag as `quasi` and rely on k-anonymity at result time. |
| Forgetting `ALTER ROLE … SET search_path = insight_safe` | LLM-emitted SQL says `FROM mv_individuals_summary` (no schema prefix) — without search_path, Postgres can't find it | Always set search_path on the role (CLI does this automatically) |
