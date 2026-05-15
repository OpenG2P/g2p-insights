"""
k-anonymity post-processing.

For grouped results, any group whose underlying row count is < k is suppressed:
all group-by column values are replaced with `<suppressed>` and any aggregated
metric is replaced with the sentinel `<k`. This protects against re-identification
even for metrics like AVG/SUM that don't themselves expose the group size.

The SQL planner always projects `_k_count = COUNT(*)` for grouped queries; this
function uses that column. The executor strips `_k_count` from the response
after this function runs so the user never sees the internal name.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

# Keep in sync with sql_validator_service.K_COUNT_COL.
K_COUNT_COL = "_k_count"


def apply_k_anonymity(
    rows: List[dict], group_by: Iterable[str], k: int
) -> Tuple[List[dict], int]:
    """Return (rows_after_suppression, suppression_count).

    If the planner did NOT include `_k_count` (i.e. an ungrouped query), we
    return rows unchanged — k-anonymity isn't meaningful for a single aggregate.
    """
    if not rows or not group_by or k <= 1:
        return rows, 0

    group_by = list(group_by)
    has_k_count = K_COUNT_COL in rows[0]
    if not has_k_count:
        # Defensive: if the planner failed to add the count column, fail closed
        # by suppressing every row. The caller should treat zero rows as an
        # indication that the query needs to be rewritten with COUNT().
        return [], len(rows)

    out: List[dict] = []
    suppressed = 0
    for r in rows:
        try:
            cnt = int(r[K_COUNT_COL])
        except (KeyError, TypeError, ValueError):
            # Treat unparseable count as zero (suppress).
            cnt = 0

        if cnt < k:
            redacted = dict(r)
            for g in group_by:
                if g in redacted:
                    redacted[g] = "<suppressed>"
            # Replace every non-group, non-count value with the sentinel so we
            # don't leak metric values either.
            for key in list(redacted):
                if key in group_by or key == K_COUNT_COL:
                    continue
                redacted[key] = f"<{k}"
            out.append(redacted)
            suppressed += 1
        else:
            out.append(r)
    return out, suppressed
