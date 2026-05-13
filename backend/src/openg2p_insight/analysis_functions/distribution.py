"""Distribution analysis — histogram + percentiles + distribution fits."""

from __future__ import annotations

import numpy as np
from scipy import stats

from ..schemas.analytics import AnalyticsRequest
from ._extract import extract


async def distribution(req: AnalyticsRequest) -> dict:
    """Histogram + percentiles + normal/lognormal fit for a numeric column.

    Params:
      bins:   int, default 50
      fits:   list[str], subset of ["normal", "lognormal"]; default both
    """
    if not req.column:
        return {"result": {}, "viz_spec": {}, "assumptions": ["error: column required"]}

    bins = int(req.params.get("bins", 50))
    fits = req.params.get("fits", ["normal", "lognormal"])

    df = await extract(req.entity, [req.column], req.filters, req.sample_size)
    if df.empty:
        return {"result": {}, "viz_spec": {}, "assumptions": ["No rows matched filters"]}

    values = df[req.column].dropna().to_numpy(dtype=float)
    counts, edges = np.histogram(values, bins=bins)
    percentiles = {f"p{p}": float(np.percentile(values, p)) for p in (10, 25, 50, 75, 90)}

    fit_results: dict = {}
    if "normal" in fits:
        mu, sigma = stats.norm.fit(values)
        fit_results["normal"] = {"mu": float(mu), "sigma": float(sigma)}
    if "lognormal" in fits and (values > 0).all():
        shape, loc, scale = stats.lognorm.fit(values, floc=0)
        fit_results["lognormal"] = {"shape": float(shape), "scale": float(scale)}

    return {
        "result": {
            "histogram": {"counts": counts.tolist(), "edges": edges.tolist()},
            "percentiles": percentiles,
            "fits": fit_results,
            "n": int(values.size),
            "mean": float(values.mean()),
            "std": float(values.std()),
        },
        "viz_spec": {
            "type": "histogram",
            "x_label": req.column,
            "y_label": "frequency",
            "threshold_slider": True,
        },
        "assumptions": [
            f"Sample size: {values.size}",
            f"Histogram bins: {bins}",
        ],
    }
