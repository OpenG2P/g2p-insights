# Onboarding a new registry — DBA runbook

> **Heads up — most of this can be automated.** The `insight-onboard` CLI
> ([docs/insight-onboard.md](insight-onboard.md)) walks through these 7 steps
> automatically. Use this runbook if you want to do it by hand, or to
> understand what the CLI is doing under the hood.

End-to-end recipe for pointing Insight at a new registry database. Read top
to bottom; each step has copy-pasteable SQL or YAML. Roles needed: one DBA
with admin on the registry Postgres, one platform engineer to deploy Helm.

Time estimate: **half a day** for a registry of moderate complexity once
you've done it once — or **~30 minutes** with the CLI.

---

## Pre-requisites

- The new registry's Postgres reachable from the K8s cluster.
- The registry's table schema (DDL or SQLAlchemy models).
- A list — even a partial one — of which columns hold PII vs which are safe.
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

Two practical aids:

1. **Run the generator on the registry's SQLAlchemy models** to get a
   conservative first pass:

   ```bash
   cd backend
   python scripts/generate_catalog.py \
       --package <registry.models.package> \
       --output catalog/schema-DRAFT.yaml
   ```

   It applies regex heuristics (`name|phone|email|address|fayda|aadhaar|...`
   → `pii`; `age|gender|marital|...` → `quasi`; everything else → `safe`).
   **Treat the output as a first draft, not a final answer.**

2. **Hand-review every column**, especially the boundary cases — `dob`
   (probably `pii` if exact, `quasi` if year-only), `kebele_code`
   (probably `quasi` — granular enough to re-identify), `livelihood_type`
   (typically `safe`).

Output of this step: a complete column-by-column classification.

---

## Step 2 — Create PII-safe views in the registry DB

Connect to the registry Postgres **as an admin** and create views that
exclude `pii` columns and (optionally) generalise `quasi` columns. Don't
duplicate data — plain views and materialised views are both fine. Choose
based on read load:

| View type | When to use |
|-----------|-------------|
| Plain `CREATE VIEW` | Cheap reads, < 100k rows, real-time freshness wanted |
| `CREATE MATERIALIZED VIEW` | Heavy reads, large registry, can tolerate ~daily lag |

### 2a. A flat PII-safe view per main entity

```sql
-- One per top-level entity (individuals, households, …)
CREATE OR REPLACE VIEW v_individuals_safe AS
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
FROM g2p_register_individuals;

CREATE OR REPLACE VIEW v_households_safe AS
SELECT
    id, headship_type, household_size,
    region_code, zone_code, woreda_code, kebele_code,
    pmt_score, monthly_income,
    coping_strategies_index, fayda_verified, registered_at
    -- DROPPED: head_full_name, contact_phone, address_line
FROM g2p_register_households;
```

### 2b. Pre-aggregated materialised views for hot query paths

```sql
CREATE MATERIALIZED VIEW mv_individuals_summary AS
SELECT
    i.*,
    h.headship_type,
    h.household_size,
    h.monthly_income AS household_monthly_income
FROM v_individuals_safe i
LEFT JOIN g2p_register_households h ON h.id = i.household_id;

CREATE INDEX ix_mv_ind_region ON mv_individuals_summary(region_code);
CREATE INDEX ix_mv_ind_gender ON mv_individuals_summary(gender);
CREATE INDEX ix_mv_ind_pmt    ON mv_individuals_summary(pmt_score);
-- ... index every column you'll filter or group by

-- District-level rollup (typically a few thousand rows)
CREATE MATERIALIZED VIEW mv_district_aggregates AS
SELECT
    region_code, zone_code, woreda_code, gender, age_bracket,
    COUNT(*)                                AS total,
    AVG(pmt_score)::NUMERIC(6,2)            AS avg_pmt,
    AVG(monthly_income)::NUMERIC(12,2)      AS avg_income,
    COUNT(*) FILTER (WHERE disability_status='YES') AS disability_count,
    COUNT(*) FILTER (WHERE displacement_status='IDP') AS idp_count
FROM mv_individuals_summary
GROUP BY region_code, zone_code, woreda_code, gender, age_bracket;

CREATE INDEX ix_mv_dist_region ON mv_district_aggregates(region_code);
```

### 2c. Schedule a nightly refresh

```sql
-- Use REFRESH MATERIALIZED VIEW CONCURRENTLY for non-blocking refresh.
-- Requires a unique index on the view first.
CREATE UNIQUE INDEX uq_mv_ind_summary_id ON mv_individuals_summary(id);
```

Then schedule via `cron`, `pg_cron`, or the OpenG2P scheduler:

```sql
-- daily at 02:00
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_individuals_summary;
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_district_aggregates;
```

---

## Step 3 — Create the `insight_ai` role with SELECT-only grants

```sql
CREATE ROLE insight_ai LOGIN PASSWORD '<strong-random-password>';

GRANT CONNECT ON DATABASE <registry_db_name> TO insight_ai;
GRANT USAGE   ON SCHEMA public TO insight_ai;

-- Grant SELECT ONLY on the safe views/MVs. NEVER on raw registry tables.
GRANT SELECT ON v_individuals_safe TO insight_ai;
GRANT SELECT ON v_households_safe  TO insight_ai;
GRANT SELECT ON mv_individuals_summary TO insight_ai;
GRANT SELECT ON mv_district_aggregates TO insight_ai;

-- Explicit revoke as defense in depth.
REVOKE ALL ON g2p_register_individuals, g2p_register_households,
              g2p_register_individual_programs, g2p_register_individual_shocks,
              g2p_register_household_housing
  FROM insight_ai;

-- Hard safety nets at the role level.
ALTER ROLE insight_ai SET statement_timeout = '30s';
ALTER ROLE insight_ai SET lock_timeout = '5s';
ALTER ROLE insight_ai SET idle_in_transaction_session_timeout = '60s';
```

Verify the role really can't see PII:

```sql
-- As the insight_ai role:
SET ROLE insight_ai;
SELECT * FROM g2p_register_individuals LIMIT 1;   -- should ERROR: permission denied
SELECT * FROM mv_individuals_summary LIMIT 1;     -- should work
RESET ROLE;
```

---

## Step 4 — Write the catalog YAML

Edit the draft from step 1:

```yaml
deployment:
  country_code: ET                 # ISO code
  default_currency: ETB
  k_anonymity: 5                   # raise per regulatory requirement
  master_data_service_url: ${INSIGHT_MASTERDATA__URL}

tables:
  - name: mv_individuals_summary
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
# Anything not here is invisible to the LLM and rejected by the SQL planner.
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
- **Cardinality hints in column descriptions** help the LLM avoid asking for "top 10 regions" when only 5 exist. See the `region_code` description above.
- **Materialised view notes** like `"Refreshed nightly at 02:00 UTC"` give the LLM context for time-sensitivity questions.
- **`row_count_estimate`** is used by the cost gate. Doesn't have to be exact; order of magnitude is enough.

---

## Step 5 — Configure Helm values for the new deployment

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

Once the backend is up, three quick checks:

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

- Which views Insight queries
- When materialised views refresh (cron schedule)
- Who holds the `insight_ai` role password
- What `k_anonymity` value is set (might be > 5 if regulator demands)
- Any custom PII recognizers added

---

## Verifying you didn't expose PII — final audit

```sql
-- As the insight_ai role:
SET ROLE insight_ai;

-- List every column visible to insight_ai
SELECT table_schema, table_name, column_name
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_schema, table_name, ordinal_position;

-- Cross-reference against your `pii_columns_in_source` list.
-- Any column tagged `pii` that appears here is a leak.

RESET ROLE;
```

If you can run this and the list contains zero `pii`-tagged columns, you're
done.

---

## Common mistakes to avoid

| Mistake | Why it's bad | Fix |
|---------|--------------|-----|
| Granting `SELECT` on raw tables to `insight_ai` "just for testing" | Bypasses the entire PII model | Don't. Use the safe views even in dev. |
| Listing raw table names in `safe_views` | Same as above, but worse — the LLM gets told to query them | Only views/MVs in `safe_views`. |
| Forgetting to refresh the materialised views | LLM returns stale data with no warning | Schedule via pg_cron or external scheduler; document the schedule in the catalog `description` so the LLM mentions it in assumptions. |
| YAML enum values like `enum: [YES, NO]` (unquoted) | YAML 1.1 parses as `[True, False]`; the catalog validator catches it but it's a confusing error | Always quote enum values: `enum: ["YES", "NO"]`. |
| `kebele_code` tagged as `safe` | Kebele-level groups can have < 20 people — re-identification risk despite k=5 | Tag as `quasi` and rely on k-anonymity at result time. |
