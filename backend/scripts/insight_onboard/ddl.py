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


def ddl_for_safe_views(p: Proposal, schema: str = "public") -> List[str]:
    out: List[str] = []
    for t in p.analytical_tables:
        kept = [c for c in t.columns if c.keep is True]
        if not kept:
            continue
        cols = ",\n    ".join(f'"{c.name}"' for c in kept)
        sql = (
            f'-- {len(kept)} safe columns from {t.source}; '
            f'{len(t.columns) - len(kept)} dropped\n'
            f'CREATE OR REPLACE VIEW "{schema}"."{t.safe_view_name}" AS\n'
            f"SELECT\n    {cols}\n"
            f'FROM "{schema}"."{t.source}";'
        )
        out.append(sql)
    return out


def ddl_for_materialized_views(p: Proposal, schema: str = "public") -> List[str]:
    out: List[str] = []
    for mv in p.materialized_views:
        if not mv.join:
            out.append(f"-- SKIPPED mv {mv.name} (no join clause in proposal)")
            continue
        sql = (
            f'-- {mv.description}\n'
            f'DROP MATERIALIZED VIEW IF EXISTS "{schema}"."{mv.name}";\n'
            f'CREATE MATERIALIZED VIEW "{schema}"."{mv.name}" AS\n'
            f'{mv.join};'
        )
        out.append(sql)
        for col in mv.indexes:
            idx_name = f"ix_{mv.name}_{col}"
            out.append(
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                f'ON "{schema}"."{mv.name}"("{col}");'
            )
    return out


def ddl_for_role_and_grants(
    p: Proposal,
    role_name: str,
    role_password: str,
    db_name: str,
    schema: str = "public",
) -> List[str]:
    out: List[str] = []
    out.append(
        f"-- Recreate role with a fresh password\n"
        f'DROP ROLE IF EXISTS "{role_name}";\n'
        f'CREATE ROLE "{role_name}" LOGIN PASSWORD \'{role_password}\';'
    )
    out.append(f'GRANT CONNECT ON DATABASE "{db_name}" TO "{role_name}";')
    out.append(f'GRANT USAGE   ON SCHEMA "{schema}" TO "{role_name}";')

    safe_targets = [t.safe_view_name for t in p.analytical_tables]
    safe_targets += [mv.name for mv in p.materialized_views]
    for tgt in safe_targets:
        out.append(f'GRANT SELECT ON "{schema}"."{tgt}" TO "{role_name}";')

    # Defense in depth — explicitly revoke any access to source tables that
    # have PII.
    pii_source_tables = sorted({t.source for t in p.analytical_tables})
    for src in pii_source_tables:
        out.append(f'REVOKE ALL ON "{schema}"."{src}" FROM "{role_name}";')

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
            cols_out.append({
                "name": c.name,
                "type": c.pg_type,
                "pii": c.pii,
                "description": c.reason or None,
            })
        tables_out.append({
            "name": t.safe_view_name,
            "schema": "public",
            "description": f"PII-safe view over {t.source}",
            "columns": cols_out,
        })

    # Materialised views also appear as queryable entities.
    for mv in p.materialized_views:
        tables_out.append({
            "name": mv.name,
            "schema": "public",
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
def audit_query(role_name: str, schema: str = "public") -> str:
    """Return a SQL block to run AS the insight_ai role. Lists every table/view
    visible to it. Caller diffs against `pii_columns_in_source`."""
    return (
        f"SET ROLE \"{role_name}\";\n"
        "SELECT table_schema, table_name, column_name\n"
        "FROM information_schema.columns\n"
        f"WHERE table_schema = '{schema}'\n"
        "ORDER BY table_schema, table_name, ordinal_position;\n"
        "RESET ROLE;\n"
    )
