"""Proposal YAML I/O. The proposal is the human-reviewable artifact between
`scan` and `apply`."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional  # noqa: F401  (Optional used in dataclass annotation)

import yaml


@dataclass
class ProposalColumn:
    name: str
    pg_type: str
    pii: str                 # pii | quasi | safe | "?"
    keep: Any                # True | False | "?"
    confidence: str          # heuristic_name | sample | llm | review | extension
    reason: str = ""
    distinct_count: int = 0
    null_rate: float = 0.0
    # Filled in from the registry extension (`--registry-extension`) when
    # the column's `Mapped[…Enum]` annotation resolves to a known enum.
    # The runtime LLM prompt uses these as the authoritative allowed-value
    # set for enum-typed filter columns — far better than guessing from
    # sampled data.
    enum_values: Optional[List[str]] = None
    # Which extension mixin/class declared this column (e.g. "G2PPerson",
    # "G2PGeo", "G2PIndividual"). Useful for downstream tooling and as
    # context in the catalog, even though we don't currently drive PII
    # decisions off it (column-level heuristics are more reliable).
    mixin: Optional[str] = None


@dataclass
class ProposalTable:
    source: str
    safe_view_name: str
    # OpenG2P concept (per docs.openg2p.org/.../registry/concepts):
    #   * "register"           — main entity register (Individual, Household,
    #                             Farmer, …). One row per registered entity.
    #                             Assigns functional IDs; has version history.
    #   * "programme_register" — application register for a benefit programme,
    #                             managed by PBMS. One row per application.
    #                             Assigns its own functional ID, no history.
    #                             Auto-detected only when an extension model
    #                             extends G2PProgramRegister; otherwise the
    #                             operator marks it manually.
    #   * "table"              — supporting one-to-many child of a register
    #                             (disabilities, livelihoods, assets, …).
    #                             No functional ID; follows parent's history.
    #
    # The scan auto-suggests `register` vs `table` from the name pattern
    # (single-segment after `g2p_register_` → register; multi-segment →
    # table) and from extension mixin info when present. The operator
    # confirms / overrides during proposal review.
    role: str = "table"                     # "register" | "programme_register" | "table"
    # For satellites only: the safe_view_name of the parent register.
    # Auto-suggested via longest-common-name-prefix heuristic; operator
    # overrides if wrong.
    parent_register: Optional[str] = None
    # Human-readable description that flows into the operational catalog
    # and onwards into the LLM's schema view. Auto-generated as a starting
    # point; operator should refine for deployment-specific nuance (e.g.,
    # "Only registered since 2019 — older records are in the legacy view").
    description: Optional[str] = None
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
    # Where the safe layer lives vs. where the registry tables live. The
    # dedicated target schema keeps the analytical surface isolated and lets
    # us grant the AI role USAGE on `target_schema` without ever touching
    # the source schema. The role's `search_path` is set to `target_schema`
    # so unqualified LLM-emitted SQL resolves to the safe views.
    source_schema: str = "public"
    target_schema: str = "insight_safe"


def proposal_to_yaml(p: Proposal) -> str:
    """Hand-tuned YAML with comments to guide the reviewer."""
    parts: List[str] = []
    parts.append("# Insight onboarding proposal — review and edit.\n")
    parts.append("# Every `?` value MUST be resolved before `insight-onboard apply` will run.\n")
    parts.append("#\n")
    parts.append("# Column classification:\n")
    parts.append("#   pii    — directly identifies a person → keep: false\n")
    parts.append("#   quasi  — contributes to re-identification → keep: true (relies on k-anonymity)\n")
    parts.append("#   safe   — pure analytical → keep: true\n")
    parts.append("#\n")
    parts.append("# Table role (OpenG2P concepts — see docs.openg2p.org/.../registry/concepts):\n")
    parts.append("#   register           — main entity register (Individual, Household, …).\n")
    parts.append("#                        One row per registered entity. Use for entity-level counts.\n")
    parts.append("#   programme_register — application register for a benefit programme (PBMS).\n")
    parts.append("#                        Auto-detected only when extension class extends\n")
    parts.append("#                        G2PProgramRegister; otherwise mark manually.\n")
    parts.append("#   table              — supporting one-to-many child of a register\n")
    parts.append("#                        (disabilities, livelihoods, assets, …). Set\n")
    parts.append("#                        parent_register to the safe_view_name of its parent.\n")
    parts.append("#\n")
    parts.append("# Roles auto-suggested from table name + extension mixins. **Verify each one** —\n")
    parts.append("# the LLM uses `role` and `description` to decide which table answers an\n")
    parts.append("# entity-level question. A misclassified satellite tagged as `register` would\n")
    parts.append("# cause undercounts (e.g. counting disability records instead of individuals).\n")
    parts.append("\n")
    parts.append(yaml.safe_dump(
        {
            "metadata": p.metadata,
            "deployment": p.deployment,
            "source_schema": p.source_schema,
            "target_schema": p.target_schema,
            "skipped": p.skipped,
            "analytical_tables": [
                {
                    "source": t.source,
                    "safe_view_name": t.safe_view_name,
                    "role": t.role,
                    "parent_register": t.parent_register,
                    "description": t.description,
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
    parts.append("\n")
    parts.append(
        "# ---------------------------------------------------------------------\n"
        "# Aggregate materialized views — TODO, declarative grammar (not yet\n"
        "# emitted by the codegen). Hand-edit if you want a rollup MV today.\n"
        "# ---------------------------------------------------------------------\n"
        "# materialized_views:\n"
        "#   - name: mv_district_aggregates\n"
        "#     description: Pre-aggregated counts by region/zone/woreda × demographics.\n"
        "#     sources: [v_individuals_safe]\n"
        "#     dimensions: [region_code, zone_code, woreda_code, gender, age_bracket]\n"
        "#     metrics:\n"
        "#       - {expr: \"COUNT(*)\",                       alias: total}\n"
        "#       - {expr: \"AVG(pmt_score)::NUMERIC(6,2)\",    alias: avg_pmt}\n"
        "#       - {expr: \"AVG(monthly_income)::NUMERIC(12,2)\", alias: avg_income}\n"
        "#     indexes: [region_code]\n"
        "#     refresh: \"daily at 02:30 UTC\"\n"
    )
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
                role=t.get("role", "table"),
                parent_register=t.get("parent_register"),
                description=t.get("description"),
                columns=[ProposalColumn(**c) for c in t.get("columns", [])],
            )
            for t in data.get("analytical_tables", [])
        ],
        materialized_views=[
            ProposalMaterializedView(**mv) for mv in data.get("materialized_views", [])
        ],
        pii_columns_in_source=data.get("pii_columns_in_source", []),
        source_schema=data.get("source_schema", "public"),
        target_schema=data.get("target_schema", "insight_safe"),
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
        if t.role not in ("register", "programme_register", "table"):
            errors.append(
                f"Table {t.source} has unknown role={t.role!r}. "
                "Must be one of: register, programme_register, table."
            )
        if t.role == "table" and not t.parent_register:
            errors.append(
                f"Table {t.source} is role=table but has no parent_register. "
                "Set parent_register to the safe_view_name of its parent register."
            )
    if not p.deployment.get("country_code"):
        errors.append("deployment.country_code is empty — please fill in (e.g. 'ET').")
    return errors


def validate_for_apply_warnings(p: Proposal) -> List[str]:
    """Non-fatal warnings the apply step prints alongside errors. Returning
    something here doesn't block `apply`, but a careful operator should
    address them. Typical case: heuristic guessed too many `register`
    tables and the operator forgot to demote some to `table`."""
    warnings: List[str] = []
    n_reg = sum(1 for t in p.analytical_tables if t.role == "register")
    n_prog = sum(1 for t in p.analytical_tables if t.role == "programme_register")
    if n_reg == 0 and n_prog == 0:
        warnings.append(
            "No tables marked role=register or role=programme_register. "
            "Entity-level questions ('how many individuals') have no clean "
            "answer. Confirm at least one register exists in this registry."
        )
    if n_reg > 3:
        warnings.append(
            f"{n_reg} tables marked role=register. OpenG2P deployments "
            f"typically have 1–2 (individuals + households). Double-check "
            "the proposal — heuristic likely over-promoted satellites."
        )
    return warnings
