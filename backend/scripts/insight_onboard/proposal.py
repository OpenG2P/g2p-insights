"""Proposal YAML I/O. The proposal is the human-reviewable artifact between
`scan` and `apply`."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class ProposalColumn:
    name: str
    pg_type: str
    pii: str                 # pii | quasi | safe | "?"
    keep: Any                # True | False | "?"
    confidence: str          # heuristic_name | sample | llm | review
    reason: str = ""
    distinct_count: int = 0
    null_rate: float = 0.0


@dataclass
class ProposalTable:
    source: str
    safe_view_name: str
    columns: List[ProposalColumn] = field(default_factory=list)


@dataclass
class ProposalMaterializedView:
    name: str
    description: str
    sources: List[str]
    join: str = ""               # e.g. "v_individuals_safe AS i LEFT JOIN g2p_register_households h ON h.id = i.household_id"
    indexes: List[str] = field(default_factory=list)
    refresh: str = "manually via REFRESH MATERIALIZED VIEW"


@dataclass
class Proposal:
    metadata: Dict[str, Any]
    deployment: Dict[str, Any]
    skipped: List[Dict[str, str]]
    analytical_tables: List[ProposalTable]
    materialized_views: List[ProposalMaterializedView]
    pii_columns_in_source: List[Dict[str, str]] = field(default_factory=list)


def proposal_to_yaml(p: Proposal) -> str:
    """Hand-tuned YAML with comments to guide the reviewer."""
    parts: List[str] = []
    parts.append("# Insight onboarding proposal — review and edit.\n")
    parts.append("# Every `?` value MUST be resolved before `insight-onboard apply` will run.\n")
    parts.append("#\n")
    parts.append("# Classification meanings:\n")
    parts.append("#   pii    — directly identifies a person → keep: false\n")
    parts.append("#   quasi  — contributes to re-identification → keep: true (relies on k-anonymity)\n")
    parts.append("#   safe   — pure analytical → keep: true\n")
    parts.append("\n")
    parts.append(yaml.safe_dump(
        {
            "metadata": p.metadata,
            "deployment": p.deployment,
            "skipped": p.skipped,
            "analytical_tables": [
                {
                    "source": t.source,
                    "safe_view_name": t.safe_view_name,
                    "columns": [asdict(c) for c in t.columns],
                }
                for t in p.analytical_tables
            ],
            "materialized_views": [asdict(mv) for mv in p.materialized_views],
            "pii_columns_in_source": p.pii_columns_in_source,
        },
        sort_keys=False,
        allow_unicode=True,
    ))
    return "".join(parts)


def load_proposal(path: str) -> Proposal:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return Proposal(
        metadata=data.get("metadata", {}),
        deployment=data.get("deployment", {}),
        skipped=data.get("skipped", []),
        analytical_tables=[
            ProposalTable(
                source=t["source"],
                safe_view_name=t["safe_view_name"],
                columns=[ProposalColumn(**c) for c in t.get("columns", [])],
            )
            for t in data.get("analytical_tables", [])
        ],
        materialized_views=[
            ProposalMaterializedView(**mv) for mv in data.get("materialized_views", [])
        ],
        pii_columns_in_source=data.get("pii_columns_in_source", []),
    )


def validate_for_apply(p: Proposal) -> List[str]:
    """Return list of errors. Empty list means proposal is ready to apply."""
    errors: List[str] = []
    for t in p.analytical_tables:
        for c in t.columns:
            if c.pii == "?" or c.keep == "?":
                errors.append(
                    f"Unresolved column {t.source}.{c.name}: pii={c.pii} keep={c.keep}. "
                    "Please review and set explicit values."
                )
    if not p.deployment.get("country_code"):
        errors.append("deployment.country_code is empty — please fill in (e.g. 'ET').")
    return errors
