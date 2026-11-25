from openg2p_insight.utils.sql_safety import static_analyze


def test_select_passes_without_blockers():
    issues = static_analyze(
        "SELECT region_code, COUNT(*) AS total FROM mv_individuals_summary "
        "WHERE gender = 'F' GROUP BY region_code"
    )
    assert not any(i.blocker for i in issues)


def test_update_is_blocked():
    issues = static_analyze("UPDATE individuals SET phone = NULL")
    assert any(i.blocker for i in issues)


def test_select_star_is_blocked():
    issues = static_analyze("SELECT * FROM individuals")
    assert any(i.blocker for i in issues)


def test_cross_join_is_blocked():
    issues = static_analyze("SELECT a.id FROM a CROSS JOIN b")
    assert any(i.blocker for i in issues)


def test_aggregate_without_where_is_not_warned_about():
    """"How many farmers are there" has no WHERE and needs none.

    The warning used to fire on every registry-wide aggregate, i.e. on most
    legitimate questions, which is how a correct answer to the simplest possible
    question arrived wrapped in a caveat.
    """
    for sql in (
        "SELECT COUNT(*) AS total FROM fr_rpt_farmer",
        "SELECT geo_2, COUNT(*) AS n FROM fr_rpt_farmer GROUP BY geo_2",
        "SELECT AVG(parcel_land_size_ha) FROM fr_rpt_land",
        "SELECT farmer_id FROM fr_rpt_farmer LIMIT 100",
    ):
        messages = [i.message for i in static_analyze(sql)]
        assert not any("WHERE" in m for m in messages), sql


def test_raw_rows_without_where_is_warned_about():
    """The case the warning is actually for: the whole table comes back."""
    messages = [i.message for i in static_analyze(
        "SELECT farmer_id, geo_2 FROM fr_rpt_farmer")]
    assert any("WHERE" in m for m in messages)


def test_column_names_containing_count_do_not_look_aggregated():
    """`discount` and `account_id` contain COUNT but aggregate nothing."""
    messages = [i.message for i in static_analyze(
        "SELECT account_id, discount FROM fr_rpt_farmer")]
    assert any("WHERE" in m for m in messages)
