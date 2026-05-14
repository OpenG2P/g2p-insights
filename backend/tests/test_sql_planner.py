"""
Unit tests for the SQL planner.

These tests do NOT touch a database — they construct an isolated
SqlPlannerService with a mocked SchemaService and inspect the produced SQL.
"""

from __future__ import annotations

import pytest

from openg2p_insight.services.llm_service import Filter, Intent, VizSpec
from openg2p_insight.services.schema_service import Catalog, SchemaService
from openg2p_insight.services.sql_planner_service import (
    K_COUNT_COL,
    SqlPlannerService,
)


def _svc_with_catalog(safe_views: list[str]) -> SqlPlannerService:
    schema = SchemaService.__new__(SchemaService)
    schema.catalog = Catalog(safe_views=safe_views)

    svc = SqlPlannerService.__new__(SqlPlannerService)
    svc._schema = schema
    return svc


def test_count_with_group_by_projects_k_count():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count",
        group_by=["region_code"],
        viz=VizSpec(type="bar"),
    )
    planned = svc.build(intent, placeholder_map={})
    assert planned.has_k_count is True
    assert "COUNT(*) AS total" in planned.sql
    assert f"AS {K_COUNT_COL}" in planned.sql
    assert "FROM mv_individuals_summary" in planned.sql
    assert "GROUP BY region_code" in planned.sql


def test_avg_requires_metric_column():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="avg",
        metric_column=None,
        group_by=["woreda_code"],
    )
    with pytest.raises(ValueError) as ei:
        svc.build(intent, placeholder_map={})
    assert "metric_column" in str(ei.value)


def test_avg_with_metric_column_emits_avg_col():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="avg",
        metric_column="pmt_score",
        group_by=["region_code"],
    )
    planned = svc.build(intent, placeholder_map={})
    assert "AVG(pmt_score) AS metric_value" in planned.sql
    assert f"AS {K_COUNT_COL}" in planned.sql


def test_in_filter_expands_to_multiple_params():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count",
        filters=[Filter(col="region_code", op="in", val=["ET-AA", "ET-OR", "ET-AM"])],
        group_by=["region_code"],
    )
    planned = svc.build(intent, placeholder_map={})
    # SQL has three placeholders, params has three bound values.
    assert planned.sql.count(":p0_") == 3
    assert planned.params == {"p0_0": "ET-AA", "p0_1": "ET-OR", "p0_2": "ET-AM"}


def test_empty_in_emits_false():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count",
        filters=[Filter(col="region_code", op="in", val=[])],
    )
    planned = svc.build(intent, placeholder_map={})
    assert "WHERE FALSE" in planned.sql


def test_unsafe_entity_rejected():
    svc = _svc_with_catalog(["v_individuals_safe"])
    intent = Intent(entity="g2p_register_individuals", metric="count")
    with pytest.raises(ValueError):
        svc.build(intent, placeholder_map={})


def test_unsafe_identifier_rejected():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count",
        group_by=["region_code; DROP TABLE individuals --"],
    )
    with pytest.raises(ValueError):
        svc.build(intent, placeholder_map={})


def test_count_distinct_emits_proper_sql():
    """`metric=count_distinct` with `metric_column=region_code` must produce
    COUNT(DISTINCT region_code) — needed for 'how many regions' style
    questions where plain COUNT(*) returns the wrong number."""
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count_distinct",
        metric_column="region_code",
    )
    planned = svc.build(intent, placeholder_map={})
    assert "COUNT(DISTINCT region_code) AS total" in planned.sql


def test_count_distinct_requires_metric_column():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(entity="mv_individuals_summary", metric="count_distinct")
    with pytest.raises(ValueError) as ei:
        svc.build(intent, placeholder_map={})
    assert "metric_column" in str(ei.value)


def test_order_by_count_is_normalised_to_total_alias():
    """LLMs naturally emit ORDER BY count DESC; the planner aliases COUNT(*)
    as `total`, so without normalisation Postgres errors with 'column count
    does not exist'."""
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count",
        group_by=["region_code"],
        order_by=["count DESC"],
    )
    planned = svc.build(intent, placeholder_map={})
    assert "ORDER BY total DESC" in planned.sql
    assert "count DESC" not in planned.sql.lower().replace("count(", "")


def test_order_by_with_count_distinct_uses_total_alias():
    """count_distinct projects as `total`, not `metric_value`. ORDER BY
    referencing the natural names must normalise to `total`."""
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count_distinct",
        metric_column="region_code",
        order_by=["count DESC", "metric_value DESC"],
    )
    planned = svc.build(intent, placeholder_map={})
    # Both natural-name forms should normalise to `total`.
    assert "ORDER BY total DESC, total DESC" in planned.sql
    assert "metric_value" not in planned.sql  # the alias for count_distinct is total


def test_order_by_avg_metric_uses_metric_value_alias():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="avg",
        metric_column="pmt_score",
        group_by=["region_code"],
        order_by=["avg DESC"],
    )
    planned = svc.build(intent, placeholder_map={})
    assert "ORDER BY metric_value DESC" in planned.sql


def test_order_by_real_column_passes_through():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count",
        group_by=["region_code"],
        order_by=["region_code ASC"],
    )
    planned = svc.build(intent, placeholder_map={})
    assert "ORDER BY region_code ASC" in planned.sql


def test_placeholder_re_injection():
    svc = _svc_with_catalog(["mv_individuals_summary"])
    intent = Intent(
        entity="mv_individuals_summary",
        metric="count",
        filters=[Filter(col="region_code", op="eq", val="[PLACE_1]")],
    )
    planned = svc.build(intent, placeholder_map={"[PLACE_1]": "ET-OR"})
    assert planned.params == {"p0": "ET-OR"}
