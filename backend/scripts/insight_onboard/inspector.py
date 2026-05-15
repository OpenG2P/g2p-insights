"""Read-only inspection of the registry Postgres."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import psycopg

from .heuristics import ColumnStats


@dataclass
class ForeignKey:
    src_table: str
    src_column: str
    dst_table: str
    dst_column: str


def list_tables(conn: psycopg.Connection, schema: str = "public") -> List[str]:
    sql = """
        SELECT tablename FROM pg_tables
        WHERE schemaname = %s
        ORDER BY tablename
    """
    with conn.cursor() as cur:
        cur.execute(sql, (schema,))
        return [r[0] for r in cur.fetchall()]


def list_columns(conn: psycopg.Connection, table: str, schema: str = "public") -> List[dict]:
    sql = """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(sql, (schema, table))
        return [
            {"name": r[0], "pg_type": r[1], "nullable": (r[2] == "YES")}
            for r in cur.fetchall()
        ]


def list_foreign_keys(conn: psycopg.Connection, schema: str = "public") -> List[ForeignKey]:
    sql = """
        SELECT
            tc.table_name AS src_table,
            kcu.column_name AS src_column,
            ccu.table_name AS dst_table,
            ccu.column_name AS dst_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema  = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema  = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (schema,))
        return [
            ForeignKey(r[0], r[1], r[2], r[3]) for r in cur.fetchall()
        ]


def column_stats(
    conn: psycopg.Connection,
    table: str,
    column: str,
    pg_type: str,
    nullable: bool,
    schema: str = "public",
    sample_n: int = 20,
) -> ColumnStats:
    """Pull cardinality, null rate, and a small set of distinct sample values.

    Sample values stay in-process (passed to local heuristics only). Aggregated
    statistics (counts, rates, value SHAPES) are what might later be sent to
    the LLM — never raw values.
    """
    fq = f'"{schema}"."{table}"."{column}"'
    table_fq = f'"{schema}"."{table}"'

    # JSON / JSONB / bytea and a few other types have no default equality
    # operator, so COUNT(DISTINCT col) and SELECT DISTINCT col raise
    # `UndefinedFunction`. We treat these as "no stats" — caller still gets
    # a usable ColumnStats and the type-aware classifier downstream tags
    # them as non-analytical anyway.
    no_distinct_types = {"json", "jsonb", "bytea", "xml", "tsvector", "tsquery", "hstore"}
    skip_distinct = pg_type.lower() in no_distinct_types

    distinct_count = 0
    null_rate = 0.0
    samples: List[str] = []

    with conn.cursor() as cur:
        if not skip_distinct:
            # n_distinct from pg_stats is approximate and cheap; fall back
            # to COUNT(DISTINCT) when it's not yet populated.
            cur.execute(
                "SELECT n_distinct, null_frac FROM pg_stats "
                "WHERE schemaname=%s AND tablename=%s AND attname=%s",
                (schema, table, column),
            )
            row = cur.fetchone()
            if row is None or row[0] is None:
                try:
                    col_ident = f'"{column}"'
                    cur.execute(
                        f"SELECT COUNT(DISTINCT {col_ident}), "
                        f"AVG(CASE WHEN {col_ident} IS NULL THEN 1 ELSE 0 END) "
                        f"FROM {table_fq}"
                    )
                    distinct_count, null_rate_raw = cur.fetchone()
                    null_rate = float(null_rate_raw or 0)
                    distinct_count = int(distinct_count or 0)
                except psycopg.errors.UndefinedFunction:
                    # Custom type without an `=` operator — give up on stats.
                    conn.rollback()
                    skip_distinct = True
            else:
                n_distinct, null_frac = row
                null_rate = float(null_frac or 0)
                if n_distinct < 0:
                    cur.execute("SELECT reltuples::bigint FROM pg_class "
                                "WHERE oid = (%s::regclass)", (f"{schema}.{table}",))
                    reltuples = cur.fetchone()[0] or 0
                    distinct_count = int(reltuples * abs(n_distinct))
                else:
                    distinct_count = int(n_distinct)

        if not skip_distinct:
            try:
                col_ident = f'"{column}"'
                cur.execute(
                    f"SELECT DISTINCT {col_ident}::text "
                    f"FROM {table_fq} WHERE {col_ident} IS NOT NULL LIMIT %s",
                    (sample_n,),
                )
                samples = [r[0] for r in cur.fetchall() if r[0] is not None]
            except psycopg.errors.UndefinedFunction:
                conn.rollback()
                samples = []

    return ColumnStats(
        name=column,
        table=table,
        pg_type=pg_type,
        nullable=nullable,
        distinct_count=distinct_count,
        null_rate=null_rate,
        sample_values=samples,
    )


def can_connect_as(dsn: str) -> Optional[str]:
    """Return None on success, or an error message string."""
    try:
        with psycopg.connect(dsn, connect_timeout=5):
            return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)
