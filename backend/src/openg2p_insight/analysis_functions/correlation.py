"""Correlation matrix — Spearman by default, with safety guards."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..config import Settings
from ..schemas.analytics import AnalyticsRequest
from ._extract import extract

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)

# Guards — keep correlation snappy by refusing to one-hot-encode high-cardinality
# columns into hundreds of dummy features. Such columns are typically geo codes
# (woreda, kebele) which the user didn't really mean to correlate.
_MAX_CATEGORICAL_CARDINALITY = 30
_DEFAULT_SAMPLE_SIZE = 5_000
_MAX_COLUMNS_FOR_CLUSTERING = 50


async def correlation(req: AnalyticsRequest) -> dict:
    """Compute a correlation matrix over the requested columns.

    Params:
      method:  'pearson' | 'spearman' | 'kendall'  (default spearman)
      cluster: bool — reorder rows/cols by hierarchical clustering. Off by
               default for safety; only enabled when explicitly requested AND
               the resulting matrix is under {_MAX_COLUMNS_FOR_CLUSTERING}.
    """
    if not req.columns:
        return {"result": {}, "viz_spec": {}, "assumptions": ["error: columns required"]}

    method = req.params.get("method", "spearman")
    do_cluster = bool(req.params.get("cluster", False))
    sample_size = req.sample_size or _DEFAULT_SAMPLE_SIZE
    assumptions: list[str] = []

    _logger.info(
        "[correlation] start  entity=%s cols=%s method=%s sample=%d",
        req.entity, req.columns, method, sample_size,
    )

    df = await extract(req.entity, req.columns, req.filters, sample_size)
    if df.empty:
        return {"result": {}, "viz_spec": {}, "assumptions": ["No rows matched filters"]}
    _logger.info("[correlation] extracted shape=%s", df.shape)
    assumptions.append(f"Sample size: {len(df)} (capped at {sample_size} for responsiveness)")

    # Drop high-cardinality categorical columns to keep get_dummies tractable.
    keep_cols: list[str] = []
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]):
            keep_cols.append(col)
            continue
        nunique = df[col].nunique(dropna=True)
        if nunique > _MAX_CATEGORICAL_CARDINALITY:
            assumptions.append(
                f"Skipped `{col}` — {nunique} distinct values, too many to encode meaningfully "
                f"(threshold {_MAX_CATEGORICAL_CARDINALITY}). Use group_compare or crosstab for it instead."
            )
            continue
        keep_cols.append(col)

    if len(keep_cols) < 2:
        return {
            "result": {},
            "viz_spec": {},
            "assumptions": assumptions + [
                "Fewer than 2 columns left after cardinality filtering — nothing to correlate."
            ],
        }

    df = df[keep_cols]
    # drop_first=False — keep ALL categories visible. The "drop first to avoid
    # multicollinearity" trick is for regression; a correlation matrix is
    # descriptive, and dropping a reference category silently hides whether
    # that category correlates with anything. Users expect to see all of
    # `gender_F`, `gender_M`, `gender_O` — not just two of them.
    df_enc = pd.get_dummies(df, drop_first=False)
    _logger.info("[correlation] encoded shape=%s", df_enc.shape)
    if df_enc.shape[1] > 200:
        # Defense in depth — even with the cardinality cap, multiple low-card
        # columns can compound. 200 features × 200 features Spearman is fine
        # (~10s), but clustering on it isn't.
        assumptions.append(
            f"{df_enc.shape[1]} encoded features after one-hot — clustering disabled to keep responsive."
        )
        do_cluster = False

    corr = df_enc.corr(method=method)
    _logger.info("[correlation] correlation matrix shape=%s", corr.shape)

    # Surface diagnostics the user would otherwise miss when the heatmap
    # silently coalesces NaN → 0 and tiny correlations look blank.
    off_diag = corr.where(~np.eye(corr.shape[0], dtype=bool))
    nan_cells = int(off_diag.isna().sum().sum())
    max_abs = float(off_diag.abs().max().max()) if not off_diag.empty else 0.0
    if nan_cells:
        assumptions.append(
            f"{nan_cells} off-diagonal cells are NaN (insufficient overlap, "
            "typically when a numeric column has many NULLs). Heatmap renders "
            "these as 0."
        )
    if max_abs < 0.1:
        assumptions.append(
            f"Strongest off-diagonal correlation is |{max_abs:.3f}| — effectively no "
            "linear relationship. For categorical-vs-numeric questions like "
            "'does gender relate to income', `group_compare` is a better tool."
        )

    if do_cluster and corr.shape[0] <= _MAX_COLUMNS_FOR_CLUSTERING:
        try:
            from scipy.cluster import hierarchy
            linkage = hierarchy.linkage(np.nan_to_num(corr.values), method="average")
            order = hierarchy.leaves_list(linkage)
            corr = corr.iloc[order, order]
            assumptions.append("Rows/columns reordered by hierarchical clustering.")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("[correlation] clustering failed: %s", exc)
            assumptions.append(f"Clustering attempted but failed: {exc!s}")

    assumptions.insert(0, f"Method: {method}")
    assumptions.append(
        "Categorical columns one-hot encoded (all categories shown — drop_first=False). "
        "Note: dummies of the same column are deterministically related to one another; "
        "what matters is each dummy's correlation with the OTHER columns."
    )

    return {
        "result": {"matrix": corr.to_dict(), "labels": list(corr.columns)},
        "viz_spec": {"type": "heatmap", "annotate": True, "title": f"{method.title()} correlation"},
        "assumptions": assumptions,
    }
