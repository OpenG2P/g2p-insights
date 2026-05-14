"""
Registry of expert analysis functions.

Each function takes an AnalyticsRequest and returns a dict with keys:
  - result      (free-form, function-specific)
  - viz_spec    (Plotly-compatible dict)
  - assumptions (list[str])
"""

from .correlation import correlation
from .crosstab import crosstab
from .distribution import distribution
from .group_compare import group_compare

ANALYSIS_REGISTRY = {
    "distribution": distribution,
    "correlation": correlation,
    "crosstab": crosstab,
    "group_compare": group_compare,
}
