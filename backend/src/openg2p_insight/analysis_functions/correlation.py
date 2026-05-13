"""Correlation matrix — Spearman by default, with optional clustering."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..schemas.analytics import AnalyticsRequest
from ._extract import extract


async def correlation(req: AnalyticsRequest) -> dict:
    """Compute a correlation matrix over the requested columns.

    Params:
      method: 'pearson' | 'spearman' | 'kendall'  (default spearman)
      cluster: bool; if True, reorder rows/cols to visually group related vars
    """
    if not req.columns:
        return {"result": {}, "viz_spec": {}, "assumptions": ["error: columns required"]}

    method = req.params.get("method", "spearman")
    df = await extract(req.entity, req.columns, req.filters, req.sample_size)
    if df.empty:
        return {"result": {}, "viz_spec": {}, "assumptions": ["No rows matched filters"]}

    df_enc = pd.get_dummies(df, drop_first=True)
    corr = df_enc.corr(method=method)

    if req.params.get("cluster"):
        try:
            from scipy.cluster import hierarchy

            linkage = hierarchy.linkage(np.nan_to_num(corr.values), method="average")
            order = hierarchy.leaves_list(linkage)
            corr = corr.iloc[order, order]
        except Exception:
            pass

    return {
        "result": {"matrix": corr.to_dict(), "labels": list(corr.columns)},
        "viz_spec": {"type": "heatmap", "annotate": True, "title": f"{method.title()} correlation"},
        "assumptions": [
            f"Method: {method}",
            f"Sample size: {len(df)}",
            "Categorical columns one-hot encoded (drop_first=True)",
        ],
    }
