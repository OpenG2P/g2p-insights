"""
Static analysis on the planned SQL before EXPLAIN.

Returns a list of StaticIssue; any issue with `blocker=True` makes the
CostGate classify the query as RED immediately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DML, Keyword

_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "GRANT", "REVOKE", "COPY", "VACUUM", "ANALYZE", "EXECUTE",
}
_MAX_JOINS = 5


@dataclass
class StaticIssue:
    message: str
    blocker: bool = False


# Aggregated output: one row per group rather than one row per record. LIMIT
# counts too — it bounds the result just as effectively.
_AGGREGATED = re.compile(
    r"\bGROUP\s+BY\b|\bLIMIT\b|"
    r"\b(?:COUNT|SUM|AVG|MIN|MAX|STDDEV|VARIANCE|PERCENTILE_CONT|PERCENTILE_DISC)\s*\("
)


def static_analyze(sql: str) -> List[StaticIssue]:
    issues: List[StaticIssue] = []
    parsed = sqlparse.parse(sql)
    if not parsed:
        return [StaticIssue("Could not parse SQL", blocker=True)]

    if len(parsed) > 1:
        issues.append(StaticIssue("Multiple SQL statements not allowed", blocker=True))

    stmt: Statement = parsed[0]
    first_dml = next((t for t in stmt.tokens if t.ttype is DML), None)
    if first_dml is None or first_dml.value.upper() != "SELECT":
        issues.append(StaticIssue("Only SELECT statements are permitted", blocker=True))

    upper = sql.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            issues.append(StaticIssue(f"Forbidden keyword: {kw}", blocker=True))

    if re.search(r"\bSELECT\s+\*", upper):
        issues.append(StaticIssue("SELECT * not allowed — list columns explicitly", blocker=True))

    if "CROSS JOIN" in upper:
        issues.append(StaticIssue("CROSS JOIN not allowed", blocker=True))

    join_count = len(re.findall(r"\bJOIN\b", upper))
    if join_count > _MAX_JOINS:
        issues.append(StaticIssue(f"Too many JOINs ({join_count}) — limit is {_MAX_JOINS}", blocker=True))

    # Soft warnings (non-blocking)
    #
    # A missing WHERE only matters when the query returns RAW ROWS: that is the
    # case where the whole table comes back. An aggregate or a GROUP BY returns
    # one row per group however much it reads, and its result size is already
    # bounded by the cost gate's row estimate.
    #
    # Without this distinction the warning fired on "how many farmers are there
    # in total" — a question whose entire point is to cover the register, and
    # which cannot be written any other way. A caveat attached to the correct
    # answer to a simple question just makes the tool look unsure of itself.
    if "WHERE" not in upper and not _AGGREGATED.search(upper):
        issues.append(StaticIssue(
            "Query has no WHERE clause and returns raw rows — may return the "
            "whole table"))

    return issues
