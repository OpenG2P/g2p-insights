#!/usr/bin/env python3
"""Generate the AI catalog from the databases this deployment actually has.

WHY THIS EXISTS
---------------
The Ask tab plans SQL from a catalog: table names, column types, enum values,
which table is the entity register. That catalog used to be a hand-written
schema.yaml shipped in the chart, and the failure mode was total and silent —
the published default contained ZERO tables, and the only example in the repo
described `mv_individuals_summary` and `mv_district_aggregates`, names that
exist in no registry. With an empty catalog `safe_table_names()` returns nothing
and the planner refuses every question, so the feature looked switched off
rather than misconfigured.

Hand-maintaining it could not have worked anyway: the tables differ per registry
(fr_rpt_farmer vs nsr_rpt_household), and the enum values differ per COUNTRY,
because they come from the country pack.

So generate it from the two places that already know:

  * the registry database — the *_rpt_* reporting views, which are exactly the
    curated, denormalised, aggregate-shaped datasets the dashboards and maps
    already read. Pointing the AI at the same surface means it can answer
    questions about what the charts show, by construction.

  * Master Data — the country's code lists, so `gender` is described with the
    values this deployment actually uses rather than a guess.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not expose the register tables themselves. Those hold names, phone
numbers and identifiers; the reporting views are aggregates built for analytics
and are the right blast radius for a system that writes its own SQL.

`safe_views` is a fail-secure allow-list — the planner will only touch what is
named there. Anything this script cannot classify is left out rather than
guessed into the list.

USAGE
    python generate_catalog.py --out /catalog/schema.yaml

Connection settings come from the environment (PGHOST/PGUSER/... for the
registry, MDS_* for Master Data), matching the maps build.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from typing import Dict, List, Optional

import asyncpg
import yaml

# Person-identifying columns, matched on name. Deliberately NARROW: a first
# attempt used `phone` and `(^|_)names?$`, which withheld `has_personal_phone`
# (a boolean flag — a yes/no cannot identify anyone), `cooperative_names` (an
# organisation, not a person) and `level_name` (the word "region"). Over-broad
# classification is not the safe direction it looks like: it silently removes
# the columns an analyst asks about, and the planner then answers a narrower
# question than the one put to it, with nothing to show that anything was
# dropped.
PII_PATTERNS = [
    r"^name$", r"first_name", r"last_name", r"middle_name", r"given_name",
    r"family_name", r"surname", r"person_name",
    r"phone_number", r"mobile_number", r"msisdn",
    r"email", r"national_id", r"passport", r"account_number",
    r"birth_date", r"^dob$", r"photo", r"image",
    r"street", r"house_number",
]

# Types that cannot carry an identity whatever the column is called. A boolean
# is a yes/no and an aggregate count is a number; neither identifies anybody.
NON_PII_TYPES = ("boolean", "smallint", "integer", "bigint", "numeric",
                 "double precision", "real")

# Identifiers: not PII on their own, but useless to an analyst and noisy in the
# prompt. Kept in the catalog as `quasi` so joins remain possible.
QUASI_PATTERNS = [r"(^|_)id$", r"(^|_)uuid$", r"record_id", r"pcode"]


def classify(column: str, data_type: str = "") -> str:
    low = column.lower()
    typ = (data_type or "").lower()
    # Type first: it is the stronger signal, and it is what keeps a flag like
    # has_personal_phone available to be counted.
    if not any(typ.startswith(t) for t in NON_PII_TYPES):
        for p in PII_PATTERNS:
            if re.search(p, low):
                return "pii"
    for p in QUASI_PATTERNS:
        if re.search(p, low):
            return "quasi"
    return "safe"


async def reporting_views(conn) -> Dict[str, List[dict]]:
    """Every *_rpt_* view and materialized view, with its columns.

    Materialized views are queried through pg_class rather than
    information_schema — information_schema.views and .columns both omit them,
    which is a mistake that costs an afternoon each time it is made.
    """
    rows = await conn.fetch(
        """
        select c.relname as table_name,
               c.relkind as kind,
               a.attname as column_name,
               format_type(a.atttypid, a.atttypmod) as data_type,
               a.attnum
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        join pg_attribute a on a.attrelid = c.oid
        where n.nspname = 'public'
          and c.relkind in ('v', 'm')
          and c.relname like '%\\_rpt\\_%'
          and a.attnum > 0
          and not a.attisdropped
        order by c.relname, a.attnum
        """
    )
    out: Dict[str, List[dict]] = {}
    for r in rows:
        out.setdefault(r["table_name"], []).append(
            {"name": r["column_name"], "type": r["data_type"], "kind": r["kind"]}
        )
    return out


async def row_counts(conn, names: List[str]) -> Dict[str, int]:
    """Planner estimates, not count(*) — this runs against production-sized
    views and an exact count would read every row of each."""
    out: Dict[str, int] = {}
    for n in names:
        try:
            v = await conn.fetchval(
                "select reltuples::bigint from pg_class where relname = $1", n
            )
            out[n] = max(int(v or 0), 0)
        except Exception:  # noqa: BLE001
            out[n] = 0
    return out


def pick_register(views: Dict[str, List[dict]]) -> Dict[str, Optional[str]]:
    """Decide which view is the entity register, and what the rest hang off.

    The rule is structural rather than a name list, so it works for a registry
    nobody has written code for yet: whichever `<x>_id` appears in the most
    OTHER views is the entity everything else is describing. In a Farmer
    Registry `farmer_id` appears in land and crop, so `fr_rpt_farmer` is the
    register; in a social registry `household_id` wins.

    This matters more than it looks. Without it the planner answers "how many
    farmers?" by counting rows in whichever view it happened to pick — crops,
    say — and returns a number that is confidently wrong.
    """
    id_owner = {}
    for name, cols in views.items():
        suffix = name.rsplit("_rpt_", 1)[-1]
        if any(c["name"] == f"{suffix}_id" for c in cols):
            id_owner[f"{suffix}_id"] = name

    references = {name: 0 for name in views}
    for idcol, owner in id_owner.items():
        for name, cols in views.items():
            if name != owner and any(c["name"] == idcol for c in cols):
                references[owner] += 1

    register = max(references, key=lambda n: (references[n], -len(n)), default=None)
    if register is not None and references.get(register, 0) == 0:
        # Nothing references anything — a single-view deployment. It is still
        # the register; there is nothing else it could be.
        register = max(views, key=lambda n: len(views[n]), default=None)

    roles: Dict[str, Optional[str]] = {}
    for name in views:
        roles[name] = register if name != register else None
    return {"register": register, "parents": roles}


async def master_data_enums(dsn: dict) -> Dict[str, List[str]]:
    """Code-list values per attribute, keyed by attribute id.

    Column `gender` is matched to attribute `GENDER` — the same upper-case
    convention the registry's own value validator uses, so a column and its
    code list stay associated without a mapping table to maintain.
    """
    try:
        conn = await asyncpg.connect(**dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"[catalog] Master Data unreachable ({exc}); "
              "columns will carry no enum values", file=sys.stderr)
        return {}
    try:
        rows = await conn.fetch(
            "select attribute_id, value_id from g2p_attribute_values order by 1, sort_order"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[catalog] no code lists in Master Data ({exc})", file=sys.stderr)
        return {}
    finally:
        await conn.close()
    out: Dict[str, List[str]] = {}
    for r in rows:
        out.setdefault(r["attribute_id"], []).append(r["value_id"])
    return out


async def geo_levels(dsn: dict) -> List[dict]:
    """geo_N_id -> the level it names, so the planner knows geo_2 is a region.

    Ordered by walking parent -> child rather than by level_id: the ids are
    opaque and their sort order is not the hierarchy.
    """
    try:
        conn = await asyncpg.connect(**dsn)
        rows = await conn.fetch(
            "select level_id, level_mnemonic, parent_level_id from g2p_geo_levels")
        await conn.close()
    except Exception:  # noqa: BLE001
        return []
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent_level_id"], []).append(
            (r["level_id"], r["level_mnemonic"]))
    order, cursor = [], None
    while by_parent.get(cursor):
        lid, mnem = by_parent[cursor][0]
        order.append(mnem)
        cursor = lid
    return [{"column": f"geo_{i + 1}", "level_mnemonic": m}
            for i, m in enumerate(order)]


async def country_code(dsn: dict) -> str:
    try:
        conn = await asyncpg.connect(**dsn)
        v = await conn.fetchval(
            "select level_value_id from g2p_geo_level_values "
            "where parent_level_value_id is null limit 1")
        await conn.close()
        return v or ""
    except Exception:  # noqa: BLE001
        return ""


def describe_geo(column: str, levels: List[dict]) -> Optional[str]:
    """What geo_2 actually MEANS in this country, in words the planner can use.

    geo_1..geo_N are positional: geo_1 is the country, geo_2 the first level
    below it, and so on. The names differ per country — Ethiopia's geo_2 is a
    region, Kamuntu's is a province — so the description has to come from the
    deployment's own hierarchy rather than be written into the code.

    Without it the planner sees five identically-shaped columns called geo_1..5
    and picks by position. Asked for farmers "in each region" it grouped by
    geo_1, the country, and returned one row: a plausible query, a correct
    execution, an answer to a question nobody asked.
    """
    base = column[:-3] if column.endswith("_id") else column
    names = {lvl["column"]: lvl["level_mnemonic"] for lvl in levels}
    mnem = names.get(base)
    if not mnem:
        return None
    n = int(base.split("_")[1])
    level = mnem.replace("_", " ").lower()
    if column.endswith("_id"):
        return (f"P-code of the {level}. A join key — group and display by "
                f"{base} instead.")
    if n == 1:
        # Saying "top of the hierarchy" is not enough: the model has to know that
        # grouping by it collapses the whole deployment into one row, which is
        # what made an answer to "by region" look like a working query.
        return (f"Name of the {level} — the top of the hierarchy. Every row "
                f"carries the same value, so grouping by it yields a single "
                f"row. For a breakdown use geo_2 or below.")
    return (f"Name of the {level} — administrative level {n}. Group by this "
            f"for \"by {level}\" questions.")


def describe(name: str, register: Optional[str], rows: int) -> str:
    entity = name.rsplit("_rpt_", 1)[-1].replace("_", " ")
    if name == register:
        return (f"One row per {entity}. This is the entity register — count rows "
                f"here for \"how many {entity}s\" questions. ~{rows:,} rows.")
    parent = (register or "").rsplit("_rpt_", 1)[-1].replace("_", " ")
    return (f"One row per {entity}. A supporting table: use it for questions "
            f"about {entity}, not to count {parent or 'entities'}. ~{rows:,} rows.")


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, help="where to write schema.yaml")
    p.add_argument("--k-anonymity", type=int, default=5)
    args = p.parse_args()

    reg_dsn = {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", 5432)),
        "database": os.environ["PGDATABASE"],
        "user": os.environ["PGUSER"],
        "password": os.environ.get("PGPASSWORD"),
    }
    mds_dsn = {
        "host": os.environ.get("MDS_PGHOST", reg_dsn["host"]),
        "port": int(os.environ.get("MDS_PGPORT", 5432)),
        "database": os.environ.get("MDS_DB", "master_data"),
        "user": os.environ.get("MDS_PGUSER", reg_dsn["user"]),
        "password": os.environ.get("MDS_PGPASSWORD", reg_dsn["password"]),
    }

    conn = await asyncpg.connect(**reg_dsn)
    try:
        views = await reporting_views(conn)
        if not views:
            print("[catalog] no *_rpt_* reporting views found — nothing to "
                  "describe. Has the registry's reporting-views job run?",
                  file=sys.stderr)
            return 1
        counts = await row_counts(conn, list(views))
    finally:
        await conn.close()

    enums = await master_data_enums(mds_dsn)
    picked = pick_register(views)
    register = picked["register"]

    tables, safe_views = [], []
    # Resolved once: the same hierarchy names the geo columns and fills the
    # catalog's geo_columns block.
    geo = await geo_levels(mds_dsn)

    for name, cols in sorted(views.items()):
        spec_cols, dropped = [], []
        for c in cols:
            pii = classify(c["name"], c["type"])
            if pii == "pii":
                dropped.append(c["name"])
                continue
            col = {"name": c["name"], "type": c["type"], "pii": pii}
            geo_desc = describe_geo(c["name"], geo)
            if geo_desc:
                col["description"] = geo_desc
            vals = enums.get(c["name"].upper())
            if vals:
                col["enum"] = vals
            spec_cols.append(col)
        if dropped:
            print(f"[catalog] {name}: withheld {len(dropped)} column(s) as PII: "
                  f"{', '.join(dropped)}", file=sys.stderr)
        tables.append({
            "name": name,
            "schema": "public",
            "role": "register" if name == register else "table",
            **({"parent_register": register} if name != register and register else {}),
            "description": describe(name, register, counts.get(name, 0)),
            "row_count_estimate": counts.get(name, 0),
            "refresh": "materialized view, rebuilt by the registry and refreshed "
                       "by the maps build",
            "columns": spec_cols,
        })
        safe_views.append(name)

    catalog = {
        "deployment": {
            "country_code": await country_code(mds_dsn),
            "k_anonymity": args.k_anonymity,
        },
        "tables": tables,
        # Fail-secure: the planner touches ONLY what is named here.
        "safe_views": safe_views,
        "geo_columns": geo,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        yaml.safe_dump(catalog, fh, sort_keys=False, width=100)

    print(f"[catalog] {len(tables)} table(s), register={register}, "
          f"{sum(len(t['columns']) for t in tables)} columns, "
          f"{len(catalog['geo_columns'])} geo level(s) -> {args.out}")
    for t in tables:
        print(f"[catalog]   {t['role']:<8} {t['name']:<24} "
              f"{len(t['columns']):>3} cols  ~{t['row_count_estimate']:,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
