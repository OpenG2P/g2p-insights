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
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import psycopg

from .ddl import (
    audit_query,
    ddl_for_materialized_views,
    ddl_for_role_and_grants,
    ddl_for_safe_views,
    ddl_for_target_schema,
    generate_password,
    operational_catalog,
)
from .extension_introspect import ExtMetadata, load_extension_metadata
from .heuristics import (
    Classification,
    ColumnStats,
    classify_by_name,
    classify_by_sample,
    classify_by_type,
    classify_skip_table,
    combine_classifications,
    load_rules,
)
from .inspector import column_stats, list_columns, list_tables
from .llm_classifier import classify_via_llm
from .proposal import (
    Proposal,
    ProposalColumn,
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
    print(f"[scan]   source schema: {args.source_schema}")
    print(f"[scan]   target schema (safe layer): {args.target_schema}")
    rules = load_rules(getattr(args, "pii_rules", None))
    if getattr(args, "pii_rules", None):
        print(f"[scan]   PII rules: {args.pii_rules}")

    # Optional: load registry-extension metadata. This narrows the
    # analytical-table set to entities the extension's models actually
    # declare, and pulls enum values from `Mapped[…Enum]` annotations so
    # the catalog tells the LLM the valid values for each enum column.
    ext_md: Optional[ExtMetadata] = None
    ext_paths: List[Path] = []
    if getattr(args, "registry_extension", None):
        ext_paths.append(Path(args.registry_extension))
    if getattr(args, "registry_core", None):
        ext_paths.append(Path(args.registry_core))
    if ext_paths:
        print(f"[scan]   extension paths: {[str(p) for p in ext_paths]}")
        ext_md = load_extension_metadata(ext_paths)
        print(
            f"[scan]   extension: {len(ext_md.models)} models, "
            f"{len(ext_md.analytical_entity_tables)} analytical entities, "
            f"{len(ext_md.enum_catalog)} enums"
        )

    with psycopg.connect(args.registry_db) as conn:
        tables = list_tables(conn, schema=args.source_schema)
        print(f"[scan] found {len(tables)} tables in {args.source_schema}")

        skipped: List[dict] = []
        analytical: List[ProposalTable] = []
        ambiguous: List[ColumnStats] = []   # for optional LLM pass

        for t in tables:
            reason = classify_skip_table(t, rules=rules)
            if reason:
                skipped.append({"name": t, "reason": reason})
                continue

            # Extension-driven skip: when an extension is loaded, ANY
            # table modelled by a class that isn't a `G2PRegister`-extending
            # entity is treated as workflow/operational and skipped — even
            # if heuristics would otherwise have kept it. This is what
            # catches `g2p_register_verifications`, `g2p_register_scores`
            # (score events, not entities), the change-request family,
            # queues, etc.
            if ext_md is not None:
                model = ext_md.model_for_table(t)
                if model is not None and not model.is_analytical_entity:
                    skipped.append({
                        "name": t,
                        "reason": (
                            f"extension declares {model.class_name} but it does "
                            f"not extend G2PRegister — workflow/operational, "
                            f"not an analytical entity"
                        ),
                    })
                    continue

            print(f"[scan]   inspecting {t}")
            cols = list_columns(conn, t, schema=args.source_schema)
            prop_cols = []
            for c in cols:
                stats = column_stats(
                    conn, t, c["name"], c["pg_type"], c["nullable"],
                    schema=args.source_schema,
                )
                type_verdict = classify_by_type(c["pg_type"])
                if type_verdict is not None:
                    cls, conf, reason_str = type_verdict
                else:
                    cls, conf, reason_str = combine_classifications(
                        classify_by_name(c["name"], rules=rules),
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

                enum_values = None
                mixin = None
                if ext_md is not None:
                    ext_model = ext_md.model_for_table(t)
                    if ext_model is not None:
                        ext_col = ext_model.columns.get(c["name"])
                        if ext_col is not None:
                            enum_values = ext_col.enum_values
                            mixin = ext_col.declaring_class

                prop_cols.append(ProposalColumn(
                    name=c["name"],
                    pg_type=c["pg_type"],
                    pii=cls if cls != "unclear" else "?",
                    keep=keep,
                    confidence=conf,
                    reason=reason_str,
                    distinct_count=stats.distinct_count,
                    null_rate=round(stats.null_rate, 4),
                    enum_values=enum_values,
                    mixin=mixin,
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

    # Aggregate materialized views are TODO — see the declarative grammar
    # block at the end of the generated proposal YAML. Operators who need
    # an MV today can hand-add one to the proposal before `apply`.
    mvs: List = []

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
        source_schema=args.source_schema,
        target_schema=args.target_schema,
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
    sql_blocks.extend(ddl_for_target_schema(p))
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
    """Confirm the AI role cannot see any column listed in
    `pii_columns_in_source`. We scan BOTH the source schema (in case the
    role has any unexpected grants left over) AND the target schema (in
    case a safe-view definition accidentally re-projected a PII column)."""
    pii_set = {(d["table"], d["column"]) for d in p.pii_columns_in_source}
    schemas = sorted({p.source_schema, p.target_schema})
    schema_in = ", ".join(f"'{s}'" for s in schemas)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SET ROLE "{role}"')
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                f"WHERE table_schema IN ({schema_in})"
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
    sc.add_argument("--source-schema", default="public",
                    help="Schema where the registry tables live. Default: public.")
    sc.add_argument("--target-schema", default="insight_safe",
                    help="Schema where the safe layer (v_*_safe views) will be created. Default: insight_safe.")
    sc.add_argument("--pii-rules", default=None,
                    help="Path to a YAML file overriding the default PII/quasi/safe keyword rules. "
                         "Sections omitted from the override fall back to the bundled defaults.")
    sc.add_argument("--registry-extension", default=None,
                    help="Path to the registry extension's `register_domain/models/` directory "
                         "(e.g. .../nsr-extension/src/openg2p_registry_nsr_extension/register_domain/models). "
                         "When provided, only tables modelled by classes that inherit `G2PRegister` are "
                         "treated as analytical entities, and enum values are extracted from Mapped[...Enum] "
                         "annotations and written into the catalog.")
    sc.add_argument("--registry-core", default=None,
                    help="Optional path to the registry-core `models/` directory "
                         "(e.g. .../openg2p-registry-core/src/openg2p_registry_core/models). "
                         "Provides the base mixins (G2PRegister, G2PPerson, G2PGeo) and their enums "
                         "for full column-to-mixin attribution.")

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
