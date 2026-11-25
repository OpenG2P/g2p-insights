"""Read-only connection to the REGISTRY database.

WHY A SECOND ENGINE
-------------------
Everything the Ask tab queries — fr_rpt_farmer, nsr_rpt_household and the rest —
lives in the registry's database. The app's own engine points at the Insight
database, which holds sessions and the audit log and none of the registry's data.

Planning worked and then fell over at the very last step: the model produced
correct SQL, the validator passed it, and EXPLAIN reported
`relation "fr_rpt_farmer" does not exist` — which reads as a catalog describing
tables that are not there, when in fact it was the right table on the wrong
connection.

The connection is deliberately SEPARATE rather than a search_path trick: it is a
different database on a possibly different host and owned by a different role.

Read-only is enforced on OUR side, by the SQL validator and the cost gate — a
single SELECT, no DDL, no writes. The credential the chart supplies today is the
registry's own owner role, so that guard is the only thing standing between a
generated statement and the register. Point this at a role granted SELECT on the
reporting views and nothing else where the deployment allows it.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..config import Settings

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)

_engine: Optional[AsyncEngine] = None


def registry_engine() -> Optional[AsyncEngine]:
    """The registry engine, created once on first use.

    Returns None when no registry database is configured, so callers can say so
    plainly instead of failing with a connection error to localhost — the
    default that made this hard to diagnose in the first place.
    """
    global _engine
    if _engine is not None:
        return _engine
    dsn = _config.registry_db_datasource
    if not dsn:
        return None
    # pool_pre_ping: this connection is idle between questions and sits behind
    # whatever the cluster does to long-lived TCP; a stale socket should cost a
    # reconnect, not somebody's query.
    _engine = create_async_engine(dsn, pool_pre_ping=True, pool_size=5, max_overflow=5)
    _logger.info("Registry database engine: %s", _config.registry_db_summary)
    return _engine
