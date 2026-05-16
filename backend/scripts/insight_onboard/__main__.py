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
    classify_by_mixin,
    classify_by_name,
    classify_by_sample,
    classify_by_type,
    classify_skip_table,
    combine_classifications,
    describe_table,
    load_rules,
    suggest_parent_register,
    suggest_table_role,
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
    validate_for_apply_warnings,
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
        db_tables = list_tables(conn, schema=args.source_schema)
        print(f"[scan] found {len(db_tables)} tables in {args.source_schema}")

        # Build the to-scan list. Two modes:
        #   * Extension-led: iterate ONLY the analytical entities the
        #     extension declares (intersected with DB existence). Everything
        #     else in the DB is skipped without column inspection — no
        #     COUNT(DISTINCT) chatter, no shape sampling. Drops scan time
        #     from minutes to seconds on real registries.
        #   * Heuristic-led (no extension): iterate every DB table, apply
        #     skip-pattern regexes, fall back to name+sample classification.
        skipped: List[dict] = []
        if ext_md is not None:
            to_scan = _extension_led_planner(db_tables, ext_md, skipped)
            print(f"[scan] extension-led: scanning {len(to_scan)} entity tables")
        else:
            to_scan = _heuristic_led_planner(db_tables, rules, skipped)
            print(f"[scan] heuristic-led: scanning {len(to_scan)} non-skipped tables")

        analytical: List[ProposalTable] = []
        ambiguous: List[ColumnStats] = []

        for t in to_scan:
            print(f"[scan]   inspecting {t}")
            cols = list_columns(conn, t, schema=args.source_schema)
            ext_model = ext_md.model_for_table(t) if ext_md is not None else None

            prop_cols: List[ProposalColumn] = []
            for c in cols:
                # Extension column metadata (when available) — enum values
                # and mixin attribution for free, no DB query needed.
                ext_col = ext_model.columns.get(c["name"]) if ext_model else None

                cls, conf, reason_str, stats = _classify_column(
                    conn=conn,
                    schema=args.source_schema,
                    table=t,
                    col=c,
                    rules=rules,
                    ext_col=ext_col,
                )

                # Decide `keep`: pii → drop; unclear → operator review.
                if cls == "pii":
                    keep: bool | str = False
                elif cls == "unclear":
                    keep = "?"
                    if stats is not None:
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
                    distinct_count=stats.distinct_count if stats else 0,
                    null_rate=round(stats.null_rate, 4) if stats else 0.0,
                    enum_values=ext_col.enum_values if ext_col else None,
                    mixin=ext_col.declaring_class if ext_col else None,
                ))

            analytical.append(ProposalTable(
                source=t,
                safe_view_name=f"v_{t}_safe" if not t.startswith("v_") else t,
                columns=prop_cols,
            ))

        # Second pass: assign role / parent_register / description per table.
        # Done after the column scan so we know the full set of registers
        # (a satellite scanned before its parent would otherwise have
        # parent=None). Three info sources, in priority order:
        #   1. Extension (when present): `suggested_role` directly attaches
        #      `register` / `programme_register` / `table`, with the entity
        #      filter already applied.
        #   2. Name heuristic: single-segment after `g2p_register_` →
        #      register; multi-segment → table. (Cannot detect
        #      programme_register from name alone.)
        # Operator confirms / overrides during proposal review.
        for tbl in analytical:
            ext_model = ext_md.model_for_table(tbl.source) if ext_md is not None else None
            if ext_model is not None and ext_model.suggested_role:
                tbl.role = ext_model.suggested_role
            else:
                tbl.role = suggest_table_role(tbl.source)

        register_sources = [tbl.source for tbl in analytical if tbl.role == "register"]
        for tbl in analytical:
            if tbl.role == "table":
                parent_source = suggest_parent_register(tbl.source, register_sources)
                if parent_source is not None:
                    # Catalog references views, not source tables. Map source
                    # → safe_view_name via the analytical set.
                    parent_view = next(
                        (p.safe_view_name for p in analytical if p.source == parent_source),
                        None,
                    )
                    tbl.parent_register = parent_view
            # description uses safe-view names everywhere — that's what
            # the LLM actually sees in its prompt.
            tbl.description = describe_table(tbl.source, tbl.role, tbl.parent_register)

        # Optional LLM pass on ambiguous columns. Only fires for columns
        # whose classifier actually sampled (those have a ColumnStats);
        # extension-led mode usually has zero or one of these.
        if args.use_llm and ambiguous:
            print(f"[scan] consulting LLM for {len(ambiguous)} ambiguous columns…")
            for s in ambiguous:
                cls, conf, reason_str = await classify_via_llm(s)
                for t_obj in analytical:
                    if t_obj.source != s.table:
                        continue
                    for col in t_obj.columns:
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
            "tables_total": len(db_tables),
            "tables_analytical": len(analytical),
            "tables_skipped": len(skipped),
            "ambiguous_columns": len(ambiguous),
            "llm_consulted": args.use_llm,
            "mode": "extension-led" if ext_md is not None else "heuristic-led",
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


# -----------------------------------------------------------------------------
# scan helpers — planners and per-column classifier
# -----------------------------------------------------------------------------
def _extension_led_planner(
    db_tables: List[str],
    ext_md: ExtMetadata,
    skipped: List[dict],
) -> List[str]:
    """When an extension is loaded it's the authoritative source for which
    tables matter. Iterate ONLY the extension's analytical entities — any DB
    table not declared as such is skipped without column inspection.

    Two skip reasons get recorded:
      * extension entity declared but missing from DB (likely a pending
        migration; operator should know)
      * DB table not declared by extension (workflow/operational/custom)
    """
    entity_set = ext_md.analytical_entity_tables
    db_set = set(db_tables)

    to_scan = sorted(entity_set & db_set)

    for missing in sorted(entity_set - db_set):
        skipped.append({
            "name": missing,
            "reason": "declared by extension but absent in DB — migrations behind?",
        })
    for not_entity in sorted(db_set - entity_set):
        model = ext_md.model_for_table(not_entity)
        if model is not None:
            reason = (
                f"extension declares {model.class_name} but it does not extend "
                f"G2PRegister — workflow/operational, not an analytical entity"
            )
        else:
            reason = (
                "not declared as an analytical entity by the extension "
                "(custom or out-of-scope table)"
            )
        skipped.append({"name": not_entity, "reason": reason})

    return to_scan


def _heuristic_led_planner(
    db_tables: List[str],
    rules,
    skipped: List[dict],
) -> List[str]:
    """No extension available — fall back to name-pattern skip rules and
    let the per-column classifier do the heavy lifting via sampling."""
    to_scan: List[str] = []
    for t in db_tables:
        reason = classify_skip_table(t, rules=rules)
        if reason:
            skipped.append({"name": t, "reason": reason})
        else:
            to_scan.append(t)
    return to_scan


def _classify_column(
    conn,
    schema: str,
    table: str,
    col: dict,
    rules,
    ext_col,
) -> tuple:
    """Classify a single column, returning (cls, conf, reason, stats_or_None).

    Layers, fastest-to-slowest, and we stop as soon as any layer returns a
    high-confidence verdict:

      1. **Type** — JSON/JSONB/bytea/xml → pii high.
      2. **Name** — keyword / multi-token match → high or medium.
      3. **Extension enum** — if `ext_col` has enum_values, the column is
         an analytical enum → safe high.
      4. **Mixin prior** — G2PPerson/G2PGeo/G2PRegister extension mixins
         give a medium-confidence prior for unclear columns.
      5. **Sample shape** — last-resort DB query (COUNT DISTINCT + 20
         distinct values). Only runs when 1-4 didn't decide.

    Returns the ColumnStats from layer 5 when it ran, otherwise None.
    Callers only push None-stats columns to the ambiguous list if their
    final classification is still `unclear`."""
    pg_type = col["pg_type"]
    col_name = col["name"]

    # 1. Type-driven exclusion (json/jsonb/bytea/xml).
    t_verdict = classify_by_type(pg_type)
    if t_verdict is not None:
        cls, conf, reason = t_verdict
        return cls, conf, reason, None

    # 2. Name-driven.
    n_cls, n_conf, n_reason = classify_by_name(col_name, rules=rules)
    if n_conf == "high":
        return n_cls, n_conf, n_reason, None

    # 3. Extension enum: presence of enum_values means the column is a
    #    categorical analytical field.
    if ext_col is not None and ext_col.enum_values:
        return (
            "safe",
            "high",
            f"extension enum: {ext_col.enum_class_name} = {ext_col.enum_values}",
            None,
        )

    # 4. Mixin prior from the extension. Wins over a low-confidence name
    #    signal but loses to a high-confidence one (handled above).
    if ext_col is not None:
        m_verdict = classify_by_mixin(ext_col.declaring_class)
        if m_verdict is not None:
            m_cls, m_conf, m_reason = m_verdict
            # If the name said something at medium-or-above confidence
            # AND it disagrees with the mixin, prefer the *more restrictive*
            # — bias toward false positive PII.
            if n_conf in ("medium", "high"):
                order = {"pii": 3, "quasi": 2, "safe": 1, "unclear": 0, "skip": -1}
                if order[n_cls] >= order[m_cls]:
                    return n_cls, n_conf, n_reason, None
            return m_cls, m_conf, m_reason, None

    # 5. Last resort: sample DB rows (slow on un-analyzed tables).
    stats = column_stats(conn, table, col_name, pg_type, col["nullable"], schema=schema)
    cls, conf, reason = combine_classifications(
        (n_cls, n_conf, n_reason),
        classify_by_sample(stats),
    )
    return cls, conf, reason, stats


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

    # Non-fatal warnings — print but don't block. Operator gets a chance to
    # ctrl-C before the confirmation prompt below.
    for w in validate_for_apply_warnings(p):
        print(f"[apply] WARNING: {w}")

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
