"""
Group-comparison primitive — answers "do these groups differ on this metric?"

Inputs (AnalyticsRequest.params):
  - metric    : numeric column to compare (e.g. 'pmt_score', 'monthly_income')
  - group_by  : single categorical column to compare across (e.g. 'gender')
  - test      : optional override — one of 'auto', 't', 'welch', 'mannwhitney', 'anova'

Outputs:
  - per-group stats: n, mean, median, std, q1, q3, min, max
  - statistical test result: name, statistic, p-value, dof (when applicable)
  - effect size: Cohen's d for two-group comparisons
  - boxplot viz_spec — Plotly-compatible
  - human-readable assumptions

Designed as a generic primitive: any (numeric column × categorical column)
pair. The same function answers "do women have lower PMT", "do households in
the highlands earn more", "are IDPs more food-insecure", etc.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

import numpy as np
from scipy import stats

from ..schemas.analytics import AnalyticsRequest
from ._extract import extract


async def group_compare(req: AnalyticsRequest) -> dict:
    metric = req.params.get("metric") or req.column
    group_by = req.params.get("group_by")
    test_override = (req.params.get("test") or "auto").lower()

    if not metric or not group_by:
        return {
            "result": {},
            "viz_spec": {},
            "assumptions": ["error: params.metric and params.group_by are required"],
        }

    df = await extract(req.entity, [metric, group_by], req.filters, req.sample_size)
    if df.empty:
        return {"result": {}, "viz_spec": {}, "assumptions": ["No rows matched filters"]}

    df = df.dropna(subset=[metric, group_by])
    if df.empty:
        return {"result": {}, "viz_spec": {}, "assumptions": ["All rows had nulls in the requested columns"]}

    # ---- per-group descriptive stats ---------------------------------------
    grouped = df.groupby(group_by)[metric]
    groups: Dict[str, Dict[str, float]] = {}
    for name, series in grouped:
        arr = series.to_numpy(dtype=float)
        groups[str(name)] = {
            "n": int(arr.size),
            "mean": float(np.mean(arr)),
            "median": float(np.median(arr)),
            "std": float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0,
            "q1": float(np.percentile(arr, 25)),
            "q3": float(np.percentile(arr, 75)),
            "min": float(arr.min()),
            "max": float(arr.max()),
        }

    # ---- statistical test --------------------------------------------------
    group_values = [grouped.get_group(name).to_numpy(dtype=float) for name in grouped.groups]
    group_names = list(grouped.groups.keys())
    n_groups = len(group_values)

    test_result: Dict[str, Any] = {"name": "none", "applicable": False}
    if n_groups < 2:
        test_result["reason"] = "Need at least two groups to compare."
    elif any(g.size < 2 for g in group_values):
        test_result["reason"] = "At least one group has fewer than 2 observations."
    else:
        test_result = _run_test(test_override, group_values, group_names)

    # ---- effect size (only meaningful for 2-group comparisons) -------------
    effect_size = None
    if n_groups == 2 and all(g.size >= 2 for g in group_values):
        effect_size = _cohens_d(group_values[0], group_values[1])

    # ---- assumptions string list -------------------------------------------
    assumptions: List[str] = [
        f"Compared `{metric}` across `{group_by}` ({n_groups} groups, n={len(df)}).",
    ]
    if test_result.get("applicable"):
        assumptions.append(
            f"Test: {test_result['name']}. Statistic={test_result.get('statistic'):.3f}, "
            f"p={test_result.get('p_value'):.4g}."
        )
        assumptions.append(_interpret_p(test_result.get("p_value", 1.0)))
    if effect_size is not None:
        assumptions.append(
            f"Effect size (Cohen's d) = {effect_size:.3f} — {_classify_d(effect_size)}."
        )

    # ---- viz_spec — boxplot ------------------------------------------------
    return {
        "result": {
            "groups": groups,
            "test": test_result,
            "effect_size_cohens_d": effect_size,
            "raw_values_for_plot": {
                str(name): grouped.get_group(name).tolist() for name in grouped.groups
            },
        },
        "viz_spec": {
            "type": "boxplot",
            "x_label": group_by,
            "y_label": metric,
            "title": f"{metric} by {group_by}",
        },
        "assumptions": assumptions,
    }


# -----------------------------------------------------------------------------
# Internals
# -----------------------------------------------------------------------------
def _run_test(override: str, groups: list, names: list) -> Dict[str, Any]:
    """Pick a sensible statistical test based on group count and shape."""
    n_groups = len(groups)
    if n_groups == 2:
        if override == "t":
            t, p = stats.ttest_ind(groups[0], groups[1], equal_var=True)
            return {"name": "Student's t-test (equal variance)", "applicable": True,
                    "statistic": float(t), "p_value": float(p),
                    "dof": int(groups[0].size + groups[1].size - 2)}
        if override == "mannwhitney":
            u, p = stats.mannwhitneyu(groups[0], groups[1], alternative="two-sided")
            return {"name": "Mann-Whitney U", "applicable": True,
                    "statistic": float(u), "p_value": float(p)}
        # auto / welch — Welch's t is the safe default; doesn't assume equal variance.
        t, p = stats.ttest_ind(groups[0], groups[1], equal_var=False)
        return {"name": "Welch's t-test", "applicable": True,
                "statistic": float(t), "p_value": float(p)}

    # 3+ groups → one-way ANOVA. For non-normal data, Kruskal-Wallis is the
    # non-parametric equivalent; we surface it as an opt-in.
    if override == "kruskal":
        h, p = stats.kruskal(*groups)
        return {"name": "Kruskal-Wallis", "applicable": True,
                "statistic": float(h), "p_value": float(p)}
    f, p = stats.f_oneway(*groups)
    return {"name": "One-way ANOVA", "applicable": True,
            "statistic": float(f), "p_value": float(p),
            "groups": names}


def _cohens_d(a, b) -> float:
    """Pooled-standard-deviation Cohen's d for two independent groups."""
    na, nb = a.size, b.size
    if na < 2 or nb < 2:
        return float("nan")
    var_a = np.var(a, ddof=1)
    var_b = np.var(b, ddof=1)
    pooled_sd = math.sqrt(((na - 1) * var_a + (nb - 1) * var_b) / (na + nb - 2))
    if pooled_sd == 0:
        return 0.0
    return float((np.mean(a) - np.mean(b)) / pooled_sd)


def _classify_d(d: float) -> str:
    a = abs(d)
    if a < 0.2:
        return "negligible difference"
    if a < 0.5:
        return "small but measurable"
    if a < 0.8:
        return "moderate"
    return "large"


def _interpret_p(p: float) -> str:
    if p < 0.001:
        return "The difference is statistically very strong (p < 0.001)."
    if p < 0.01:
        return "The difference is statistically reliable (p < 0.01)."
    if p < 0.05:
        return "The difference is statistically reliable (p < 0.05)."
    if p < 0.1:
        return "The difference is borderline; not conventionally significant."
    return "The difference could plausibly be due to chance."
