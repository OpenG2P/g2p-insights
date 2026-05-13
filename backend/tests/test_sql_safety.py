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
