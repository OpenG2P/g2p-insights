from openg2p_insight.utils.k_anonymity import K_COUNT_COL, apply_k_anonymity


def test_small_groups_are_suppressed_with_k_count():
    rows = [
        {"region_code": "A", "total": 100, K_COUNT_COL: 100},
        {"region_code": "B", "total": 3,   K_COUNT_COL: 3},
    ]
    out, suppressed = apply_k_anonymity(rows, group_by=["region_code"], k=5)
    assert suppressed == 1
    assert out[0]["region_code"] == "A"
    assert out[1]["region_code"] == "<suppressed>"
    assert out[1]["total"] == "<5"


def test_avg_metric_suppressed_even_without_count_in_user_view():
    """For AVG queries the planner sets metric_value AND _k_count. Small
    groups must be suppressed on the metric too."""
    rows = [
        {"woreda": "X", "metric_value": 250.5, K_COUNT_COL: 100},
        {"woreda": "Y", "metric_value": 999.0, K_COUNT_COL: 2},
    ]
    out, suppressed = apply_k_anonymity(rows, group_by=["woreda"], k=5)
    assert suppressed == 1
    assert out[1]["woreda"] == "<suppressed>"
    assert out[1]["metric_value"] == "<5"


def test_no_k_count_fails_closed():
    """If the planner forgot to project _k_count, we MUST suppress everything
    — silently allowing the result would be a re-identification risk."""
    rows = [{"region_code": "A", "metric_value": 42.0}]
    out, suppressed = apply_k_anonymity(rows, group_by=["region_code"], k=5)
    assert out == []
    assert suppressed == 1


def test_no_op_when_above_threshold():
    rows = [
        {"x": "P", "total": 10, K_COUNT_COL: 10},
        {"x": "Q", "total": 20, K_COUNT_COL: 20},
    ]
    out, suppressed = apply_k_anonymity(rows, group_by=["x"], k=5)
    assert suppressed == 0
    assert [r["x"] for r in out] == ["P", "Q"]
