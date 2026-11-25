# G2P Insight

**AI-driven analytics and insight platform for OpenG2P registries.**

G2P Insight lets non-technical administrators, analysts, and data scientists explore registry data through:

1. **Dashboards** — fixed, drillable visualizations served by Apache Superset, authored either manually or generated from natural language.
2. **Ad-hoc natural-language queries** — typed questions are translated to safe SQL, executed locally, and returned as result + chart + assumptions.
3. **Expert analytics** — statistical functions (distributions, correlations, overlaps, cross-tabs) operating on aggregated, PII-stripped data.

Built to be deployed per-country into the existing OpenG2P Kubernetes stack alongside services like `openg2p-audit-manager`, registries (e.g. `farmer-registry`), and the OpenG2P Master Data Service.

---

## Table of contents

1. [Scope and non-goals](#1-scope-and-non-goals)
2. [Architecture](#2-architecture)
3. [Repository layout](#3-repository-layout)
4. [Tech stack](#4-tech-stack)
5. [Schema catalog — generation and structure](#5-schema-catalog--generation-and-structure)
6. [Master Data Service integration](#6-master-data-service-integration)
7. [Core data flows](#7-core-data-flows)
8. [PII protection](#8-pii-protection)
9. [Cost gate and query safety](#9-cost-gate-and-query-safety)
10. [k-anonymity and result safety](#10-k-anonymity-and-result-safety)
11. [Roles and access](#11-roles-and-access)
12. [Frontend — UI and branding](#12-frontend--ui-and-branding)
13. [Deployment — Docker, Helm, Kubernetes](#13-deployment--docker-helm-kubernetes)
14. [Configuration](#14-configuration)
15. [Audit and observability](#15-audit-and-observability)
15a. [LLM choice and open-source alternatives](#15a-llm-choice-and-open-source-alternatives)
16. [Roadmap](#16-roadmap)
17. [Open items](#17-open-items)

---

## 1. Scope and non-goals

### MVP scope

| In | Out (deferred) |
|----|----------------|
| FastAPI backend on `openg2p-fastapi-common` | Keycloak login (post-MVP; stubbed admin token for now) |
| Schema catalog generator from registry SQLAlchemy models | Cube.dev semantic layer |
| Per-deployment YAML catalog (manual edits after generation) | OpenSearch / Elasticsearch backend |
| Local PII proxy (Presidio + spaCy) before any cloud LLM call | Local / air-gapped LLM |
| Cloud LLM (Anthropic Claude) via tool-use; key per deployment | Air-gapped deployment mode |
| NL → intent JSON → safe SQL → execute locally → result + viz | Differential privacy noise |
| Multi-turn **clarification loop** when intent is ambiguous (LLM asks; user answers via option buttons or free text) | — (in MVP) |
| Generated SQL **always visible** to the user for review (no approval required) | — (in MVP) |
| Cost gate (EXPLAIN + static checks) with user warnings | Empirical cost-threshold auto-tuning |
| k-anonymity on results (default `k=5`, per-deployment override) | Per-role `k` thresholds |
| Generic statistical engine; one reference function (e.g. distribution) | Full library of expert functions |
| Apache Superset chart authoring via REST API (configurable URL) | Superset shipped under our chart |
| Geographic drill-down using Master Data Service `/geo/*` endpoints | Multi-language UI (English only in MVP) |
| React + TypeScript frontend with G2P Insight branding | Watermarked exports |
| Backend-rendered chart preview before Superset save | Multi-turn conversation memory |
| Helm chart following OpenG2P conventions; deployable to K8s | Multi-tenant isolation |
| Audit log written to local DB (integration with central audit later) | — |

### Non-goals

- **Not a transactional system.** All DB access by the analytics role is read-only against PII-stripped views and materialized views.
- **Not a custom BI tool.** Standard dashboarding leans on Apache Superset; we do not reimplement what Superset gives us.
- **Not air-gapped in MVP.** The cloud LLM is required. Air-gapped LLM is a v2 capability.

---

## 2. Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│  React + TypeScript frontend (g2p-insight-ui)                          │
│  Title bar: #061327 (almost-black) with G2P Insight dark logo          │
│  ┌──────────────┬──────────────┬──────────────┬─────────────────────┐  │
│  │ Dashboards   │ Ask          │ Expert Lab   │ Admin               │  │
│  │ (Superset    │ (NL chat,    │ (Plotly,     │ (schema catalog,    │  │
│  │  embedded)   │  chart       │  drill-down  │  audit log)         │  │
│  │              │  render)     │  map)        │                     │  │
│  └──────────────┴──────────────┴──────────────┴─────────────────────┘  │
└───────────────────────────┬──────────────────────────┬─────────────────┘
                            │                          │ Superset
                            │ REST                     │ embedded-sdk
                            ▼                          ▼
┌──────────────────────────────────────────────┐  ┌─────────────────────┐
│  g2p-insight (FastAPI, openg2p-fastapi-      │  │  Apache Superset    │
│  common)                                      │  │  (existing OpenG2P  │
│                                               │  │   deployment, e.g.  │
│  ┌─────────────────────────────────────────┐ │  │   superset.qa2.     │
│  │ PII Service (Presidio + spaCy)          │ │  │   openg2p.org)      │
│  └──────────────────┬──────────────────────┘ │  │                     │
│                     ▼                         │  │  Configurable URL.  │
│  ┌─────────────────────────────────────────┐ │  └──────────┬──────────┘
│  │ Schema Service                          │ │             │
│  │ (catalog YAML + pgvector retrieval)     │ │             │
│  └──────────────────┬──────────────────────┘ │             │
│                     ▼                         │             │
│  ┌─────────────────────────────────────────┐ │             │
│  │ LLM Service (Claude tool-use loop)      │◄┼─ schema +   │
│  │   - get_schema, get_exchange_rate,      │ │  sanitized  │
│  │     get_geo_levels, propose_intent      │ │  query only │
│  └──────────────────┬──────────────────────┘ │             │
│                     ▼                         │             │
│  ┌─────────────────────────────────────────┐ │             │
│  │ SQL Planner (intent JSON → safe SQL)    │ │             │
│  └──────────────────┬──────────────────────┘ │             │
│                     ▼                         │             │
│  ┌─────────────────────────────────────────┐ │             │
│  │ Cost Gate (EXPLAIN + static analysis)   │ │             │
│  └──────────────────┬──────────────────────┘ │             │
│                     ▼                         │             │
│  ┌─────────────────────┬───────────────────┐ │             │
│  │ Query Executor      │ Analytics Engine  │ │             │
│  │ (read-only role,    │ (pandas/scipy,    │ │             │
│  │  statement timeout, │  function         │ │             │
│  │  k-anonymity)       │  registry)        │ │             │
│  └──────────┬──────────┴─────────┬─────────┘ │             │
│             │                    │           │             │
│             ▼                    ▼           │             │
│  ┌─────────────────────┬───────────────────┐ │             │
│  │ Superset Client     │ Audit Logger      │─┼─────────────┘
│  └─────────────────────┴───────────────────┘ │
└──────────────────┬───────────────────────────┘
                   │ SELECT only on safe views
                   ▼
┌───────────────────────────────────────────────────────────────────────┐
│  PostgreSQL                                                            │
│  ┌────────────────────────────┐  ┌────────────────────────────────┐   │
│  │ Registry schemas           │  │ insight schema (owned by app)  │   │
│  │ (e.g. farmer-registry):    │  │   - audit_log                  │   │
│  │   raw tables (have PII)    │  │   - chat_session               │   │
│  │   v_*_safe (PII stripped)  │  │   - saved_chart                │   │
│  │   mv_* (materialized,      │  │   - catalog_versions           │   │
│  │         aggregated)        │  │   - schema_embeddings          │   │
│  │   pgvector index           │  │     (pgvector)                 │   │
│  └────────────────────────────┘  └────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘

External:
  • OpenG2P Master Data Service  → /geo/get_g2p_geo_levels, /geo/get_g2p_geo_level_values
  • Anthropic Claude API          → only sanitized query + schema metadata leave the cluster
  • OpenG2P Audit Manager         → integration target post-MVP
  • Keycloak                      → integration target post-MVP
```

---

## 3. Repository layout

```
g2p-insight/
├── README.md                          ← this file
├── LICENSE                            ← MPL 2.0 (OpenG2P standard)
│
├── backend/                           ← FastAPI service (g2p-insight)
│   ├── pyproject.toml
│   ├── Dockerfile                     ← multi-stage, python:3.13-slim
│   ├── docker-entrypoint.sh
│   ├── docker-compose.yaml            ← local-dev convenience
│   ├── config/
│   │   └── default.yaml
│   ├── src/openg2p_insight/
│   │   ├── __init__.py
│   │   ├── app.py                     ← Initializer (extends common)
│   │   ├── config.py                  ← Settings (extends common Settings)
│   │   ├── controllers/
│   │   │   ├── chat_controller.py            # POST /ask, GET /ask/{sid}
│   │   │   ├── dashboard_controller.py       # POST /dashboards/charts
│   │   │   ├── analytics_controller.py       # POST /analytics/{function}
│   │   │   ├── catalog_controller.py         # GET/PUT /catalog
│   │   │   ├── geo_controller.py             # GET /geo/levels, /geo/values
│   │   │   └── audit_controller.py
│   │   ├── services/
│   │   │   ├── pii_service.py
│   │   │   ├── schema_service.py
│   │   │   ├── llm_service.py
│   │   │   ├── sql_planner_service.py
│   │   │   ├── cost_gate_service.py
│   │   │   ├── query_executor_service.py
│   │   │   ├── analytics_service.py
│   │   │   ├── superset_service.py
│   │   │   ├── masterdata_service.py
│   │   │   └── audit_service.py
│   │   ├── models/                            # SQLAlchemy (insight schema)
│   │   ├── schemas/                           # Pydantic
│   │   ├── prompts/                           # Claude system prompts
│   │   ├── analysis_functions/                # registered Python fns
│   │   │   ├── __init__.py                    #   ANALYSIS_REGISTRY
│   │   │   ├── distribution.py
│   │   │   ├── correlation.py
│   │   │   ├── crosstab.py
│   │   │   └── overlap.py
│   │   └── utils/
│   ├── catalog/                       ← per-deployment YAML, mounted via ConfigMap
│   │   ├── schema.example.yaml
│   │   ├── pii_recognizers.example.yaml
│   │   └── viz_templates.example.yaml
│   ├── scripts/
│   │   └── generate_catalog.py        ← see §5; inspects registry SQLAlchemy models
│   ├── migrations/                    ← Alembic
│   └── tests/
│
├── frontend/                          ← React + TS + Vite (g2p-insight-ui)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── Dockerfile                     ← multi-stage; nginx for runtime
│   ├── nginx.conf
│   ├── public/
│   │   └── logos/                     ← G2P INSIGHT dark logo, copied from branding
│   ├── src/
│   │   ├── theme.ts                   ← Ant Design theme with G2P palette
│   │   ├── pages/
│   │   │   ├── Dashboards.tsx
│   │   │   ├── Ask.tsx
│   │   │   ├── ExpertLab.tsx
│   │   │   ├── Catalog.tsx
│   │   │   └── Audit.tsx
│   │   ├── components/
│   │   │   ├── TitleBar.tsx           ← #061327 background, dark logo
│   │   │   ├── ChatPanel.tsx
│   │   │   ├── ChartRenderer.tsx      ← Plotly
│   │   │   ├── DrilldownMap.tsx       ← deck.gl
│   │   │   ├── CostWarning.tsx
│   │   │   ├── AssumptionsPanel.tsx
│   │   │   ├── PiiBadge.tsx
│   │   │   └── SupersetEmbed.tsx
│   │   ├── api/                       ← generated from backend OpenAPI
│   │   ├── auth/                      ← Keycloak adapter, stubbed in MVP
│   │   ├── i18n/                      ← English only in MVP, scaffold present
│   │   └── App.tsx
│   └── tests/
│
├── helm/
│   └── g2p-insight/                   ← umbrella chart
│       ├── Chart.yaml                 ← depends on common-2.30.0, postgres-init-1.1.0
│       ├── values.yaml
│       ├── templates/
│       │   ├── _helpers.tpl
│       │   ├── configmap-catalog.yaml ← mounts catalog/*.yaml
│       │   └── secret-llm.yaml        ← Claude API key (stub; provided externally)
│       ├── charts/
│       │   ├── insight-backend/
│       │   └── insight-frontend/
│       └── questions.yaml             ← Rancher catalog metadata
│
└── docs/
    ├── architecture.md
    ├── catalog-format.md
    ├── pii-strategy.md
    └── llm-prompts.md
```

---

## 4. Tech stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend framework | **FastAPI** via [openg2p-fastapi-common](https://github.com/OpenG2P/openg2p-fastapi-common) | Controller / Service / Component pattern; same as `audit-manager`, `farmer-registry`. |
| Python runtime | 3.13 (slim) | Match `audit-manager` base. |
| Async DB | SQLAlchemy 2.x async + asyncpg | Common library convention. |
| Migrations | Alembic | Standard. |
| LLM | **Anthropic Claude** via official SDK, tool-use loop | Per-deployment API key (Kubernetes secret). |
| PII | **Microsoft Presidio** + **spaCy `en_core_web_lg`** | Country-specific recognizers in catalog. |
| Embeddings (local) | **sentence-transformers `all-MiniLM-L6-v2`** + **pgvector** | Schema RAG without cloud calls. |
| Stats | **pandas, scipy, numpy, statsmodels** | Function registry pattern. |
| BI engine | **Apache Superset** (existing OpenG2P deployment) | URL configurable, e.g. `superset.qa2.openg2p.org`. |
| Frontend | **React 18 + TypeScript + Vite** | Modern, fast. |
| UI kit | **Ant Design 5** themed to G2P palette | Strong dashboard primitives. |
| Charts (custom) | **Plotly.js** | Analytical, exportable, interactive. |
| Maps | **deck.gl + MapLibre GL** (open source basemap-friendly) | Hierarchical drill-down. |
| Superset embed | `@superset-ui/embedded-sdk` | Official. |
| Fonts | **Roboto** + **Roboto Slab** | Per OpenG2P branding. |
| Container | Docker (multi-stage) | Same shape as `audit-manager/Dockerfile`. |
| Orchestration | Kubernetes + Helm | OpenG2P helm conventions. |
| Helm common chart | `common@2.30.0` from `https://openg2p.github.io/openg2p-helm` | As used by audit-manager. |
| DB bootstrap | `postgres-init@1.1.0` from the same repo | Same pattern. |
| Service mesh | Istio VirtualService | Same pattern as audit-manager. |
| Observability | OpenTelemetry → existing OpenG2P stack | Wired post-MVP. |

---

## 5. Schema catalog — generation and structure

> **Onboarding a new registry? Two paths, same result:**
> - **Automated (default)** — `insight-onboard` CLI:
>   [docs/insight-onboard.md](docs/insight-onboard.md). Three commands
>   (`scan` / `apply` / `audit`) take a fresh registry DB to a deployed
>   catalog in ~30 minutes. The human reviews a single proposal YAML
>   before any DDL runs. For OpenG2P registries, pointing the CLI at the
>   extension's SQLAlchemy models (`--registry-extension`) adds
>   authoritative analytical-entity detection and enum-value extraction.
> - **Manual** — DBA runbook:
>   [docs/onboarding-new-registry.md](docs/onboarding-new-registry.md) —
>   7 steps with copy-pasteable SQL/YAML. Use this when the CLI doesn't
>   fit (highly non-standard schema, no admin available to install the
>   tool, or you need line-by-line SQL review for compliance sign-off).
>   The runbook now shows the CLI shortcut alongside each manual step.
>
> Both paths produce the same artifacts: PII-safe views in a dedicated
> `insight_safe` schema, an `insight_ai` role with SELECT-only grants,
> the operational catalog YAML, and a Helm values overlay.
>
> **After onboarding** — to actually run Insight against the new
> registry (local-dev `.env` switchover for smoke-test, then the K8s
> rollout): see
> [docs/connecting-to-the-new-registry.md](docs/connecting-to-the-new-registry.md).
> Its top-of-doc "TL;DR — three terminals" block is the fastest path.
>
> This section covers the catalog *format*; the runbook is the *recipe*;
> the CLI is the *automation* over the recipe; the connecting doc is
> *how you actually start serving*.

### Approach

The schema catalog is the single source of truth that makes G2P Insight country-agnostic and registry-agnostic. The platform itself knows nothing about specific tables, columns, or PII semantics until the catalog is loaded.

**Generation:**

1. As a **first step**, the catalog is **generated by a script** that inspects the SQLAlchemy models of the target registry extension (e.g. `farmer-registry/farmer-extension/src/openg2p_registry_farmer_extension/register_domain/models/*.py`).
2. The generator emits a draft `schema.yaml` listing every table, column, type, nullability, foreign keys, and a best-effort PII classification based on column-name heuristics (`name`, `phone`, `email`, etc.).
3. After generation, the operator **manually edits** the YAML to:
   - Adjust PII classifications (`safe`, `quasi`, `pii`)
   - Add column descriptions (used by the LLM for query understanding)
   - Add units (e.g. `unit: ETB`, `unit: kg`)
   - Mark columns as indexed
   - Add row-count estimates and notes
   - Add enum value lists where applicable
4. Changes made directly in the database after generation are tracked manually — the operator re-runs the generator and merges, or edits the YAML by hand. The catalog file is versioned in Git per deployment.

A future iteration may add database introspection (over `information_schema`) and a Catalog Admin UI; not in MVP.

### Format

```yaml
# catalog/schema.yaml
deployment:
  country_code: ET
  default_currency: ETB
  k_anonymity: 5                              # per-deployment override of default
  master_data_service_url: ${MDS_URL}

tables:
  - name: g2p_register_crops
    schema: public
    description: "Crop records per registered farmer"
    row_count_estimate: 12000000
    indexed: [internal_record_id, season]
    pii_class_table: safe                     # majority class
    columns:
      - {name: internal_record_id, type: int,    pii: quasi, description: "Internal record id"}
      - {name: commodity,          type: string, pii: safe,  description: "Crop commodity name"}
      - {name: planted_date,       type: date,   pii: safe}
      - {name: season,             type: string, pii: safe,  enum: [Belg, Meher]}
      - {name: end_use,            type: enum,   pii: safe,  enum_source: CropEndUseEnum}

  - name: mv_individuals_summary
    schema: public
    description: "Pre-joined individuals with demographics, latest income, geography"
    row_count_estimate: 47000000
    refresh: "nightly at 02:00 UTC"
    indexed: [region_code, zone_code, woreda_code, gender, age_bracket]
    columns:
      - {name: id,              type: int,    pii: pii}
      - {name: gender,          type: enum,   pii: safe,  enum: [M, F, O]}
      - {name: age_bracket,     type: enum,   pii: quasi, enum: [0-5,6-17,18-35,36-55,55+]}
      - {name: region_code,     type: string, pii: safe}
      - {name: zone_code,       type: string, pii: safe}
      - {name: woreda_code,     type: string, pii: safe}
      - {name: pmt_score,       type: float,  pii: safe}
      - {name: monthly_income,  type: float,  pii: safe, unit: ETB}

pii_columns_in_source:
  - {table: individuals, column: full_name}
  - {table: individuals, column: phone}
  - {table: individuals, column: fayda_id}

safe_views:                                   # what the AI role can SELECT
  - v_individuals_safe
  - mv_individuals_summary
  - mv_district_aggregates

geo_columns:
  - {column: region_code, level_mnemonic: region}
  - {column: zone_code,   level_mnemonic: zone}
  - {column: woreda_code, level_mnemonic: woreda}
  - {column: kebele_code, level_mnemonic: kebele}
```

### Catalog mounting in Kubernetes

The catalog files are mounted into the backend pod via a `ConfigMap` from `helm/g2p-insight/templates/configmap-catalog.yaml`, at `/app/catalog/`. The backend watches the directory and hot-reloads on changes.

---

## 6. Master Data Service integration

OpenG2P Master Data Service exposes (per the supplied OpenAPI):

| Endpoint | Purpose |
|----------|---------|
| `POST /geo/get_g2p_geo_levels` | List geo levels (region, zone, woreda, kebele, …); each has `level_id`, `level_mnemonic`, optional `parent_level_id` |
| `POST /geo/get_g2p_geo_level_values` | List values under a level; supports `parent_level_value_id` for drill-down |
| `POST /partner/get_all_partners` | List partners (post-MVP) |
| `POST /partner/get_partner` | Single partner (post-MVP) |

### MasterDataService client

The backend wraps these in `services/masterdata_service.py`:

- All calls send a `G2PRequestHeader` (`sender_app_mnemonic="g2p-insight"`, `sender_app_url`, generated `request_id`, current UTC timestamp).
- Responses are unwrapped through `response_header` / `response_body.response_payload`.
- Geo levels are fetched once at startup and refreshed every 24 hours; values are fetched lazily per `level_id` (and per `parent_level_value_id` for drill-down).
- Cache: in-memory TTL cache with optional persistent JSON snapshot.

### Drill-down behavior

The Expert Lab map and any geo-grouped chart:

1. Loads the country's level hierarchy (one HTTP call, cached).
2. Initial render groups by the top level (e.g. `region`).
3. On click, fetches children using `parent_level_value_id = clicked_value_id` and re-aggregates the current SQL with a deeper `GROUP BY`.
4. The `level_mnemonic` from MDS maps to the column name via the `geo_columns` section of the catalog.

The level hierarchy is **not hardcoded** — multiple countries can be onboarded with different numbers of levels and different mnemonics.

---

## 7. Core data flows

### Flow A — Natural-language ad-hoc query (conversational, four-tool routing)

The LLM is given **four tools** and must call exactly one per turn. It is
effectively a classifier over the user's question. Each tool maps to a
distinct downstream pipeline; this is what keeps the system safe and
testable instead of "ask an LLM to do anything".

| Tool the LLM calls | What kind of question it handles | What the platform does next |
|--------------------|----------------------------------|----------------------------|
| `propose_intent` | Aggregation (count / sum / avg / count_distinct / min / max, with filters / group_by) | Build safe SQL → cost-gate → execute → return rows + viz |
| `propose_expert_redirect` | Statistical analysis (group comparison, correlation, distribution, cross-tab) — anything that needs a real test, not an aggregate | Frontend renders a "Run in Expert Lab" card with the function + params pre-filled |
| `propose_direct_answer` | **Meta-questions** the LLM can answer from the schema/system info already in its prompt — *"what tables exist?"*, *"what values can `gender` take?"*, *"help"* | Text answer rendered directly. No SQL, no executor, no cost gate. |
| `request_clarification` | Ambiguous question — multiple plausible interpretations, missing scope, unmapped enum value, etc. | Frontend renders the question + 2-4 clickable options (and a free-text input). User answers, loop continues. |

**The whole flow:**

```
Turn 1 ── Browser → POST /ask {question, session_id?}
  │
  ├─ PiiService.sanitize(question)              (local, never network)
  │   • Presidio + spaCy NER → placeholders, reverse map per session
  │
  ├─ Build history = prior session.messages    (multi-turn conversation)
  │
  ├─ SchemaService.retrieve_relevant(question)  (keyword + future pgvector)
  │
  ├─ LlmService.plan(sanitized, schemas, history)
  │   • LLM picks ONE of the four tools
  │   • Returns Intent | Clarification | ExpertRedirect | DirectAnswer
  │
  ├─ dispatch on the result type:
  │   • Clarification    → status="needs_clarification" (loop continues
  │                        on /ask/clarify, full history forwarded)
  │   • ExpertRedirect   → status="expert_redirect" {function, params,
  │                        explanation} — frontend deep-links to Expert Lab
  │   • DirectAnswer     → status="direct_answer" {answer, source} — rendered
  │                        as plain text; no further pipeline runs
  │   • Intent           → continue to planning ↓

Intent committed:
  ├─ SqlPlannerService.build(intent, placeholder_map)
  │   • Allow-listed views/columns only; enum values validated
  │   • Parameterised SQL; PII placeholders re-injected here
  │   • ORDER BY aliases ("count","sum",...) normalised to actual SELECT alias
  │
  ├─ CostGateService.assess(sql)               (static + EXPLAIN)
  │   • RED     → status="rejected"             (SQL still shown)
  │   • YELLOW  → status="needs_confirmation"   (Run anyway button)
  │   • GREEN   → execute immediately
  │
  ├─ QueryExecutorService.run(sql)             (insight_ai role, 30s timeout)
  ├─ KAnonymity.apply(rows, group_by, k)
  ├─ AuditService.record(user, question, sql, cost, rows)
  │
  └─ Append assistant-side summary to session.messages so the *next* turn
     can reference this one ("computed avg(monthly_income) WHERE gender='F'.
     Result: total=2840."). This is what makes "and of men?" work.
```

**Conversational follow-ups.** The frontend keeps a sticky `session_id`
across questions. The backend threads `session.messages` into the LLM
prompt as history. The system prompt has a *"conversational continuity"*
rule telling the model to interpret short follow-ups (*"and of men?"*,
*"what about Tigray?"*) as **refinements** of the prior Intent — carry over
entity, metric, and untouched filters. The UI shows a *"Send follow-up"*
button when a conversation is going and an explicit *"New question (clear
conversation)"* button to reset.

**Escape from clarification.** The clarification card includes a
*"← Start over with a new question"* link so users never get trapped if
they don't want to answer the LLM's question.

**SQL visibility.** Every Intent-committed response carries the generated
SQL. The UI shows it in a collapsible right-side panel. The user can always
review it; we don't block on approval — the cost gate decides whether to
run.

**When the LLM asks for clarification.** Examples:
- *"Show me women with low income in Oromia"* → which `monthly_income`
  (individual or household)? what threshold for "low"?
- *"Recent registrations"* → what time window?
- *"Displaced households"* → IDP, REFUGEE, RETURNEE, or any of those?
- *"Average PMT in Bahir Dar"* → no geo code matches; the LLM offers the
  closest zone or asks for the woreda.

**When the LLM redirects to Expert Lab.** Trigger words: *correlation*,
*related*, *predict*, *significant*, *differ*, *distribution*, *spread*,
*variance*, *association*. The redirect carries the function name and
suggested parameters; the frontend deep-links into Expert Lab with those
pre-filled via `sessionStorage`.

**When the LLM answers directly.** Meta-questions about the system —
*"what tables exist?"*, *"what columns are in mv_individuals_summary?"*,
*"what can I ask about?"*, *"what enum values does gender take?"*. The
LLM composes a plain-text answer from the schema/system info already in
its prompt. No SQL.

### Flow B — Superset dashboards (embedded) + NL chart authoring

The Dashboards page has two complementary surfaces:

1. **Embedded view of existing Superset dashboards** — Insight lists every
   dashboard the service account can see; clicking one renders it in-place
   via `@superset-ui/embedded-sdk` (short-lived guest tokens minted by our
   backend). The user never navigates to Superset. See
   [docs/dashboards-embedded.md](docs/dashboards-embedded.md) for setup and
   how guest tokens work.

2. **NL chart authoring** — the "Add chart from natural language" affordance
   on the Dashboards page builds an Intent JSON from the user's question,
   previews it locally, and pushes the saved chart to Superset via REST.
   Two endpoints, three frontend actions:

```
Step 1 ── Browser → POST /dashboards/charts/preview {question}
  │
  ├─ PII → Schema → LLM (returns a chart-oriented Intent: group_by, metric, viz)
  ├─ SqlPlanner.build()  → safe SQL
  ├─ CostGate.assess()   → GREEN/YELLOW/RED report
  ├─ QueryExecutor.run(preview=True)  → first N rows for the preview table
  ├─ SupersetService.build_form_data(intent) → Superset-shaped form_data dict
  │
  └─ → { status: "preview", cost, form_data, viz_spec, preview_rows,
         assumptions, sql }

Step 2 ── Frontend displays preview (cost banner, SQL panel, suggested
          viz type, preview rows, assumptions). User can click Refine
          (restart) or Save.

Step 3 ── Browser → POST /dashboards/charts/save {name, dataset, form_data}
  │
  ├─ SupersetService.create_chart():
  │     • Resolves dataset_id from the table_name via /api/v1/dataset
  │     • POST /api/v1/chart with:
  │         slice_name, viz_type, datasource_id, datasource_type="table"
  │         params = JSON.dumps(form_data)        ← Superset wants a STRING here
  │     • Returns the chart_id
  │
  ├─ optional: attach to a dashboard_id
  │
  └─ → { status: "saved", chart_id }
```

**Preview is non-destructive.** Nothing is created in Superset until the user
explicitly clicks Save. The preview runs through the same cost gate as `/ask`,
so an expensive chart query gets flagged before any data leaves Postgres.

**Frontend (Dashboards page).** Empty by default with a single CTA
"**Add chart from natural language**". Clicking it shows a question textarea
+ optional chart name, then a Preview button. The preview view shows cost,
collapsible SQL, suggested viz type, first preview rows, and assumptions —
followed by **Save to Superset** and **Refine question**. After save, an
alert links straight to the chart in Superset (`/explore/?slice_id=<id>`).

### Flow C — Expert analytics

```
Browser → POST /analytics/{function} {filters, params}
  │
  ├─ AnalyticsService.dispatch(function_name)
  │   • Looks up ANALYSIS_REGISTRY[function_name]
  │   • The LLM is NOT used to execute Python; it only suggests which
  │     function to call (or admin picks one directly)
  │
  ├─ Function emits an extraction SQL → CostGate → execute → DataFrame
  │
  ├─ Function runs pandas/scipy/numpy on the DataFrame
  │
  └─ Returns {result_data, viz_spec, assumptions}
```

The analytics layer is generic. PMT-score-distribution is one **example** registered function (`analysis_functions/distribution.py`) — the platform does not bake in PMT semantics; the function is parameterized over column name, bin count, fit family.

#### Design philosophy: analysis primitives, not per-question functions

The registry today is:

| Primitive | Inputs | Answers |
|-----------|--------|---------|
| `distribution` | numeric column | histogram + percentiles + fitted distributions |
| `group_compare` | numeric metric + categorical group | per-group stats + t-test / Welch / ANOVA / Mann-Whitney + Cohen's d + boxplot |
| `correlation` | list of columns | correlation matrix (numeric × numeric, categoricals one-hot-encoded; high-cardinality columns skipped with a warning) |
| `crosstab` | two categorical columns | contingency table + chi-square |

These are **primitives**, not "one function per anticipated question". The
goal is that the next ~3 primitives (`time_series`, `regression`, `summarize`)
cover the long tail of analytical questions without code changes per
question. When something doesn't fit a primitive, the answer is *another
primitive* — not sandboxed code generation. Sandboxed Python is a v2
option we don't need yet.

#### Guards on every analysis call

- **60-second wall-clock timeout** wrapped around every function in
  `AnalyticsService.run()` via `asyncio.timeout()`. Plus the DB role's
  30-second `statement_timeout` bounds the SQL extract.
- **Default `sample_size`** per function (5k for correlation, 50k for
  distribution / crosstab) — operators override per request.
- **High-cardinality cap** in correlation: any categorical with > 30
  distinct values is skipped (and called out in `assumptions`) to keep
  `pd.get_dummies` tractable.
- **Step-level logs** so a slow function shows you which step (extract /
  encode / compute) is the bottleneck.

---

## 8. PII protection

PII protection is **layered**, not a single check.

### Layer 1 — Local query sanitization
Before any cloud LLM call, `pii_service.py`:
- Runs Presidio with spaCy NER + country-specific recognizers loaded from `catalog/pii_recognizers.yaml`.
- Replaces detected entities with stable placeholders (`[PERSON_1]`, `[ID_1]`).
- Saves the reverse map in the chat session for re-injection at SQL build time.
- Country-specific recognizers cover identifiers like Fayda token (ET), Aadhaar (IN), national ID formats, phone-number patterns, etc.

### Layer 2 — Schema-level redaction
The schema list sent to the LLM **excludes** any column tagged `pii: pii`. The LLM literally never sees that those columns exist.

### Layer 3 — Database role isolation
The `insight_ai` Postgres role:
- `SELECT` only on PII-stripped views (`v_*_safe`) and materialized views (`mv_*`).
- No access to the raw registry tables.
- `statement_timeout`, `lock_timeout`, `idle_in_transaction_session_timeout` set.

### Layer 4 — SQL validator
`utils/sql_safety.py` parses the LLM-produced SQL and rejects:
- Any non-`SELECT` (no `INSERT`/`UPDATE`/`DELETE`/`DROP`/`COPY`/`CREATE`).
- Any reference to a non-allow-listed table or column.
- Any `SELECT *`.
- Any cross join.

### Layer 5 — k-anonymity on results
See §10.

### What may leave the cluster

| Goes to Claude | Stays local |
|----------------|-------------|
| Sanitized question text (no PII) | Raw question text |
| Table & column names and descriptions (safe & quasi only) | PII column names, all PII values |
| Row count estimates and indexes | Result rows |
| Suggested intent JSON returned by Claude | Final SQL execution and results |
| Generic enums (e.g. gender values) | Names, phones, IDs, addresses |

---

## 9. Cost gate and query safety

Before any query touches the database, `cost_gate_service.py` runs:

1. **Static analysis** (instant)
   - SELECT-only? Else RED.
   - Join count ≤ 5? Else RED.
   - Has WHERE on a large table without an indexed column? Warn.
   - Mentions only allow-listed tables? Else RED.

2. **Planner estimate** (~ms)
   - `EXPLAIN (FORMAT JSON) <sql>` — no execution, just the planner's estimate.
   - Extract `Plan Rows` and `Total Cost`.
   - If EXPLAIN itself fails (bad column, etc.), we classify as **RED** — never as
     YELLOW. The reasoning: if Postgres can't even price the query, we will not
     run it.

3. **Classification**
   - `GREEN` — auto-execute.
   - `YELLOW` — return the cost report to the UI; the SQL is shown immediately
     and a `Run anyway` button posts to `/ask/confirm`, which reuses the cached
     intent (deterministic — does not re-call the LLM).
   - `RED` — refuse; the SQL is still shown for review; offer to reformulate.

4. **Suggested optimization**
   - If a matching materialized view exists in the catalog, suggest using it (`mv_district_aggregates` typically 10⁴× faster).

5. **Hard safety net**
   - Postgres role-level `statement_timeout = 30s` (analytics) / `120s` (expert).

The thresholds for GREEN/YELLOW/RED are configurable per deployment in `appConfig.costGate`.

**Long-running-query alerting.** Cost classification runs on every planned
query. YELLOW raises a banner in the UI with the estimated rows + bottleneck
and a one-click `Run anyway`. RED rejects outright with a reason. Combined
with the 30-second hard statement timeout this means the platform will not
silently sit on an expensive query.

---

## 10. k-anonymity and result safety

After execution, before returning to the UI:

- The SQL planner **always projects `COUNT(*) AS _k_count`** for any
  `GROUP BY` query, even when the user's chosen metric is `AVG` / `SUM` /
  `MIN` / `MAX`. This is the only way to enforce k-anonymity for non-count
  metrics — without an explicit count column we'd silently return averages
  over tiny cohorts (a real re-identification risk).
- Any group with `_k_count < k` (default `k = 5`, configurable per deployment
  in `catalog/schema.yaml` → `deployment.k_anonymity`) is **suppressed**: the
  group-by values become `<suppressed>` and the metric becomes `<k`.
- The executor **strips `_k_count` from the response** before it leaves the
  backend — the user never sees the internal column.
- **Fail-secure**: if the planner forgets to include `_k_count` on a grouped
  query (shouldn't happen, but defense-in-depth), `apply_k_anonymity` returns
  an empty result rather than the raw rows.
- Single-row "identify an individual" results are rejected at the validator
  layer (the SQL must contain an aggregation; raw-row results require explicit
  user opt-in and a role that allows it).
- Per-role k-thresholds are deferred to v2; MVP uses one k per deployment.

---

## 11. Roles and access

| Role | Dashboards | Ask | Expert Lab | Admin |
|------|-----------|-----|------------|-------|
| `insight.viewer` | View | — | — | — |
| `insight.analyst` | View + filter | aggregates only | — | — |
| `insight.expert` | View + filter | full | full | — |
| `insight.admin` | Author + view | full | full | catalog, audit |

Keycloak integration is **post-MVP**. For the MVP, the backend accepts a static admin bearer token (set via secret) and the frontend ships with no login screen. The middleware is structured so that swapping in `openg2p-fastapi-auth` is a localized change.

---

## 12. Frontend — UI and branding

### Branding (from `/repos/branding/guidelines.txt`)

| Color | Hex | Usage |
|-------|-----|-------|
| Yellow | `#F5BB1A` | Generous — primary accents, CTAs |
| Black (deep navy) | `#061327` | Generous — title bar, headings |
| White | `#FFFFFF` | Generous |
| Orange | `#F07B1A` | Medium — highlights, warnings |
| Magenta | `#88498F` | Sparingly |
| Grey | `#C4C4C4` | As needed |
| Light grey | `#F6F6F6` | Backgrounds |
| Blue | `#2A477A` | Rarely |

Fonts: **Roboto** (body) and **Roboto Slab** (display).

### Title bar

- Background `#061327` (almost-black), full width.
- Logo: `G2P INSIGHT dark.png` / `.svg` from `/repos/branding/logos/g2p-insight/DARK/`, copied into `frontend/public/logos/` at build time.
- Right side: user/role badge (a placeholder pre-Keycloak), language switcher (English only in MVP), help icon.

### Layout

- Left sidebar: top-level navigation (Dashboards, Ask, Expert Lab, Catalog, Audit).
- Main pane: route-dependent.
- All chart components consume a uniform `viz_spec` shape so backend-rendered previews and live charts share the same renderer.

### i18n

`i18n/` is scaffolded with English-only message catalogs in MVP. Adding a locale = drop in a JSON; no code changes.

---

## 13. Deployment — Docker, Helm, Kubernetes

The deployment shape mirrors `openg2p-audit-manager`.

### Docker images

Two images:

| Image | Base | Port |
|-------|------|------|
| `openg2p/g2p-insight-backend` | `python:3.13-slim` (multi-stage) | 8000 |
| `openg2p/g2p-insight-frontend` | `node:20-alpine` build → `nginx:alpine` runtime | 8080 |

Both produced by multi-stage Dockerfiles in `backend/Dockerfile` and `frontend/Dockerfile`. Build args `GIT_COMMIT` and `BUILD_TIME` are baked in for observability, same pattern as audit-manager.

### Helm chart

`helm/g2p-insight/Chart.yaml` follows OpenG2P conventions:

```yaml
apiVersion: v2
name: g2p-insight
description: "OpenG2P Insight — AI-driven analytics platform"
type: application
version: 0.0.0-develop
dependencies:
  - name: common
    repository: https://openg2p.github.io/openg2p-helm
    version: 2.30.0
  - name: postgres-init
    version: 1.1.0
    repository: https://openg2p.github.io/openg2p-helm
    condition: postgres-init.enabled
icon: https://openg2p.github.io/openg2p-helm/openg2p-logo.png
keywords: [openg2p, g2p-insight, analytics, ai]
maintainers:
  - {email: info@openg2p.org, name: OpenG2P}
annotations:
  catalog.cattle.io/display-name: "OpenG2P G2P Insight"
  openg2p.org/add-to-rancher: ""
```

### Pattern reuse

| From audit-manager | Reused in g2p-insight |
|---|---|
| `global.*` block (DB host, DB name, secrets) | yes |
| `postgres-init` subchart to create DB + user | yes |
| `istio.virtualservice` for path-based routing | yes (`/v1/insight/`) |
| ConfigMap-mounted YAML config at `/app/config/config.yaml` | yes |
| `envVars` / `envVarsFrom` (DB password from secret) | yes |
| Startup/liveness/readiness probes on `/health` | yes |
| `postgresCheckerInit` init container | yes |
| Build args `GIT_COMMIT`, `BUILD_TIME` | yes |
| `questions.yaml` for Rancher | yes |

### Additional resources in `templates/`

- `configmap-catalog.yaml` — mounts `catalog/*.yaml` into `/app/catalog/`. Operators override via `values.yaml` (`appCatalog.schemaYaml: |- …`).
- `secret-llm.yaml` — `ANTHROPIC_API_KEY`. Per-deployment, provided via Helm `--set` or external secret. Never committed.
- `secret-superset.yaml` — Superset API user credentials for chart authoring.

### Default ingress / routing

- Backend: `insight.<env>.openg2p.org/v1/insight/` via Istio VirtualService.
- Frontend: `insight.<env>.openg2p.org/` → frontend Service.
- Superset is **not** under our chart — we point at the existing deployment via `global.supersetUrl` (e.g. `superset.qa2.openg2p.org`).

---

## 14. Configuration

Per-deployment settings live in `values.yaml`. Highlights:

```yaml
global:
  insightHostname: insight.qa2.openg2p.org
  postgresqlHost: commons-postgresql

  insightDB: '{{ printf "%s" .Release.Name | replace "-" "_" }}'
  insightDBUser: '{{ printf "%s_user" .Release.Name | replace "-" "_" }}'
  insightDBSecret: '{{ .Release.Name }}'

  # External services — all overridable per deployment
  supersetUrl: https://superset.qa2.openg2p.org
  masterDataServiceUrl: https://masterdata.qa2.openg2p.org
  registryDBHost: commons-postgresql        # the registry data DB (read-only access)
  registryDBName: farmer_registry           # what we read from

  # LLM
  anthropicApiKeySecret: g2p-insight-llm
  anthropicApiKeyKey: anthropic-api-key
  llmModel: anthropic/claude-sonnet-4.5                 # current Claude model id

insightBackend:
  enabled: true
  replicaCount: 2
  appConfig:
    serviceId: openg2p.insight
    apiVersion: "1.0"
    catalogPath: /app/catalog
    pii:
      spacyModel: en_core_web_lg
    costGate:
      green:  {rows: 10000,    cost: 1000}
      yellow: {rows: 1000000,  cost: 100000}
    kAnonymity:
      defaultK: 5
    superset:
      url: '{{ tpl .Values.global.supersetUrl $ }}'
      username: insight-service-account
      passwordSecret: g2p-insight-superset
    masterData:
      url: '{{ tpl .Values.global.masterDataServiceUrl $ }}'
      senderAppMnemonic: g2p-insight

insightFrontend:
  enabled: true
  replicaCount: 2
  env:
    VITE_API_BASE_URL: https://insight.qa2.openg2p.org/v1/insight
    VITE_SUPERSET_URL: '{{ tpl .Values.global.supersetUrl $ }}'
```

---

## 15. Audit and observability

- Every NL query, generated SQL, cost class, row count, latency, user, role is written to `insight.audit_log` and exported via structured logs.
- LLM token usage is recorded per user.
- OpenTelemetry traces span PII → schema → LLM → planner → cost → exec → response.
- A built-in observability dashboard (created via the platform itself) shows queries/day, rejection reasons, slowest queries, top users.
- Post-MVP: emit CloudEvents to `openg2p-audit-manager` matching its existing schema.

---

## 15a. LLM choice and open-source alternatives

MVP runs against **Anthropic Claude**. For air-gapped deployments — and as a cost lever in cloud deployments — several open-source models do this workload reasonably well. This section captures the evaluation so future deployments can swap without re-scoping.

### What the LLM here actually needs to be good at

| Capability | Where it shows up in our code |
|------------|------------------------------|
| Tool-use / structured JSON output | Must pick exactly one of `propose_intent` / `request_clarification` and fill the schema |
| Reading a catalog and grounding in it | Use only allow-listed tables, declared enum values, real column names |
| Multi-turn reasoning | Carry the clarification thread, refine the Intent across turns |
| Following negative rules | "Don't invent values like `FEMALE` when enum is `{M,F,O}`" |
| Knowing when to ask | Tell ambiguous from clear; ask one targeted clarification rather than guessing |

The model does **not** need vision, code execution, long-context retrieval, or creative writing. The narrow profile means a 70B-class open model is usually enough.

### Top open candidates (as of early 2026)

| Model | Size | Tool use | Schema-grounding | Multi-turn | Notes |
|-------|------|----------|------------------|-----------|-------|
| **Qwen3-32B Instruct** | 32B | ★★★★ | ★★★★ | ★★★★ | **Top pick.** Apache 2.0. Native function calling. Hybrid thinking mode. Fits 1× A6000 at INT8. |
| **Qwen3-30B-A3B** (MoE) | 30B total / 3B active | ★★★★ | ★★★★ | ★★★ | Same family, faster inference at similar quality. vLLM `--enable-expert-parallel`. |
| **Qwen 2.5 72B Instruct** | 72B | ★★★★ | ★★★★ | ★★★ | Larger alternative. Needs 2× A100 80GB or 1× H100 80GB. Marginal quality gain for our task. |
| **Llama 3.3 70B Instruct** | 70B | ★★★★ | ★★★★ | ★★★ | Mature, well-supported by vLLM. Meta Llama Community License. Same hardware as 72B. |
| **DeepSeek V3** | 671B MoE (~37B active) | ★★★★ | ★★★★ | ★★★★ | Frontier-level reasoning. Open weights. Heavy to host but cheap inference per token. |
| **Mistral Small 3 24B** | 24B | ★★★ | ★★★ | ★★ | Smaller, fast. Good budget option. |
| SQLCoder-70B (Defog) | 70B | ★★ | ★★★★★ | ★ | SQL specialist; no function calling — would break our clarification + Intent JSON pipeline. |
| Gemma 2 27B | 27B | ★★ | ★★★ | ★★ | Decent; tool-use less polished. |

★ ratings are estimates for *this specific workload*, not general benchmarks.

### Recommended stack for OpenG2P deployments

1. **Default — Qwen3-32B Instruct.** Best quality among models that fit a single
   commodity GPU. Native function-calling (our `INTENT_TOOL_SCHEMA` works as-is),
   hybrid thinking mode for clarification routing, Apache 2.0. The same model
   runs on OpenRouter (for testing) and on the eventual self-hosted vLLM
   instance — **no model swap when you go on-prem**, just a base-URL change.
   **A6000 (48 GB) fit:** INT8 ~33 GB ✓ comfortable, INT4 ~17 GB ✓ ample
   headroom. Latency 8–15 s per turn at INT8.

2. **Fast alternative — Qwen3-30B-A3B (MoE).** 30 B total parameters but only
   3 B active per token — noticeably faster inference, quality close to dense
   32 B for our workload. Useful when latency on A6000 matters. Serving in
   vLLM needs `--enable-expert-parallel`.

3. **Larger-hardware alternative — Qwen 2.5 72B / Llama 3.3 70B.** When the
   deployment has 2× A100 80 GB or H100 80 GB available. Marginal quality
   gain for our specific task; not worth the extra hardware unless it's
   already there.

4. **Serving stack — vLLM.** OpenAI-compatible tool-use API. Means we swap
   Anthropic for an OpenAI client and point at our vLLM endpoint with minimal
   code changes (~30 lines, already on the roadmap as item 6).

**Hardware quick reference:**
- A6000 48 GB (single) → Qwen3-32B at INT8, or 30B-A3B MoE
- A100 40 GB (single) → Qwen3-32B at INT4
- A100 80 GB / H100 80 GB (single) → Qwen3-32B at FP16, or 72B-class at INT8
- 2× H100 80 GB → Qwen 2.5 72B at FP16

### Code changes needed to swap LLM providers

The LLM service is already abstracted. The change set is small:

1. **`config.py`** — extend `LlmConfig.provider` from `Literal["anthropic"]` to `Literal["anthropic", "openai_compatible"]`. Add `LlmConfig.base_url`.

2. **`services/llm_service.py`** — branch the client init:
   ```python
   if _config.llm.provider == "openai_compatible":
       from openai import AsyncOpenAI
       self._client = AsyncOpenAI(base_url=_config.llm.base_url, api_key="not-needed")
   else:  # anthropic
       from anthropic import AsyncAnthropic
       self._client = AsyncAnthropic(api_key=...)
   ```
   …then translate the tool-call shape (OpenAI uses `{type:"function", function:{...}}`; Claude uses `{type:"tool_use", name:...}`). ~30 lines.

3. **Helm** — values pivot from `anthropicApiKeySecret` to `llmBaseUrl` + a Service for the vLLM deployment in the same cluster (or out-of-cluster URL).

System prompt usually needs minor tightening when moving from Claude to open models — they're less forgiving of ambiguous instructions. Run the integration smoke against both and tune.

### Realistic tradeoffs vs Claude

| Aspect | Claude Opus 4.7 | Qwen3-32B (or 72B-class) |
|--------|----------------|--------------------------|
| Knows when to clarify | Excellent | Good — sometimes over-clarifies or commits when it shouldn't |
| Respects negative rules ("don't invent enum values") | Reliable | Mostly reliable; planner-side validator catches the rest |
| Multi-turn coherence over 4+ turns | Excellent | Good — occasionally loses thread |
| SQL correctness on materialised views | Excellent | Very good |
| SQL correctness on novel joins | Excellent | Weaker |
| Inference cost | ~$15/M output tokens | Hardware cost only — free at the margin |
| Air-gapped | No | Yes ✓ |
| Hardware | Just API | 2× A100/H100 |
| First-token latency | ~300ms | 50–200ms (local network) |
| Throughput | API-limited | Limited by GPU count |

The protections we built — cost gate, planner enum validation, allow-listed views, k-anonymity, clarification routing — compensate for the small intelligence gap. The biggest practical hit is **more clarification turns**: a question Claude would commit on, Qwen might ask about. That's a UX trade, not a correctness one.

### Implementation timeline

Tracked in §16 as roadmap item 6 (Air-gapped LLM).

| # | Step | Status |
|---|------|--------|
| 1 | Add `openai_compatible` branch in `LlmService` so the switch is one config flag away | **Done.** `INSIGHT_LLM__PROVIDER`, `INSIGHT_LLM__BASE_URL`, `INSIGHT_LLM__API_KEY`, `INSIGHT_LLM__MODEL` flip between providers. Tested against OpenRouter for Qwen3-32B. |
| 2 | Stand up a vLLM endpoint serving Qwen3-32B (test box first; fits 1× A6000) | Pending |
| 3 | Run integration smoke + a 20-question evaluation set against both Claude and Qwen3 | Pending |
| 4 | Tune the system prompt for the gap | Pending |
| 5 | Cut over per-deployment via Helm values | Pending |

**Switching providers today** is a single env-var edit:

```bash
# In backend/.env
INSIGHT_LLM__PROVIDER=openai_compatible
INSIGHT_LLM__BASE_URL=https://openrouter.ai/api/v1     # or your vLLM URL
INSIGHT_LLM__API_KEY=<provider key>
INSIGHT_LLM__MODEL=qwen/qwen3-32b
INSIGHT_LLM__ENABLE_THINKING=false                     # Qwen3 hybrid thinking
```

The `LlmService` translates between Anthropic's tool-use shape and the
OpenAI-compatible `tools[]` / `tool_calls[]` shape automatically. The four-
tool surface (§7 Flow A) works identically on either provider.

### On-prem / sovereignty deployment

For deployments that cannot use a cloud LLM at all — data residency regulations,
sovereign-cloud requirements, classified data, etc. — **the recommended path is
Qwen3-32B Instruct served by vLLM inside the customer's own infrastructure**.
Same model on OpenRouter for testing and on the self-hosted vLLM endpoint in
production — no model swap when going on-prem, just a base-URL change.
Fits a single A6000 (48 GB) at INT8 with comfortable headroom.

**Why this model family fits this codebase**

- Native function-calling support that is OpenAI-API-compatible — our
  `INTENT_TOOL_SCHEMA` and the multi-turn clarification loop work as-is.
- Apache 2.0 (Qwen) or Llama Community License — usable across deployments
  without per-country procurement friction.
- Multilingual — relevant for OpenG2P's deployment countries.
- Fits on a single modern data-centre GPU with vLLM (INT8 or INT4 quantisation).
- Air-gapped end-to-end: model weights, schema, sanitised queries, and results
  all stay on the customer's infrastructure. Nothing leaves the cluster.

**What we lose compared to Claude on this workload**

- More clarification turns on average — questions Claude would commit on, the
  open model often clarifies. UX cost, not a correctness one.
- Occasionally weaker SQL for novel joins. Rarely matters here because the
  workload is materialised-view-heavy.
- Slightly more enum hallucination — but the planner's enum validator (§9, §10)
  catches it before EXPLAIN.
- Per-query latency is bounded by local hardware rather than an API SLA.

**Why we did NOT pick SQLCoder-70B for on-prem**

SQLCoder is a strong NL→SQL specialist but a poor fit for our pipeline:

- No tool use / function calling — would break the clarification loop and the
  Intent-JSON contract the planner depends on.
- Single-turn by design — multi-turn refinement isn't supported.
- No viz / `assumptions` / `applies` metadata — would force a simplified UI.
- For schema-constrained NL→SQL specifically, general 70B models are nearly as
  good AND preserve the rest of the pipeline.

**Switch effort**

Tracked as roadmap item 6 (§16). End-state: add an `openai_compatible` provider
branch in `LlmService` (~30 lines), point `INSIGHT_LLM__BASE_URL` at the vLLM
endpoint, and update the Helm chart to reference the on-prem URL instead of
the Anthropic secret. Frontend is unchanged.

---

## 16. Roadmap

In priority order after the MVP lands:

1. Keycloak integration (`openg2p-fastapi-auth`) and per-role authorization.
2. `openg2p-audit-manager` integration via CloudEvents on Kafka.
3. Catalog Admin UI (in-app YAML edit with validation + preview).
4. Additional registered analysis functions (correlation matrix, UpSet overlap, cross-tabs, regression).
5. Cube.dev semantic layer with pre-aggregations (consistency + speed).
6. Air-gapped LLM (Qwen 2.5 / Llama 3.1 via vLLM, tool-use compatible).
7. OpenSearch as an optional fast analytical backend.
8. Differential privacy noise injection for high-sensitivity aggregates.
9. Multi-turn conversation memory in Ask.
10. Scheduled reports (PDF/XLSX) and watermarked exports.
11. Full i18n with locale switching.

---

## 16a. Verification

Three layers of tests:

| Layer | Where | What it covers |
|-------|-------|----------------|
| Unit tests | `backend/tests/test_*.py` | sql_safety, k-anonymity (incl. AVG/SUM fail-secure), schema-service allowlist, sql_planner (`_k_count`, IN expansion, unsafe identifier rejection), PII span merging, LLM clarification / intent / no-tool-fallback. |
| Integration test (Python) | `backend/tests/test_integration_flow.py` | Drives `/ask` → `/ask/clarify` → committed intent against the live test stack. Skipped automatically when `INSIGHT_BASE_URL` isn't reachable. |
| Integration smoke (script) | `test-stack/scripts/integration_smoke.py` (run via `make smoke`) | Verifies health, catalog, geo (live MDS), simple aggregate, PII redaction, clarification loop, cost-gate alerting on unbounded queries, k-anonymity field shape. Prints PASS/FAIL per check. |

Running the smoke against a local stack:

```bash
cd test-stack
make up
make seed-md   # snapshot live MDS geo
make seed      # 10k synthetic individuals
make smoke     # runs scripts/integration_smoke.py
```

---

## 17. Open items

These do not block scaffolding but should be resolved before the corresponding feature is built.

| # | Item | Blocks |
|---|------|--------|
| 1 | Confirm the registry source(s) we target first (farmer-registry only? others?) — affects which models the catalog generator parses initially. | Catalog generator. |
| 2 | Anthropic API key custody pattern in OpenG2P — per-deployment Kubernetes secret confirmed; agree on naming. | Helm secret. |
| 3 | Superset service account — does an `insight-service-account` already exist on `superset.qa2.openg2p.org` with chart-write permission, or do we provision one? | NL chart authoring (Flow B). |
| 4 | Country-specific PII recognizer patterns for the first deployment (e.g. Fayda token regex). | PII service first run. |
| 5 | Choice of basemap tile source for `deck.gl + MapLibre GL` (OpenStreetMap raster, MapTiler key, or self-hosted tiles). | Drill-down map. |
| 6 | Existing OpenG2P observability stack endpoints (OTLP collector) for wiring telemetry. | Observability wiring. |
| 7 | Approval flow for YELLOW queries — auto-approve for `insight.expert` and prompt only for `insight.analyst`, or always prompt? | Cost gate UX. |
| 8 | License files and copyright headers — MPL 2.0 as per OpenG2P; confirm. | LICENSE / headers. |

---

## License

Mozilla Public License 2.0 — same as the rest of OpenG2P.
