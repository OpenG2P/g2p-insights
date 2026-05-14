"""
insight-onboard CLI — automate registry onboarding.

Usage:
    python -m insight_onboard scan   --registry-db <DSN> [--use-llm] [--out proposal.yaml]
    python -m insight_onboard apply  --registry-db <DSN> [--dry-run] proposal.yaml
    python -m insight_onboard audit  --registry-db <DSN> --proposal proposal.yaml

Read-only steps (`scan`, `audit`) never modify anything. `apply` defaults to
`--dry-run=false` but always asks for confirmation before executing DDL;
`--dry-run` prints the SQL without running it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from typing import List
from urllib.parse import urlparse

import psycopg

from .ddl import (
    audit_query,
    ddl_for_materialized_views,
    ddl_for_role_and_grants,
    ddl_for_safe_views,
    generate_password,
    operational_catalog,
)
from .heuristics import (
    Classification,
    ColumnStats,
    classify_by_name,
    classify_by_sample,
    classify_skip_table,
    combine_classifications,
)
from .inspector import column_stats, list_columns, list_foreign_keys, list_tables
from .llm_classifier import classify_via_llm
from .proposal import (
    Proposal,
    ProposalColumn,
    ProposalMaterializedView,
    ProposalTable,
    load_proposal,
    proposal_to_yaml,
    validate_for_apply,
)


_logger = logging.getLogger("insight_onboard")


# =============================================================================
# scan
# =============================================================================
async def cmd_scan(args: argparse.Namespace) -> int:
    print(f"[scan] connecting to {_safe_dsn(args.registry_db)}")
    with psycopg.connect(args.registry_db) as conn:
        tables = list_tables(conn)
        print(f"[scan] found {len(tables)} tables")

        skipped: List[dict] = []
        analytical: List[ProposalTable] = []
        ambiguous: List[ColumnStats] = []   # for optional LLM pass

        for t in tables:
            reason = classify_skip_table(t)
            if reason:
                skipped.append({"name": t, "reason": reason})
                continue
            print(f"[scan]   inspecting {t}")
            cols = list_columns(conn, t)
            prop_cols = []
            for c in cols:
                stats = column_stats(conn, t, c["name"], c["pg_type"], c["nullable"])
                cls, conf, reason_str = combine_classifications(
                    classify_by_name(c["name"]),
                    classify_by_sample(stats),
                )
                # Decide `keep` from classification: pii -> drop, others -> keep.
                if cls == "pii":
                    keep: bool | str = False
                elif cls == "unclear":
                    keep = "?"
                    ambiguous.append(stats)
                else:
                    keep = True

                prop_cols.append(ProposalColumn(
                    name=c["name"],
                    pg_type=c["pg_type"],
                    pii=cls if cls != "unclear" else "?",
                    keep=keep,
                    confidence=conf,
                    reason=reason_str,
                    distinct_count=stats.distinct_count,
                    null_rate=round(stats.null_rate, 4),
                ))

            analytical.append(ProposalTable(
                source=t,
                safe_view_name=f"v_{t}_safe" if not t.startswith("v_") else t,
                columns=prop_cols,
            ))

        # Optional LLM pass on ambiguous columns
        if args.use_llm and ambiguous:
            print(f"[scan] consulting LLM for {len(ambiguous)} ambiguous columns…")
            for s in ambiguous:
                cls, conf, reason_str = await classify_via_llm(s)
                # Patch the proposal in place
                for t in analytical:
                    if t.source != s.table:
                        continue
                    for col in t.columns:
                        if col.name != s.name:
                            continue
                        if cls != "unclear":
                            col.pii = cls
                            col.keep = (cls != "pii")
                            col.confidence = "llm"
                            col.reason = reason_str

        # Foreign-key driven materialised-view suggestion
        fks = list_foreign_keys(conn)
        mvs = _suggest_materialized_views(analytical, fks)

    pii_source = []
    for t in analytical:
        for c in t.columns:
            if c.keep is False and c.pii == "pii":
                pii_source.append({"table": t.source, "column": c.name})

    proposal = Proposal(
        metadata={
            "scanned_at": dt.datetime.utcnow().isoformat() + "Z",
            "registry_db": _safe_dsn(args.registry_db),
            "tables_total": len(tables),
            "tables_analytical": len(analytical),
            "tables_skipped": len(skipped),
            "ambiguous_columns": len(ambiguous),
            "llm_consulted": args.use_llm,
        },
        deployment={"country_code": "", "default_currency": "", "k_anonymity": 5},
        skipped=skipped,
        analytical_tables=analytical,
        materialized_views=mvs,
        pii_columns_in_source=pii_source,
    )

    out_path = args.out
    with open(out_path, "w") as f:
        f.write(proposal_to_yaml(proposal))
    print(f"[scan] wrote {out_path}")
    print(f"[scan] summary:")
    print(f"         {len(analytical)} analytical tables")
    print(f"         {len(skipped)} skipped tables")
    print(f"         {len(pii_source)} PII columns identified (will be dropped)")
    print(f"         {len(ambiguous)} columns need human review (`pii: '?'`)")
    print(f"         {len(mvs)} materialized views suggested")
    print()
    print(f"Next: review {out_path}, then run `insight-onboard apply {out_path}`")
    return 0


def _suggest_materialized_views(
    tables: List[ProposalTable], fks
) -> List[ProposalMaterializedView]:
    """Very simple heuristic: if we see a 'register_individuals' style + a
    'register_households' style with a FK between them, suggest
    mv_individuals_summary. If there are geographic columns, suggest a
    district rollup. Both are starting points — user can edit/remove."""
    individuals = next((t for t in tables if "individual" in t.source.lower()), None)
    households = next((t for t in tables if "household" in t.source.lower()), None)
    mvs: List[ProposalMaterializedView] = []

    if individuals:
        join_clause = (
            f"SELECT i.*"
            + (", h.headship_type, h.household_size, h.monthly_income AS household_monthly_income"
               if households else "")
            + f"\nFROM \"{individuals.safe_view_name}\" i"
            + (f"\nLEFT JOIN \"{households.safe_view_name}\" h ON h.id = i.household_id"
               if households else "")
        )
        geo_cols = [c.name for c in individuals.columns
                    if c.keep and any(g in c.name.lower() for g in ("region", "zone", "woreda", "kebele"))]
        mvs.append(ProposalMaterializedView(
            name="mv_individuals_summary",
            description="Individuals joined with household-level fields; refresh nightly",
            sources=[individuals.safe_view_name] + ([households.safe_view_name] if households else []),
            join=join_clause,
            indexes=[c.name for c in individuals.columns
                     if c.keep and c.name in geo_cols + ["gender", "age_bracket", "pmt_score"]],
            refresh="daily at 02:00 UTC",
        ))

        # District rollup if we have at least region + woreda
        if geo_cols and "region_code" in [c.name for c in individuals.columns]:
            mvs.append(ProposalMaterializedView(
                name="mv_district_aggregates",
                description="Pre-aggregated counts by geo × demographics — use for district-level questions",
                sources=["mv_individuals_summary"],
                join=(
                    "SELECT region_code, zone_code, woreda_code,\n"
                    "       gender, age_bracket,\n"
                    "       COUNT(*) AS total,\n"
                    "       AVG(pmt_score)::NUMERIC(6,2) AS avg_pmt,\n"
                    "       AVG(monthly_income)::NUMERIC(12,2) AS avg_income\n"
                    "FROM mv_individuals_summary\n"
                    "GROUP BY region_code, zone_code, woreda_code, gender, age_bracket"
                ),
                indexes=["region_code"],
                refresh="daily at 02:30 UTC",
            ))
    return mvs


# =============================================================================
# apply
# =============================================================================
async def cmd_apply(args: argparse.Namespace) -> int:
    p = load_proposal(args.proposal)
    errors = validate_for_apply(p)
    if errors:
        print("[apply] proposal not ready:")
        for e in errors:
            print(f"  - {e}")
        return 2

    db_name = _db_name_from_dsn(args.registry_db)
    role_name = args.role or "insight_ai"
    role_password = args.role_password or generate_password()

    sql_blocks: List[str] = []
    sql_blocks.extend(ddl_for_safe_views(p))
    sql_blocks.extend(ddl_for_materialized_views(p))
    sql_blocks.extend(ddl_for_role_and_grants(p, role_name, role_password, db_name))

    full_sql = "\n\n".join(sql_blocks)

    if args.dry_run:
        print("-- DRY RUN — SQL that would be executed:")
        print(full_sql)
        _write_catalog_and_helm(p, args, role_name)
        print("\n[apply] dry-run complete. No DB changes.")
        return 0

    print("[apply] about to execute the following SQL:")
    print(full_sql)
    print()
    confirm = input("Proceed? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("[apply] aborted by user")
        return 1

    with psycopg.connect(args.registry_db, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(full_sql)
    print("[apply] DDL executed")

    # Audit
    leaks = _run_audit(args.registry_db, role_name, p)
    if leaks:
        print(f"[apply] AUDIT FAILED — {len(leaks)} PII columns visible to {role_name}:")
        for src_table, col in leaks:
            print(f"  - {src_table}.{col}")
        return 3
    print("[apply] PII-leak audit: 0 leaks ✓")

    _write_catalog_and_helm(p, args, role_name)
    print()
    print(f"[apply] done. Save this `{role_name}` password into your K8s secret:")
    print(f"  {role_password}")
    print()
    print(f"  kubectl create secret generic g2p-insight-registry-creds \\")
    print(f"      --from-literal=insight-ai-password='{role_password}'")
    return 0


def _write_catalog_and_helm(p: Proposal, args, role_name: str) -> None:
    cat_yaml = operational_catalog(p)
    with open(args.catalog_out, "w") as f:
        f.write(cat_yaml)
    print(f"[apply] wrote catalog → {args.catalog_out}")
    # Helm overlay
    helm = (
        f"global:\n"
        f"  registryDBHost: <fill-me>\n"
        f"  registryDBName: {_db_name_from_dsn(args.registry_db)}\n"
        f"\n"
        f"insightBackend:\n"
        f"  envVars:\n"
        f"    DB_USER: {role_name}\n"
        f"  appCatalog:\n"
        f"    schemaYaml: |\n"
        + "\n".join("      " + line for line in cat_yaml.splitlines())
    )
    with open(args.helm_out, "w") as f:
        f.write(helm)
    print(f"[apply] wrote Helm values overlay → {args.helm_out}")


# =============================================================================
# audit
# =============================================================================
async def cmd_audit(args: argparse.Namespace) -> int:
    p = load_proposal(args.proposal)
    leaks = _run_audit(args.registry_db, args.role or "insight_ai", p)
    if leaks:
        print(f"AUDIT FAILED — {len(leaks)} PII columns visible to {args.role or 'insight_ai'}:")
        for src_table, col in leaks:
            print(f"  - {src_table}.{col}")
        return 3
    print("audit: 0 leaks ✓")
    return 0


def _run_audit(dsn: str, role: str, p: Proposal) -> List[tuple]:
    pii_set = {(d["table"], d["column"]) for d in p.pii_columns_in_source}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Set role via SET ROLE; runs in same session.
            cur.execute(f'SET ROLE "{role}"')
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
            visible = [(t, c) for t, c in cur.fetchall()]
            cur.execute("RESET ROLE")

    visible_set = set(visible)
    return sorted(pii_set & visible_set)


# =============================================================================
# helpers
# =============================================================================
def _safe_dsn(dsn: str) -> str:
    """Strip password from DSN for logging."""
    try:
        u = urlparse(dsn)
        if u.password:
            netloc = u.netloc.replace(f":{u.password}", ":***")
            return u._replace(netloc=netloc).geturl()
    except Exception:  # noqa: BLE001
        pass
    return dsn


def _db_name_from_dsn(dsn: str) -> str:
    try:
        u = urlparse(dsn)
        return (u.path or "/").lstrip("/") or "postgres"
    except Exception:  # noqa: BLE001
        return "postgres"


# =============================================================================
# CLI
# =============================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insight-onboard",
        description="Automate onboarding a new registry for OpenG2P Insight.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scan", help="Inspect the registry schema and write a reviewable proposal.")
    sc.add_argument("--registry-db", required=True, help="Postgres DSN (admin role).")
    sc.add_argument("--out", default="proposal.yaml")
    sc.add_argument("--use-llm", action="store_true",
                    help="Consult the LLM (INSIGHT_LLM__* env vars) for columns the heuristics couldn't classify.")

    ap = sub.add_parser("apply", help="Apply a reviewed proposal: create views, role, grants, catalog, Helm overlay.")
    ap.add_argument("proposal", help="Path to the reviewed proposal YAML.")
    ap.add_argument("--registry-db", required=True, help="Postgres DSN (admin role for DDL).")
    ap.add_argument("--catalog-out", default="catalog.yaml",
                    help="Path to write the operational catalog (consumed by the backend).")
    ap.add_argument("--helm-out", default="values-onboard.yaml",
                    help="Path to write the Helm values overlay.")
    ap.add_argument("--role", default="insight_ai")
    ap.add_argument("--role-password", default=None,
                    help="If unset, a 32-char random password is generated and printed.")
    ap.add_argument("--dry-run", action="store_true", help="Print SQL but don't execute.")

    au = sub.add_parser("audit", help="Verify no PII columns are visible to the insight role.")
    au.add_argument("--registry-db", required=True)
    au.add_argument("--proposal", required=True, help="Path to the reviewed proposal (provides PII-source list).")
    au.add_argument("--role", default="insight_ai")

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        return asyncio.run(cmd_scan(args))
    if args.cmd == "apply":
        return asyncio.run(cmd_apply(args))
    if args.cmd == "audit":
        return asyncio.run(cmd_audit(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
