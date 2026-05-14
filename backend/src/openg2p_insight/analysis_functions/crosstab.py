"""Cross-tabulation with chi-square significance."""

from __future__ import annotations

import pandas as pd
from scipy import stats

from ..schemas.analytics import AnalyticsRequest
from ._extract import extract


async def crosstab(req: AnalyticsRequest) -> dict:
    """2-D contingency table over two categorical columns + chi-square.

    Required params: row (col name), col (col name).
    """
    row = req.params.get("row")
    col = req.params.get("col")
    if not row or not col:
        return {"result": {}, "viz_spec": {}, "assumptions": ["error: row & col params required"]}

    sample_size = req.sample_size or 50_000
    df = await extract(req.entity, [row, col], req.filters, sample_size)
    if df.empty:
        return {"result": {}, "viz_spec": {}, "assumptions": ["No rows matched filters"]}

    ct = pd.crosstab(df[row], df[col])
    chi2, p_value, dof, _ = stats.chi2_contingency(ct)

    return {
        "result": {
            "table": ct.to_dict(),
            "chi2": float(chi2),
            "p_value": float(p_value),
            "dof": int(dof),
        },
        "viz_spec": {"type": "heatmap", "x_label": col, "y_label": row, "annotate": True},
        "assumptions": [
            f"Rows: {len(df)}",
            f"Chi-square dof={dof}, p={p_value:.4g}",
        ],
    }
