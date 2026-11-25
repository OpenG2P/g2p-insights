# Master Data Service — required changes for hierarchical drill-down

**Status:** proposal · **Owner:** master-data-service (separate branch)
**Consumer:** G2P Insight drill engine (`services/drill_service.py`)
**Repo:** [openg2p/master-data-service](https://github.com/OpenG2P/master-data-service)

G2P Insight's drill engine works against MDS as it stands **for non-map
drill-downs** (bar, table, funnel). The changes below are required before the
**map** renderer can be trusted, and before drill-down can be relied on across
countries.

Nothing here assumes a country, a level name, or a hierarchy depth.

---

## 1. Current state

MDS today exposes two tables via `/geo/get_g2p_geo_levels` and
`/geo/get_g2p_geo_level_values`:

**`g2p_geo_levels`** — the hierarchy definition
| column | note |
|---|---|
| `level_id` | e.g. `l0…l4` |
| `level_mnemonic` | e.g. `country, region, district, ward, village` |
| `parent_level_id` | self-referencing chain |

**`g2p_geo_level_values`** — the nodes
| column | note |
|---|---|
| `level_value_id` | **path-style**, e.g. `kamuntu/kilima/mkutani/kwetu/amani` |
| `level_id` | which level this belongs to |
| `level_value_mnemonic` | short name, e.g. `amani` |
| `parent_level_value_id` | parent path |

### What is already right

- **Levels are data, not code** — depth is variable by construction.
- **No hardcoded level names** — each country supplies its own mnemonics.
- **Proper adjacency lists** at both tiers, so the chain is generically walkable.

These properties are why the drill engine could be written country-agnostically
at all. The gaps below are additive; none of them require reshaping the above.

---

## 2. Gap 1 — no P-code (highest priority)

`level_value_id` is a **path built from names**. Official boundary datasets
(national statistics agencies, OCHA/HDX COD-AB) are keyed on **administrative
P-codes**, not names.

Without a pcode, joining registry data to boundary polygons falls back to
**name matching**. Measured evidence from an Ethiopia spike:

- exact normalised name match: **76%**
- with `_town` suffix handling + fuzzy (edit distance ≤ 2): **91%**
- per-region spread: **56%–94%**; ~30 woredas never matched
- same woreda names recur in different regions, so name matching also produced
  **geographically wrong** assignments until constrained by a point-in-polygon
  pass

That failure rate is unacceptable for reporting that informs payments.

### Proposed

Add to `g2p_geo_level_values`:

| column | type | note |
|---|---|---|
| `pcode` | `varchar` | Official administrative code. Unique within `(level_id, version)`. |
| `pcode_source` | `varchar` | e.g. `national-statistics`, `ocha-cod-ab` — provenance matters when codes disagree. |

Return `pcode` from `/geo/get_g2p_geo_level_values`.

> **Note.** `level_value_id` should stay as-is — it is a good stable internal
> key. `pcode` is the *join key to the outside world*, not a replacement.

Until this lands, the drill engine joins on the leaf token of the path
(`_leaf_of()` in `drill_service.py`) — explicitly marked as the weak point.

---

## 3. Gap 2 — no geometry reference

Nothing links a node to its boundary polygon, so a choropleth has nothing to
draw.

### Proposed

Either (a) a reference column, or (b) a convention. (a) is preferred because it
survives storage changes:

| column | type | note |
|---|---|---|
| `boundary_uri` | `varchar` | Pointer to the GeoJSON for this node's geometry, e.g. an object-store URL. |
| `boundary_simplified_uri` | `varchar` | Optional web-optimised geometry. Full-resolution admin boundaries are far too large to ship to a browser (a simplified national ADM3 was still ~2.6 MB in testing). |

**Storage:** MinIO already runs in the cluster; `country/level/version/…​.geojson`
is a natural layout. MDS stores the pointer, not the polygon.

**Requirement:** every GeoJSON *feature* must carry the **same `pcode`** as a
property. That is the join key — the map layer must never match on names.

**PostGIS is not required** for drill-down (pcode joins suffice). It is only
needed for genuine spatial operations, e.g. point-in-polygon to assign a
household to an area.

---

## 4. Gap 3 — no versioning / effective dating

Administrative structures change. A real example: Ethiopia's SNNPR region was
split into four successor regions, so a 2016 boundary set has 11 regions where
current data has 14. Today MDS would silently serve one structure and reports
would be drawn on the wrong geography with no warning.

### Proposed

| column | type | note |
|---|---|---|
| `version` | `varchar` | Boundary/structure vintage, e.g. `2023.1`. |
| `valid_from` | `date` | |
| `valid_to` | `date` NULL | NULL = current. |

Applies to **both** `g2p_geo_levels` and `g2p_geo_level_values` — a split can
add levels, not just values.

API: accept an optional `as_of` (date) or `version`; default to current.
Responses should state which version was served so a report can cite it.

---

## 5. Gap 4 — no localised display labels

`level_value_mnemonic` is a slug (`addis_ababa`), not a display name. OpenG2P
deployments need Amharic, French, Arabic, etc.

### Proposed

| column | type | note |
|---|---|---|
| `display_name` | `varchar` | Properly cased default label. |
| `display_name_i18n` | `jsonb` | `{"am": "…", "fr": "…"}` |

Same for `g2p_geo_levels` (`level_mnemonic` → a translatable level label, so a
breadcrumb can read "Región" not "region").

Today Insight falls back to showing the raw DB value.

---

## 6. Deployment model — RESOLVED: one country per deployment

**Decided:** a deployment serves exactly **one country**.

Consequences:

- **No `country_code` column is needed.** `g2p_geo_levels` stays global to the
  deployment, and there is no risk of Country A's `l1` = "region" colliding with
  Country B's `l1` = "state" — they are separate instances.
- Level mnemonics and depth remain **per-deployment data**, so onboarding a new
  country is still a new deployment with different rows, never a code change.
- Multi-country roll-ups (if ever needed) are an aggregation across
  deployments, not a schema concern here.

> **UI consequence worth knowing.** With one country per deployment the root
> level (`country`) contains exactly one node, so starting a drill there shows
> a single row. The drill engine lets the caller choose the starting level
> (`level_id`), so the UI should normally **start one level below the root**
> (e.g. `region`) and use the root only for the breadcrumb. The engine does not
> hardcode this — it remains depth-agnostic.

---

## 7. Suggested sequencing

| # | Change | Unblocks | Effort |
|---|---|---|---|
| 1 | `pcode` + `pcode_source` | Reliable boundary joins — **the map renderer** | S |
| 2 | `boundary_uri` (+ simplified) | Choropleth rendering | S |
| 3 | `display_name` / i18n | Presentable, localised UI | S |
| 4 | `version` / `valid_from` / `valid_to` | Historically correct reporting | M |

(No `country_code` scoping — one country per deployment, see §6.)

Items 1–3 are additive and backward-compatible: existing consumers keep working
because nothing is removed or renamed.

---

## 8. What Insight does in the meantime

- **Non-map drill (bar/table/funnel): works today.** Levels, depth and labels
  all come from MDS as-is.
- **Map drill: blocked on items 1–2.** The engine is ready; the renderer needs
  a trustworthy geometry join.
- The engine already refuses to widen results when an ancestor level cannot be
  filtered, rather than silently returning a broader set.
