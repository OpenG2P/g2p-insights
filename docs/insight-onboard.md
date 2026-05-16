# `insight-onboard` — registry onboarding CLI

Automates the steps in the [onboarding runbook](onboarding-new-registry.md):
inspects a registry's schema, classifies columns by PII risk, proposes safe
views in a dedicated `insight_safe` schema, applies them with a
least-privilege role, and writes the operational catalog + Helm overlay.

Three subcommands: `scan`, `apply`, `audit`. `scan` and `audit` are
read-only; `apply` defaults to interactive confirmation and supports
`--dry-run` so you always see the SQL before anything runs.

---

## Why this exists

Onboarding by hand: classify every column, write view DDL, create roles +
grants, draft a catalog YAML, verify no PII leaks. With 20+ tables × 30+
columns each it's mechanical work where mistakes leak data. The CLI turns
it into:

1. **`scan`** — produces a reviewable `proposal.yaml`
2. *human reviews the YAML in their editor*
3. **`apply proposal.yaml`** — emits and runs DDL, writes catalog + Helm
   overlay, runs the leak audit at the end
4. **`audit`** — re-runs the leak check standalone (for CI, or after a
   registry schema change)

About 30 minutes of human time per registry, vs. half a day by hand.

---

## Install

The tool ships in `backend/scripts/insight_onboard`. Runs in the same venv
as the backend:

```bash
cd backend
source .venv/bin/activate
pip install -e .                       # brings in psycopg, pyyaml, sqlglot
python -m insight_onboard --help       # PYTHONPATH=scripts adds the package
```

If you want to use registry-extension introspection (recommended for OpenG2P
registries — see below), the only thing you need is **read access to the
extension's source tree**. No need to install the extension or its
dependencies — the tool parses the Python files via AST without executing
them.

---

## End-to-end workflow

### 1. Scan

Connect as a DBA who can read every table. The tool only does `SELECT`.

Minimal invocation — heuristic-only:

```bash
python -m insight_onboard scan \
    --registry-db postgresql://dba:***@registry-host:5432/registry \
    --out /tmp/proposal.yaml
```

**Recommended for OpenG2P registries** — point at the extension models:

```bash
python -m insight_onboard scan \
    --registry-db postgresql://dba:***@registry-host:5432/registry \
    --registry-extension /path/to/nsr-extension/src/openg2p_registry_nsr_extension/register_domain/models \
    --registry-core      /path/to/openg2p-registry-core/src/openg2p_registry_core/models \
    --out /tmp/proposal.yaml
```

Output:

```
[scan] connecting to postgresql://dba:***@registry-host:5432/registry
[scan]   source schema: public
[scan]   target schema (safe layer): insight_safe
[scan]   extension paths: ['.../register_domain/models', '.../core/models']
[scan]   extension: 59 models, 12 analytical entities, 34 enums
[scan] found 92 tables in public
[scan]   inspecting g2p_register_household_assets
[scan]   inspecting g2p_register_households
[scan]   ...
[scan] wrote /tmp/proposal.yaml
[scan] summary:
         12 analytical tables
         80 skipped tables
         120 PII columns identified (will be dropped)
         2 columns need human review (`pii: '?'`)
         0 materialized views suggested

Next: review /tmp/proposal.yaml, then run `insight-onboard apply ...`
```

Inspect `/tmp/proposal.yaml` in your editor. Every kept column has a
classification (`pii` / `quasi` / `safe`), a confidence, a reason, and —
when the extension is loaded — enum values and the mixin that declared
the column. Edit anything that looks wrong.

#### Two scan modes

The scan operates in one of two modes, picked by whether `--registry-extension`
was passed:

| Mode | When | What it iterates | Typical scan time | Ambiguity |
|------|------|------------------|-------------------|-----------|
| **Extension-led** (recommended for OpenG2P) | `--registry-extension` provided | Only the entity tables declared by the extension (e.g. 12 on NSR) | Seconds | Near zero |
| **Heuristic-led** (fallback for any Postgres) | No extension flag | Every DB table, minus name-pattern skips | Minutes (1–5 min per ~100 tables) | Several per scan |

In **extension-led mode**, the script asks the extension "what tables matter?"
and **never even calls `list_columns` on DB tables that aren't declared
analytical entities**. The 80 operational/workflow/queue tables in a real
NSR registry get skipped without a single column query. This is the main
source of the speedup — heuristic-led mode pays per-column `COUNT(DISTINCT)`
on every column of every non-skip-patterned table; extension-led mode pays
that cost only when no other classifier decided.

#### What the classifier does

The per-column classifier runs in this order; the first layer that produces a
high-confidence verdict wins, and the slow sample step at the end only runs
if nothing earlier decided:

| Layer | Method | What it catches | Costs a DB query? |
|-------|--------|-----------------|-------------------|
| **1. Type-based exclusion** | Column type is `json`/`jsonb`/`bytea`/`xml` | Opaque blobs that may carry nested PII — auto-`pii` | No |
| **2. Token-set matching on column name** | `gps_latitude` → tokens `{gps, latitude}` → both PII keywords. Keyword sets live in [`default_pii_rules.yaml`](../backend/scripts/insight_onboard/default_pii_rules.yaml). Temporal types (date/timestamp) also short-circuit here as `safe`. | Most well-named columns | No |
| **3. Extension enum** | `Mapped[GenderEnum]` annotation in the registry extension → safe enum, with values written into the catalog | Categorical analytical fields (gender, marital_status, etc.) | No |
| **4. Extension mixin prior** | Column declared in `G2PPerson` mixin → `pii` prior; `G2PGeo` → `quasi`; `G2PRegister` → `safe`. Medium-confidence, loses to a high-confidence name signal. | Columns whose name is too generic to decide, like `prefix`/`suffix`/`altitude` | No |
| **5. Sample-value shape inspection** | `COUNT(DISTINCT col)` + 20 distinct values; phone-shaped → `pii`, low cardinality + text → `safe` enum, free-text → suspect | Generically-named columns that no earlier layer decided | **Yes — slow** |
| **6. (Optional) LLM-assisted** | Sends column metadata + value SHAPES (never raw values) to the configured LLM | The few residual unclear columns | No (LLM API) |

Layers 3 and 4 only contribute when the extension is loaded. In
extension-led mode they usually decide the residual columns the name
classifier couldn't, so layer 5 (the slow one) almost never fires.

Layer 6 is opt-in via `--use-llm`:

```bash
python -m insight_onboard scan --use-llm \
    --registry-db ... --out /tmp/proposal.yaml
```

This uses the same `INSIGHT_LLM__*` env vars as the backend, so whatever
LLM you've configured for production (Claude / OpenRouter Qwen / on-prem
vLLM) is what classifies. Cost: roughly $0.10 per registry, one-time.

#### What the registry extension adds

When `--registry-extension` is provided, the scanner does four extra
things — all AST-based, no runtime imports needed:

1. **Authoritative analytical-table set, drives the outer loop.** Only
   tables whose model class transitively inherits `G2PRegister` count as
   analytical entities. The scan iterates THIS set, not the DB's table
   list. Tables in the DB but not in the extension's entity list are
   skipped immediately — no `list_columns`, no sampling. This catches
   `g2p_register_verifications` (looks analytical by name, but its model
   doesn't extend `G2PRegister` — workflow/audit), change-request tables,
   queues, ingestion pipelines, theme/config tables, etc.

2. **Enum values from `Mapped[…Enum]` annotations.** Many columns are
   declared `Mapped[GenderEnum]` in Python but stored as plain `String`
   in Postgres. The DB has no way to tell you the valid values. The
   extension's enum classes are pure-Python; the scanner pulls
   `{member.value, …}` out and writes them into the catalog so the
   runtime LLM prompt can render `gender: enum={M, F, O}` and the model
   filters on the exact declared values.

3. **Mixin attribution + PII priors.** For every kept column the catalog
   records which mixin declared it (`G2PRegister` / `G2PPerson` / `G2PGeo`
   / domain mixin). The mixin also drives a medium-confidence PII prior
   for residual columns the name classifier couldn't decide: `G2PPerson`
   → `pii`, `G2PGeo` → `quasi`, `G2PRegister` → `safe`. A high-confidence
   name signal (`registration_date` in G2PPerson → still `safe` via the
   temporal-type rule) always wins.

4. **Sanity check for schema drift.** Entity tables declared by the
   extension but missing from the DB show up in `skipped:` with reason
   "declared by extension but absent in DB — migrations behind?". Useful
   diagnostic — typically means the deployment is on an older migration.

**Caveats worth knowing:**

- **`g2p_register_scores` is excluded by default.** Its model
  (`G2PRegisterScore`) doesn't transitively inherit `G2PRegister`, so the
  strict entity rule treats it as non-entity. If you want PMT scores as a
  queryable table, manually move it from `skipped:` to
  `analytical_tables:` in the proposal YAML before `apply`.
- **Schema drift is a hard dependency.** Extension-led mode trusts the
  extension package as the source of truth. If a deployment ran a
  migration the extension's models don't know about yet, the new columns
  are invisible to Insight until the extension package catches up. For
  pure-OpenG2P deployments this is the right tension; for deployments
  that hand-customise the schema, run **without** the extension flag
  (heuristic-led mode) until the customisations land in a packaged
  extension.

### 2. Review the proposal

The YAML has every table and column with the proposed classification and
the reason. Reviewers do four things:

1. **Override any wrong classification** — set `pii:` and `keep:` to the
   intended values.
2. **Resolve any `pii: '?'` entries** — these are columns the heuristics
   couldn't classify (typically empty columns where sample-shape gives no
   signal). Read the `reason`, decide, replace `?` with a real value.
   With the extension loaded, ambiguous columns are usually rare (most
   classifications come from authoritative metadata).
3. **(Optional) Hand-add materialized views** — auto-generation is
   deferred; the proposal ends with a commented declarative example of
   the eventual `{dimensions: […], metrics: […]}` grammar. For now you
   can hand-write a `materialized_views:` block to get a custom rollup.
4. **Fill in `deployment.country_code` and `default_currency`** — these
   are required by `apply`.

You can also adjust:

- `deployment.k_anonymity` (raise to meet a stricter policy)
- `source_schema` / `target_schema` (default `public` / `insight_safe`)
- Move tables from `skipped` to `analytical_tables` if the heuristic was
  too aggressive about excluding them

`apply` refuses to run until every `?` is resolved and `country_code` is
set.

### 3. Apply — dry-run first

Always do a dry-run first to see the DDL:

```bash
python -m insight_onboard apply \
    --registry-db postgresql://dba:***@registry-host:5432/registry \
    --dry-run \
    --catalog-out /tmp/catalog.yaml \
    --helm-out    /tmp/values-onboard.yaml \
    /tmp/proposal.yaml
```

Output: the full SQL that would execute, followed by the generated catalog
and Helm overlay. **No DB changes.**

The emitted SQL has four sections:

1. `CREATE SCHEMA IF NOT EXISTS "insight_safe"`
2. `CREATE OR REPLACE VIEW "insight_safe"."v_<table>_safe" AS SELECT … FROM "public"."<table>"` — one per analytical table, with PII columns omitted
3. *(optional)* `CREATE MATERIALIZED VIEW` — only for MVs you hand-added
4. Role + grants:
   - `DROP ROLE IF EXISTS "insight_ai"; CREATE ROLE … LOGIN PASSWORD '…'`
   - `GRANT CONNECT ON DATABASE … TO "insight_ai"`
   - `GRANT USAGE ON SCHEMA "insight_safe" TO "insight_ai"` *(NOT on the source schema)*
   - `GRANT SELECT ON "insight_safe"."<view>" TO "insight_ai"` per view
   - `ALTER ROLE "insight_ai" SET search_path = "insight_safe"`
   - Per-role timeouts (statement 30s, lock 5s, idle-tx 60s)

### 4. Apply — for real

Drop `--dry-run`. The tool prints the SQL again and asks for confirmation:

```
[apply] about to execute the following SQL:
   ... full SQL block ...
Proceed? (yes/no): yes
[apply] DDL executed
[apply] PII-leak audit: 0 leaks ✓
[apply] wrote catalog → /tmp/catalog.yaml
[apply] wrote Helm values overlay → /tmp/values-onboard.yaml

[apply] done. Save this `insight_ai` password into your K8s secret:
  Ak83mVxQp9Lz7rNwT4hYbCf2sD6jKvEu

  kubectl create secret generic g2p-insight-registry-creds \
      --from-literal=insight-ai-password='Ak83mVxQp9Lz7rNwT4hYbCf2sD6jKvEu'
```

The password is generated fresh each run (32 random chars). Save it
immediately — there's no way to recover it later. If you want a specific
password, pass `--role-password`.

### 5. Audit — verify no PII leaked

The audit is a separate command so you can re-run it later (e.g., after a
DDL change to the registry, or as part of CI):

```bash
python -m insight_onboard audit \
    --registry-db postgresql://dba:***@registry-host:5432/registry \
    --proposal /tmp/proposal.yaml
```

It `SET ROLE insight_ai`, lists every column visible across both the
source and target schemas, and diffs against the proposal's
`pii_columns_in_source`. Output:

```
audit: 0 leaks ✓
```

…or, if anything's wrong:

```
AUDIT FAILED — 2 PII columns visible to insight_ai:
  - g2p_register_individuals.full_name
  - g2p_register_individuals.primary_phone
```

`apply` runs the audit automatically at the end and refuses to succeed if
there are leaks. The standalone `audit` command is for re-checks.

---

## Reference — all CLI flags

### `scan`

| Flag | Default | Purpose |
|------|---------|---------|
| `--registry-db <DSN>` | required | Postgres connection string for the source registry |
| `--out <path>` | `proposal.yaml` | Where to write the reviewable proposal |
| `--source-schema <name>` | `public` | Schema where the registry tables live |
| `--target-schema <name>` | `insight_safe` | Dedicated schema for the safe layer (created by `apply`) |
| `--pii-rules <path>` | bundled defaults | YAML overriding the keyword sets / skip patterns (see below) |
| `--registry-extension <path>` | none | Path to the extension's `register_domain/models/` dir — enables analytical-entity filter + enum extraction |
| `--registry-core <path>` | none | Path to `openg2p-registry-core/src/openg2p_registry_core/models/` — provides the base mixins (G2PRegister, G2PPerson, G2PGeo) |
| `--use-llm` | off | Consult the configured LLM for columns the heuristics flagged unclear |

### `apply`

| Flag | Default | Purpose |
|------|---------|---------|
| `<proposal>` | required | Path to the reviewed proposal YAML |
| `--registry-db <DSN>` | required | Postgres connection string (admin role for DDL) |
| `--dry-run` | off | Print SQL but don't execute |
| `--role <name>` | `insight_ai` | Role to create / overwrite |
| `--role-password <pw>` | random 32 chars | Skip auto-generation |
| `--catalog-out <path>` | `catalog.yaml` | Where to write the operational catalog |
| `--helm-out <path>` | `values-onboard.yaml` | Where to write the Helm values overlay scaffold |

### `audit`

| Flag | Default | Purpose |
|------|---------|---------|
| `--registry-db <DSN>` | required | Postgres connection string |
| `--proposal <path>` | required | Reviewed proposal — provides the PII source-of-truth list |
| `--role <name>` | `insight_ai` | Role to test |

---

## Schema layout — what `apply` creates in your DB

```
registry (database)
├── public                              ← source schema (operator's tables, untouched)
│     g2p_register_individuals
│     g2p_register_households
│     ...
└── insight_safe                        ← target schema (created by apply)
      v_g2p_register_individuals_safe   ← view: only safe columns
      v_g2p_register_households_safe
      ...
```

The `insight_ai` role:
- has `USAGE` on `insight_safe` only — **not** on `public`
- has `SELECT` on each `v_*_safe` view
- has `search_path = "insight_safe"`, so unqualified LLM-emitted SQL
  resolves to the safe layer transparently
- no `INSERT`/`UPDATE`/`DELETE`/`DDL` anywhere

This is defence-in-depth: even if a prompt-injection slipped past the
SQL validator in the backend, the Postgres role itself can't see PII
columns or write anything.

---

## Heuristics — extending per deployment

The keyword sets, multi-token patterns, and skip-table regexes live in
[`default_pii_rules.yaml`](../backend/scripts/insight_onboard/default_pii_rules.yaml).
Pass `--pii-rules my-rules.yaml` to override per deployment. Sections you
omit from the override fall back to the bundled defaults — you only have
to write the parts you want to change.

Sections in the rules file:

- **`pii_keywords`** — single-token column-name matches (e.g. `phone`,
  `email`, `foundational`, `latitude`)
- **`pii_multi_token`** — combinations like `(date, birth)` → DOB,
  `(plus, code)` → Google Plus Code (location)
- **`quasi_keywords`** — single-token re-identifier matches (`gender`,
  `disability`, `displacement`, `kebele`)
- **`safe_keywords`** — single-token analytical matches (`region`,
  `pmt`, `income`, `livelihood`)
- **`safe_multi_token`** — combinations like `(age, bracket)` →
  generalised age, checked *before* `pii` so they win over single-token
  matches
- **`skip_table_patterns`** — case-insensitive regex matches against the
  table name. Anything matching is removed from the analytical set
  upfront, before column inspection

Adding a country-specific national-ID column: append the column-name
token to `pii_keywords` in your override file. No code change.

---

## Tables auto-skipped (out of the box)

The bundled rules skip these patterns:

| Pattern | Why |
|---------|-----|
| `^g2p_intake_form_` | Raw intake submissions — pre-validation, not analytical |
| `^g2p_register_history_` and `_history$` | History mirror of base table |
| `^g2p_register_change_request` | Change-request workflow |
| `^dedup_` / `^deduplication_` | Deduplication tracking |
| `(?:_|^)queue$` | Background-job queue |
| `_log$` / `_logs$` / `_audit$` | Audit/log tables |
| `^g2p_attributes?$` / `^g2p_attribute_values$` | Attribute schema metadata |
| `^g2p_input_mechanisms?$` | Input-channel registry |
| `^g2p_intake_form_definitions$` | Form-definition metadata |
| `^g2p_register_(definitions|schemas|sections|ui_)` | Register schema / UI metadata |
| `^g2p_register_section_` | Section metadata (UI-driven, not data) |
| `^g2p_register_score_definitions$` | Score-formula definitions, not score values |
| `^data_models?$` | Schema definitions |
| `^g2p_registry_` | Registry configuration (language, theme, import, VC) |
| `^g2p_registrant_` | Registrant authentication metadata |
| `^incoming_` / `^outgoing_` / `^import_file_` | Ingestion / outbound pipeline staging |
| `^registry_(languages|themes|theme_values)$` | i18n / theming |
| `^subscription_` | Subscription/activity metadata |
| `^alembic_version$` | Migration state |

With `--registry-extension`, the skip set is **expanded**: any table the
extension declares but whose class doesn't inherit `G2PRegister` is also
skipped (catches `g2p_register_verifications`, etc.).

To force a "skipped" table back in, move it to `analytical_tables` in
the proposal YAML before `apply`.

---

## Column-classification gotchas worth knowing

These behaviours are deliberate but easy to be surprised by:

1. **Empty columns → `unclear` (operator must review).** If a column has
   no rows there's nothing to learn from sample shapes. We default to
   `pii: '?'` so the operator must explicitly decide rather than silently
   marking it safe. Annoying on fresh deployments where every table is
   empty; safe-by-default.

2. **JSON / JSONB / bytea / xml → auto-`pii`.** Opaque blobs can contain
   anything including nested PII. They can't be projected into a flat
   safe view without an explicit `jsonb_path` extraction, which is a
   manual step. Default behaviour: drop.

3. **Date / timestamp columns → safe** (unless the name carries a PII
   signal like `birth_date`). The sample-shape phone-number regex
   collides with `YYYY-MM-DD` strings, so we short-circuit on type before
   running the shape check.

4. **Sample-based classification is bounded.** We sample at most 20
   distinct values per column. Rare PII patterns in tail data may not
   show up. The token-name match catches most of this; the
   `--registry-extension` and `--use-llm` paths catch more still.

---

## What this tool deliberately does NOT do

- **Modify your raw registry tables.** It only `CREATE`s a new schema,
  views in it, and the `insight_ai` role. Source-schema objects are
  read-only to the tool.
- **Decide policy.** k-anonymity threshold, regulatory classifications
  (`pii` vs `quasi` for borderline columns) are operator decisions.
  The tool's job is to expose every classification for review, not to
  make policy.
- **Store the role password.** Printed once to stdout — you put it in
  your K8s secret. Nothing on disk.
- **Configure the LLM provider.** Uses your existing `INSIGHT_LLM__*`
  env vars; if you haven't set them and pass `--use-llm`, classification
  falls back to "unclear" for ambiguous cases.
- **Deploy Helm.** The tool writes a Helm overlay file; the human runs
  `helm upgrade` with it.
- **Auto-generate materialized views.** Auto-suggestion was removed — it
  was registry-specific and brittle. The proposal YAML ends with a
  commented declarative example showing the eventual
  `{dimensions:[…], metrics:[…]}` grammar. For now, hand-add MVs to the
  proposal as needed.

---

## Limitations / roadmap

| Limitation | Workaround | Roadmap |
|------------|-----------|---------|
| Aggregate MV codegen still TODO | Hand-write `materialized_views:` block in proposal | Declarative grammar — already documented in the proposal YAML comment block |
| Refresh scheduling is a free-text string (`refresh:`) — no pg_cron wiring | Set up your own cron / pg_cron | Optional pg_cron integration when present |
| Distinct-counting is slow on un-analyzed tables (`COUNT(DISTINCT col)` per column) | Use `--registry-extension` to skip sampling on classifier-resolvable columns; or run `ANALYZE` on the registry first | Done for extension-led mode — sampling only fires when type+name+extension all return unclear |
| `--registry-extension` is path-based, not module-name based | Just point at the source tree | Could add module-name resolution via `importlib.util.find_spec` |
| No `unapply` command | Manually `DROP SCHEMA insight_safe CASCADE; DROP ROLE insight_ai;` or re-run `apply` with a new proposal | Record an `applied.yaml` so an inverse exists |
| Audit only covers PII listed in the proposal | Re-run `audit` after registry schema changes; wire into CI | Generate an audit GitHub Action |

---

## See also

- [onboarding-new-registry.md](onboarding-new-registry.md) — the
  hand-execution runbook this tool automates
- [catalog-format.md](catalog-format.md) — format of the operational
  catalog YAML the tool writes
- [pii-strategy.md](pii-strategy.md) — the layered model the tool enforces
- [`default_pii_rules.yaml`](../backend/scripts/insight_onboard/default_pii_rules.yaml)
  — the bundled keyword sets and skip patterns
- [`extension_introspect.py`](../backend/scripts/insight_onboard/extension_introspect.py)
  — the AST walker that pulls metadata from OpenG2P extension packages
