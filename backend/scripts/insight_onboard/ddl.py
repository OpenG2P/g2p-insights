"""Generate SQL DDL and the operational catalog/Helm values from a reviewed
proposal."""

from __future__ import annotations

import secrets
import string
from typing import List

import yaml

from .proposal import Proposal, ProposalTable, ProposalMaterializedView


# -----------------------------------------------------------------------------
# DDL generation
# -----------------------------------------------------------------------------
def generate_password(n: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def ddl_for_target_schema(p: Proposal) -> List[str]:
    """Emit the CREATE SCHEMA block for the dedicated safe-layer schema.

    Kept separate from the view DDL so callers can sequence it before
    everything that references the schema (views, grants, search_path)."""
    return [
        f"-- Dedicated analytical schema — isolates the safe layer from the\n"
        f"-- registry. The insight_ai role gets USAGE here and only here.\n"
        f'CREATE SCHEMA IF NOT EXISTS "{p.target_schema}";'
    ]


def ddl_for_safe_views(p: Proposal) -> List[str]:
    out: List[str] = []
    for t in p.analytical_tables:
        kept = [c for c in t.columns if c.keep is True]
        if not kept:
            continue
        cols = ",\n    ".join(f'"{c.name}"' for c in kept)
        sql = (
            f'-- {len(kept)} safe columns from {p.source_schema}.{t.source}; '
            f'{len(t.columns) - len(kept)} dropped\n'
            f'CREATE OR REPLACE VIEW "{p.target_schema}"."{t.safe_view_name}" AS\n'
            f"SELECT\n    {cols}\n"
            f'FROM "{p.source_schema}"."{t.source}";'
        )
        out.append(sql)
    return out


def ddl_for_materialized_views(p: Proposal) -> List[str]:
    """Materialized views are TODO. Today we keep this entry point so the
    proposal shape stays stable and operators who hand-write an MV in their
    proposal still get it emitted. Auto-generation is deferred to the
    declarative aggregate-MV grammar described in the proposal YAML."""
    out: List[str] = []
    for mv in p.materialized_views:
        if not mv.join:
            out.append(f"-- SKIPPED mv {mv.name} (no join clause in proposal)")
            continue
        sql = (
            f'-- {mv.description}\n'
            f'DROP MATERIALIZED VIEW IF EXISTS "{p.target_schema}"."{mv.name}";\n'
            f'CREATE MATERIALIZED VIEW "{p.target_schema}"."{mv.name}" AS\n'
            f'{mv.join};'
        )
        out.append(sql)
        for col in mv.indexes:
            idx_name = f"ix_{mv.name}_{col}"
            out.append(
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                f'ON "{p.target_schema}"."{mv.name}"("{col}");'
            )
    return out


def ddl_for_role_and_grants(
    p: Proposal,
    role_name: str,
    role_password: str,
    db_name: str,
) -> List[str]:
    """Create the AI role and grant it access to the safe layer only.

    Key boundary: USAGE is granted on `target_schema` only — NOT on
    `source_schema`. Without USAGE on the source schema, the role can't
    reference base tables even if it knew their names. The role's
    `search_path` is then set to `target_schema` so LLM-emitted SQL that
    says `FROM mv_individuals_summary` resolves there transparently."""
    out: List[str] = []
    out.append(
        f"-- Recreate role with a fresh password\n"
        f'DROP ROLE IF EXISTS "{role_name}";\n'
        f'CREATE ROLE "{role_name}" LOGIN PASSWORD \'{role_password}\';'
    )
    out.append(f'GRANT CONNECT ON DATABASE "{db_name}" TO "{role_name}";')
    out.append(f'GRANT USAGE ON SCHEMA "{p.target_schema}" TO "{role_name}";')

    safe_targets = [t.safe_view_name for t in p.analytical_tables]
    safe_targets += [mv.name for mv in p.materialized_views]
    for tgt in safe_targets:
        out.append(
            f'GRANT SELECT ON "{p.target_schema}"."{tgt}" TO "{role_name}";'
        )

    # Default search_path so unqualified table references resolve to the
    # safe layer. We deliberately don't include the source schema — even
    # if someone REVOKE'd USAGE from us elsewhere, this guarantees the
    # role never even *looks* in the registry's schema for a table.
    out.append(
        f'ALTER ROLE "{role_name}" '
        f"SET search_path = \"{p.target_schema}\";"
    )

    out.append(f'ALTER ROLE "{role_name}" SET statement_timeout = \'30s\';')
    out.append(f'ALTER ROLE "{role_name}" SET lock_timeout = \'5s\';')
    out.append(f'ALTER ROLE "{role_name}" SET idle_in_transaction_session_timeout = \'60s\';')
    return out


# -----------------------------------------------------------------------------
# Operational catalog YAML — consumed by the backend
# -----------------------------------------------------------------------------
def operational_catalog(p: Proposal) -> str:
    tables_out = []
    for t in p.analytical_tables:
        cols_out = []
        for c in t.columns:
            if not c.keep:
                continue
            entry = {
                "name": c.name,
                "type": c.pg_type,
                "pii": c.pii,
                "description": c.reason or None,
            }
            if c.enum_values:
                # Authoritative enum values from the registry extension.
                # The runtime LLM prompt renders these inline so the model
                # filters with exact declared values (`gender = 'F'`, not
                # `'FEMALE'`).
                entry["enum"] = c.enum_values
            if c.mixin:
                entry["mixin"] = c.mixin
            cols_out.append(entry)
        tables_out.append({
            "name": t.safe_view_name,
            "schema": p.target_schema,
            "description": f"PII-safe view over {p.source_schema}.{t.source}",
            "columns": cols_out,
        })

    # Materialised views also appear as queryable entities.
    for mv in p.materialized_views:
        tables_out.append({
            "name": mv.name,
            "schema": p.target_schema,
            "description": mv.description,
            "refresh": mv.refresh,
            "indexed": mv.indexes,
        })

    safe_view_names = (
        [t.safe_view_name for t in p.analytical_tables]
        + [mv.name for mv in p.materialized_views]
    )

    catalog = {
        "deployment": p.deployment,
        "tables": tables_out,
        "pii_columns_in_source": p.pii_columns_in_source,
        "safe_views": safe_view_names,
        "geo_columns": [],
    }
    return yaml.safe_dump(catalog, sort_keys=False, allow_unicode=True)


# -----------------------------------------------------------------------------
# Audit
# -----------------------------------------------------------------------------
def audit_query(role_name: str, schemas: list[str]) -> str:
    """Return a SQL block to run AS the insight_ai role. Lists every column
    visible to it across the listed schemas. Caller diffs against
    `pii_columns_in_source` to confirm no PII leaks."""
    schema_in = ", ".join(f"'{s}'" for s in schemas)
    return (
        f"SET ROLE \"{role_name}\";\n"
        "SELECT table_schema, table_name, column_name\n"
        "FROM information_schema.columns\n"
        f"WHERE table_schema IN ({schema_in})\n"
        "ORDER BY table_schema, table_name, ordinal_position;\n"
        "RESET ROLE;\n"
    )
