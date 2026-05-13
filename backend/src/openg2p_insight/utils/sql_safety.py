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
    if "WHERE" not in upper:
        issues.append(StaticIssue("Query has no WHERE clause — may scan whole table"))

    return issues
