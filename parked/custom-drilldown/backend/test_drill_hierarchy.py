"""
Drill hierarchy tests.

The whole point of the drill engine is that it is country-agnostic: no level
names, no fixed depth, no country baked into code. These tests assert exactly
that, so a regression that hardcodes "region"/"woreda" or assumes 5 levels
fails loudly.
"""

from __future__ import annotations

import pytest

from openg2p_insight.services.drill_service import DrillError, DrillService


class _StubCatalog:
    def __init__(self, geo_columns, safe_views=None):
        self.geo_columns = geo_columns
        self._safe = safe_views or []

    def safe_table_names(self):
        return list(self._safe)


class _GeoCol:
    def __init__(self, level_mnemonic, column):
        self.level_mnemonic = level_mnemonic
        self.column = column


def _service(geo_columns=(), safe_views=()):
    """DrillService with its collaborators bypassed — we only exercise the
    pure hierarchy/SQL-shaping logic here."""
    svc = DrillService.__new__(DrillService)
    svc._schema = type("S", (), {"catalog": _StubCatalog(list(geo_columns), list(safe_views))})()
    return svc


# --------------------------------------------------------------- ordering


def test_orders_levels_from_parent_chain_not_input_order():
    """Input arrives unordered; order must come from parent_level_id."""
    svc = _service([_GeoCol("country", "c"), _GeoCol("region", "r")])
    raw = [
        {"level_id": "l2", "level_mnemonic": "district", "parent_level_id": "l1"},
        {"level_id": "l0", "level_mnemonic": "country", "parent_level_id": None},
        {"level_id": "l1", "level_mnemonic": "region", "parent_level_id": "l0"},
    ]
    levels = svc._order_levels(raw)
    assert [lv.level_id for lv in levels] == ["l0", "l1", "l2"]
    assert levels[-1].is_leaf is True
    assert levels[0].is_leaf is False


def test_works_for_a_different_country_shape():
    """A 3-level country with entirely different level names must work with
    no code change — this is the country-agnostic guarantee."""
    svc = _service([_GeoCol("nation", "n"), _GeoCol("province", "p"), _GeoCol("barangay", "b")])
    raw = [
        {"level_id": "a1", "level_mnemonic": "province", "parent_level_id": "a0"},
        {"level_id": "a0", "level_mnemonic": "nation", "parent_level_id": None},
        {"level_id": "a2", "level_mnemonic": "barangay", "parent_level_id": "a1"},
    ]
    levels = svc._order_levels(raw)
    assert [lv.level_mnemonic for lv in levels] == ["nation", "province", "barangay"]
    assert all(lv.is_queryable for lv in levels)


def test_levels_without_catalog_mapping_are_not_queryable():
    """The hierarchy may go deeper than the data does; those levels are
    reported but flagged so the UI stops offering drill."""
    svc = _service([_GeoCol("country", "c"), _GeoCol("region", "r")])
    raw = [
        {"level_id": "l0", "level_mnemonic": "country", "parent_level_id": None},
        {"level_id": "l1", "level_mnemonic": "region", "parent_level_id": "l0"},
        {"level_id": "l2", "level_mnemonic": "ward", "parent_level_id": "l1"},
    ]
    levels = svc._order_levels(raw)
    assert [lv.is_queryable for lv in levels] == [True, True, False]


def test_cycle_in_levels_terminates():
    """Malformed master data must not hang the request."""
    svc = _service()
    raw = [
        {"level_id": "x0", "level_mnemonic": "a", "parent_level_id": None},
        {"level_id": "x1", "level_mnemonic": "b", "parent_level_id": "x0"},
        {"level_id": "x0", "level_mnemonic": "a-dup", "parent_level_id": "x1"},
    ]
    levels = svc._order_levels(raw)
    assert len(levels) <= 3  # terminated rather than looping


# ------------------------------------------------------------- safety


def test_table_must_be_in_catalog_allow_list():
    svc = _service(safe_views=["v_household_safe"])
    with pytest.raises(DrillError):
        svc._validate_table("pg_catalog.pg_user")
    assert svc._validate_table("v_household_safe") == "v_household_safe"


def test_rejects_unknown_aggregate():
    from openg2p_insight.schemas.drill import DrillMeasure

    svc = _service()
    with pytest.raises(DrillError):
        svc._build_measure(DrillMeasure(agg="; drop table x --", column="a"))


def test_rejects_unsafe_identifier():
    from openg2p_insight.schemas.drill import DrillMeasure

    svc = _service()
    with pytest.raises(DrillError):
        svc._build_measure(DrillMeasure(agg="sum", column="amount); DROP TABLE t--"))


def test_count_needs_no_column():
    from openg2p_insight.schemas.drill import DrillMeasure

    svc = _service()
    expr, label = svc._build_measure(DrillMeasure(agg="count"))
    assert expr == "COUNT(*)"
    assert label == "Count"


# ------------------------------------------------- ancestor scoping (regression)


def test_ancestor_path_produces_bound_filters_for_every_level():
    """Regression: an early version dropped ancestor filters entirely, which
    silently returned unscoped rows (a district drill showing every region)."""
    from openg2p_insight.schemas.drill import DrillPathItem, DrillRequest

    svc = _service([_GeoCol("country", "country_col"), _GeoCol("region", "region_col")])
    levels = svc._order_levels(
        [
            {"level_id": "l0", "level_mnemonic": "country", "parent_level_id": None},
            {"level_id": "l1", "level_mnemonic": "region", "parent_level_id": "l0"},
        ]
    )
    req = DrillRequest(
        table="v",
        path=[
            DrillPathItem(level_id="l0", level_value_id="kamuntu"),
            DrillPathItem(level_id="l1", level_value_id="kamuntu/kilima"),
        ],
    )
    where, params = svc._build_parent_filter(req, svc._geo_column_map(), levels)
    assert "country_col = :drill_p0" in where
    assert "region_col = :drill_p1" in where
    # Path-style MDS ids are reduced to their leaf token to match registry cols.
    assert params == {"drill_p0": "kamuntu", "drill_p1": "kilima"}


def test_unmappable_ancestor_refuses_rather_than_widening():
    """If an ancestor cannot be filtered we must fail, not silently return a
    wider result set."""
    from openg2p_insight.schemas.drill import DrillPathItem, DrillRequest

    svc = _service([_GeoCol("country", "country_col")])  # no mapping for region
    levels = svc._order_levels(
        [
            {"level_id": "l0", "level_mnemonic": "country", "parent_level_id": None},
            {"level_id": "l1", "level_mnemonic": "region", "parent_level_id": "l0"},
        ]
    )
    req = DrillRequest(
        table="v",
        path=[DrillPathItem(level_id="l1", level_value_id="kamuntu/kilima")],
    )
    with pytest.raises(DrillError):
        svc._build_parent_filter(req, svc._geo_column_map(), levels)
