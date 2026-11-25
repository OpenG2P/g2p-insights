# Parked — custom hierarchical drill-down

This is the **country-agnostic hierarchical drill-down engine** (backend) and the
Expert Lab **Plotly drill renderer** (frontend), built earlier in the project.

**Why parked:** the platform now uses **Evidence.dev** (embedded as the *Maps* tab)
for hierarchical / zoom-drill map views, so this custom implementation is no
longer wired into the app. It is kept here — not deleted — because the engine is
sound (it walks Master Data Service levels with no hardcoded country/depth, joins
on pcode, enforces the same validator/k-anonymity/cost-gate pipeline) and may be
revived if a future need arises for native (non-Evidence) drill inside Insight.

## Contents
- `backend/drill_service.py`      — the drill engine (hierarchy walk + safe SQL)
- `backend/drill_controller.py`   — `GET /drill/hierarchy`, `POST /drill`
- `backend/drill.py`              — Pydantic schemas (belongs in `schemas/`)
- `backend/test_drill_hierarchy.py` — country-agnostic + safety regression tests
- `frontend/DrillDown.tsx`        — Plotly bar renderer with breadcrumb click-drill

## To revive
1. Move the backend files back to `services/`, `controllers/`, `schemas/`, `tests/`.
2. Re-add `DrillService()` + `DrillController().post_init()` to `app.py`.
3. Move `DrillDown.tsx` to `frontend/src/components/` and mount it in a page.
The drill API client helpers are still present in `frontend/src/api/client.ts`.

Known-good as of this parking: `/drill/hierarchy` and `POST /drill` (incl.
second-level drill) worked end-to-end against the test stack.
