"""Distribution analysis — histogram + percentiles + distribution fits."""

from __future__ import annotations

import numpy as np
import pandas as pd
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
    sample_size = req.sample_size or 50_000

    df = await extract(req.entity, [req.column], req.filters, sample_size)
    if df.empty:
        return {"result": {}, "viz_spec": {}, "assumptions": ["No rows matched filters"]}

    # A histogram, percentiles and a lognormal fit are all numeric ideas. Asked
    # for a category the conversion raises, and the user sees "could not convert
    # string to float: 'FEMALE'" — a pandas message about their data, for what is
    # really a mismatch between the column and the function. The column picker
    # offers every column in the view, so this is a click away, not an edge case.
    series = df[req.column].dropna()
    numeric = pd.to_numeric(series, errors="coerce")
    if len(series) and numeric.isna().all():
        return {"result": {}, "viz_spec": {}, "assumptions": [
            f"error: `{req.column}` is not numeric, and a distribution needs "
            f"numbers to bin. For a categorical column use crosstab, or "
            f"group_compare to compare a numeric metric across its values."]}
    values = numeric.dropna().to_numpy(dtype=float)
    if values.size == 0:
        return {"result": {}, "viz_spec": {}, "assumptions": [
            f"No numeric values in `{req.column}`"]}
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
