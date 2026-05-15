"""
Unit tests for the SQL validator (sqlglot-based).

The validator parses LLM-emitted SQL, enforces the catalog allow-list,
substitutes PII placeholders, and injects `_k_count` on grouped queries
against row-level views. These tests run without a database — they
construct an isolated SqlValidatorService with a mocked SchemaService.
"""

from __future__ import annotations

import pytest

from openg2p_insight.services.schema_service import Catalog, ColumnSpec, SchemaService, TableSpec
from openg2p_insight.services.sql_validator_service import (
    K_COUNT_COL,
    SqlValidationError,
    SqlValidatorService,
)


def _svc(safe_views: list[str], tables: list[TableSpec] | None = None) -> SqlValidatorService:
    schema = SchemaService.__new__(SchemaService)
    schema.catalog = Catalog(safe_views=safe_views, tables=tables or [])
    svc = SqlValidatorService.__new__(SqlValidatorService)
    svc._schema = schema
    return svc


def _individuals_table() -> TableSpec:
    return TableSpec(
        name="mv_individuals_summary",
        columns=[
            ColumnSpec(name="region_code", type="string"),
            ColumnSpec(name="gender", type="enum"),
            ColumnSpec(name="pmt_score", type="float"),
            ColumnSpec(name="monthly_income", type="float"),
            ColumnSpec(name="id", type="int", pii="pii"),  # blocked
        ],
    )


def _aggregates_table() -> TableSpec:
    return TableSpec(
        name="mv_district_aggregates",
        columns=[
            ColumnSpec(name="region_code", type="string"),
            ColumnSpec(name="total", type="int"),
        ],
    )


def test_simple_count_passes():
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    v = svc.validate("SELECT COUNT(*) AS total FROM mv_individuals_summary")
    assert "mv_individuals_summary" in v.sql
    assert v.has_k_count is False  # no GROUP BY
    assert v.from_tables == ["mv_individuals_summary"]


def test_grouped_query_gets_k_count_injected():
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    v = svc.validate(
        "SELECT region_code, COUNT(*) AS total FROM mv_individuals_summary GROUP BY region_code"
    )
    assert v.has_k_count is True
    assert K_COUNT_COL in v.sql
    assert v.group_by_columns == ["region_code"]


def test_grouped_query_on_aggregated_view_skips_k_count():
    """mv_district_aggregates is pre-aggregated; injecting `_k_count` based on
    row count would suppress everything. The validator should skip the
    injection in that case."""
    svc = _svc(["mv_district_aggregates"], [_aggregates_table()])
    v = svc.validate(
        "SELECT region_code, SUM(total) AS metric_value "
        "FROM mv_district_aggregates GROUP BY region_code"
    )
    assert v.has_k_count is False
    assert K_COUNT_COL not in v.sql


def test_select_star_rejected():
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    with pytest.raises(SqlValidationError, match="SELECT \\*"):
        svc.validate("SELECT * FROM mv_individuals_summary")


def test_count_star_allowed():
    """COUNT(*) uses an exp.Star but is fine — only bare SELECT * is blocked."""
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    v = svc.validate("SELECT COUNT(*) AS total FROM mv_individuals_summary")
    assert "COUNT(*)" in v.sql


def test_unsafe_table_rejected():
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    with pytest.raises(SqlValidationError, match="not in the allow-listed"):
        svc.validate("SELECT COUNT(*) FROM g2p_register_individuals")


def test_pii_column_rejected():
    """`id` is marked pii=pii on the catalog, so it is excluded from
    safe_columns() and any reference to it should be rejected."""
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    with pytest.raises(SqlValidationError, match="not in any allow-listed"):
        svc.validate("SELECT id FROM mv_individuals_summary")


def test_unknown_column_rejected():
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    with pytest.raises(SqlValidationError, match="not in any allow-listed"):
        svc.validate("SELECT bogus_col FROM mv_individuals_summary")


def test_pii_placeholder_rebound():
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    v = svc.validate(
        "SELECT COUNT(*) FROM mv_individuals_summary WHERE region_code = '[PLACE_1]'",
        placeholder_map={"[PLACE_1]": "ET-OR"},
    )
    # Placeholder rewritten as bind parameter; actual value in params.
    assert "[PLACE_1]" not in v.sql
    assert "ET-OR" in v.params.values()


def test_ddl_rejected():
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    with pytest.raises(SqlValidationError):
        svc.validate("DROP TABLE mv_individuals_summary")


def test_multi_statement_rejected():
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    with pytest.raises(SqlValidationError, match="single SQL statement"):
        svc.validate(
            "SELECT COUNT(*) FROM mv_individuals_summary; "
            "SELECT COUNT(*) FROM mv_individuals_summary"
        )


def test_select_distinct_passes():
    """The whole motivation for moving to LLM-emitted SQL: 'list the regions'
    needs SELECT DISTINCT, which the old Intent shape couldn't express."""
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    v = svc.validate("SELECT DISTINCT region_code FROM mv_individuals_summary")
    assert "DISTINCT" in v.sql.upper()
    assert v.has_k_count is False  # SELECT DISTINCT has no GROUP BY


def test_case_when_ratio_passes():
    """Ratios via CASE WHEN — another shape the old Intent couldn't express."""
    svc = _svc(["mv_individuals_summary"], [_individuals_table()])
    v = svc.validate(
        "SELECT SUM(CASE WHEN gender = 'F' THEN 1 ELSE 0 END)::float / COUNT(*) AS share "
        "FROM mv_individuals_summary"
    )
    assert "CASE" in v.sql.upper()
