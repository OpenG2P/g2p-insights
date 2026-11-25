# OpenG2P Insight — Local Test Stack

End-to-end testbed. Brings up Postgres (port `5433`, isolated), Apache Superset,
the Insight backend, and the frontend on a single laptop with one command.

## What you get

| Service   | URL / port              | Credentials                                                              |
|-----------|-------------------------|---------------------------------------------------------------------------|
| Frontend  | http://localhost:5173   | Run with `make ui` (Vite dev server — live errors, no CORS)                |
| Backend   | http://localhost:8000   | health: `GET /v1/insight/health`                                          |
| Superset  | http://localhost:8088   | `admin` / `admin` (from `.env`)                                            |
| Postgres  | `localhost:5433`        | `postgres` / `postgres` (registry DB), `insight_ai` / `insight` (read-only) |

The Insight shell surfaces this as four tabs, all against the synthetic data:

- **Dashboards** — Superset 6.1 dashboards embedded via the embedded SDK. Seed a
  working sample with `make seed-dash` (see below).
- **Maps** — an Evidence.dev choropleth (region → zone drill) embedded as an
  iframe. Serve it with `make maps-host` (see below).
- **Ask / Expert Lab** — governed AI NL→SQL and registered statistical functions.

The Postgres container hosts three databases:

- `registry`  — synthetic NSR-modelled data the analytics platform queries
- `insight`   — the backend's own schema (audit_log, chat_session, etc.)
- `superset`  — Superset's metadata

The container loads `.sql` files from `postgres/init/` in order at first start:

```
00_databases.sql       ← creates insight + superset DBs
01_extensions.sql      ← pgvector, pg_trgm
02_nsr_schema.sql      ← NSR-modelled enums, tables, indexes
03_views_and_roles.sql ← v_*_safe views, mv_* materialized views, insight_ai role
```

## Prerequisites

- Docker Desktop 4.x or Docker Engine 24+
- `make`, `python3` (3.11+), `jq` (optional, for `make smoke`)
- Host deps for the seeders (they run on your machine, not in a container):
  `pip3 install --user httpx 'psycopg[binary]'`
  (Homebrew Python may need `--break-system-packages`, or use a venv. Use
  `psycopg[binary]` — plain `psycopg` compiles against libpq and often fails on macOS.)

## First-run workflow

```bash
cd test-stack
cp .env.example .env             # then add OPENROUTER_API_KEY if you have one
make up-full                     # backend + Superset (~3-4 min); frontend runs via `make ui`
make seed-md                     # snapshot Ethiopia geo from master-data.trial.openg2p.org
make seed                        # 10k individuals (idempotent — TRUNCATEs first)
make seed-dash                   # sample embedded "Registry Overview" dashboard in Superset
make smoke                       # health + count check
make ui                          # frontend dev server (Vite) — leave running (Terminal 1)
make maps-host                   # build + serve the Evidence Maps surface on :3100 (Terminal 2)
open http://localhost:5173       # frontend — all four tabs live
open http://localhost:8088       # Superset (admin/admin)
```

`make ui` and `make maps-host` both stay in the foreground — run them in
separate terminals. On a memory-constrained Docker VM (Colima), serve Evidence
from the host with `make maps-host` rather than the in-Docker `make maps`
(building the static site inside the VM alongside Superset can OOM).

`make seed-dash` uses form-login (session cookie), not JWT: Superset's REST
API treats a Bearer token as *anonymous* here, so writes would be ownerless and
lists return empty — the backend hit the same wall and works around it the same
way (see `backend/.../superset_service.py`).

## Master Data Service

The seeder talks to whatever URL is in `MASTERDATA_URL` (default
`https://master-data.trial.openg2p.org`, settable per-deployment in `.env`).

- `make seed-md` walks the geo hierarchy via `/geo/get_g2p_geo_levels` and
  `/geo/get_g2p_geo_level_values` and writes `postgres/cache/ethiopia_geo.json`.
- The seeded registry rows are pinned to these MDS-known geo codes — so the
  running backend's `/geo/levels` and `/geo/values` endpoints resolve the same
  codes a dashboard or drill-down map will request.
- If MDS is unreachable, the seeder falls back to a small built-in Ethiopia
  sample (5 regions × ~3 zones × ~2 woredas × 1-2 kebeles). Tests keep running.

The Master Data Service v2 OpenAPI does NOT expose write endpoints, so the
seeder is read-only. If your MDS instance has admin endpoints for loading
sample data, run them separately before `make seed`.

## Data shape

`make seed --households N` generates:

| Table                                | Rows (≈)            | Notes |
|--------------------------------------|---------------------|-------|
| `g2p_register_households`            | N                   | head info, geo, PMT, income |
| `g2p_register_individuals`           | ~4 N                | head + family members, demographics, vulnerability |
| `g2p_register_household_housing`     | N                   | dwelling type, materials, water, sanitation |
| `g2p_register_individual_programs`   | ~40% of individuals | PSNP / CBHI / PSSN / AGP / RDP, with overlap (~6% double-dip) |
| `g2p_register_individual_shocks`     | ~20% of individuals | DROUGHT / FLOOD / CONFLICT / ILLNESS, with CSI change |

After insert, both materialized views are refreshed.

## Smoke queries

```bash
make refresh-mv
docker exec -it insight-test-postgres psql -U postgres -d registry

-- distribution of PMT score
SELECT width_bucket(pmt_score, 0, 80, 16) AS bin, COUNT(*) FROM mv_individuals_summary GROUP BY 1 ORDER BY 1;

-- women in a region with PMT < 30
SELECT region_code, COUNT(*) FROM mv_individuals_summary
WHERE gender = 'F' AND pmt_score < 30 GROUP BY region_code;

-- double-dipping individuals
SELECT individual_id, COUNT(DISTINCT program_code) c
FROM g2p_register_individual_programs WHERE status = 'ACTIVE'
GROUP BY individual_id HAVING COUNT(DISTINCT program_code) > 1 LIMIT 10;
```

## Resetting

```bash
make down            # stop containers, keep data
make clean           # stop + delete volumes + delete MDS cache
make up && make seed
```

## Notes / caveats

- The `g2p_register_*` schema here is a **simplified standalone copy**
  modelled around the SQLAlchemy models in
  [`openg2p-registry-nsr-extensions`](https://github.com/OpenG2P/openg2p-registry-nsr-extensions).
  History tables and intake-form tables are intentionally omitted — they
  aren't needed for analytics testing. The shape of the analytical surface
  (column names, enums, geo, vulnerability flags) matches the source.
- All personal data is synthetic. Names come from a fixed pool; phones and
  Fayda tokens are random strings shaped like the real thing.
- `OPENROUTER_API_KEY` is optional. When unset, the LLM service falls back
  to a deterministic stub — handy for plumbing tests that don't need real
  natural-language understanding.
