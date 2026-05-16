"""
Classification heuristics for the insight-onboard tool.

Three layers:
  1. Name patterns (column-name keyword sets — loaded from YAML so deployments
     can extend without forking the code).
  2. Sample-value shapes (looks like a phone? a 12-digit ID? free-text?).
  3. Optional LLM classifier (called by caller when this module returns LOW
     confidence). LLM lives in llm_classifier.py.

Each classifier returns (classification, confidence, reason).

Rule source of truth: `default_pii_rules.yaml` bundled next to this module.
Operators pass `--pii-rules <file>` to override. The override may include a
subset of sections; missing sections fall back to the bundled defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import yaml

Classification = Literal["pii", "quasi", "safe", "skip", "unclear"]
Confidence = Literal["high", "medium", "low"]


# -----------------------------------------------------------------------------
# Rules config (loaded from YAML; see default_pii_rules.yaml)
# -----------------------------------------------------------------------------
@dataclass
class PiiRules:
    """Configurable classification rules. Built-in defaults live in the
    bundled `default_pii_rules.yaml`; `load_rules()` returns those when no
    override path is given."""

    pii_keywords: set                          # set[str]
    pii_multi_token: List[Tuple[set, str]]     # [(token-set, reason), …]
    quasi_keywords: set
    safe_keywords: set
    safe_multi_token: List[Tuple[set, str]]
    skip_table_patterns: List[Tuple[str, str]]  # [(regex, reason), …]


_DEFAULT_RULES_PATH = Path(__file__).parent / "default_pii_rules.yaml"


def _to_token_pairs(items) -> List[Tuple[set, str]]:
    """Coerce the YAML shape `[{tokens: [a, b], reason: '…'}, …]` into
    `[(set('a','b'), '…'), …]`."""
    out: List[Tuple[set, str]] = []
    for item in items or []:
        out.append((set(item.get("tokens") or []), item.get("reason") or ""))
    return out


def _to_pattern_pairs(items) -> List[Tuple[str, str]]:
    """Coerce `[{pattern: '…', reason: '…'}, …]` → `[(regex, reason), …]`."""
    out: List[Tuple[str, str]] = []
    for item in items or []:
        out.append((item.get("pattern") or "", item.get("reason") or ""))
    return out


def load_rules(path: Optional[str] = None) -> PiiRules:
    """Load classification rules. With `path=None`, returns the bundled
    defaults. With a path, the given YAML overlays the defaults — any
    section the operator omits falls through to the default."""
    defaults = yaml.safe_load(_DEFAULT_RULES_PATH.read_text()) or {}
    override: Dict = {}
    if path:
        override = yaml.safe_load(Path(path).read_text()) or {}

    def pick(key: str):
        return override.get(key, defaults.get(key))

    return PiiRules(
        pii_keywords=set(pick("pii_keywords") or []),
        pii_multi_token=_to_token_pairs(pick("pii_multi_token")),
        quasi_keywords=set(pick("quasi_keywords") or []),
        safe_keywords=set(pick("safe_keywords") or []),
        safe_multi_token=_to_token_pairs(pick("safe_multi_token")),
        skip_table_patterns=_to_pattern_pairs(pick("skip_table_patterns")),
    )


# Module-level singleton — populated lazily by `_default_rules()` so the
# YAML isn't read on import in tests that don't need it.
_RULES_SINGLETON: Optional[PiiRules] = None


def _default_rules() -> PiiRules:
    global _RULES_SINGLETON
    if _RULES_SINGLETON is None:
        _RULES_SINGLETON = load_rules()
    return _RULES_SINGLETON


def _tokenize(name: str) -> List[str]:
    """Split on any non-letter run — handles snake_case, camelCase, hyphens."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [t.lower() for t in re.split(r"[^a-zA-Z]+", s) if t]


# -----------------------------------------------------------------------------
# Table-name skip patterns
# -----------------------------------------------------------------------------
def classify_skip_table(
    table_name: str, rules: Optional[PiiRules] = None
) -> Optional[str]:
    """Return a skip reason if this table looks operational/metadata, else None."""
    rules = rules or _default_rules()
    for pat, reason in rules.skip_table_patterns:
        if pat and re.search(pat, table_name, re.IGNORECASE):
            return reason
    return None


# -----------------------------------------------------------------------------
# Sample-value shape detection — done LOCALLY, raw values never leave
# -----------------------------------------------------------------------------
_PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,15}\d$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ID_NUMERIC_RE = re.compile(r"^\d{9,16}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$",
                      re.IGNORECASE)


@dataclass
class ColumnStats:
    name: str
    table: str
    pg_type: str
    nullable: bool
    distinct_count: int
    null_rate: float
    sample_values: List[str]


def classify_by_name(
    col_name: str, rules: Optional[PiiRules] = None
) -> Tuple[Classification, Confidence, str]:
    """Classify by tokenising the column name and checking keyword/token-set
    membership. Order: safe-multi → PII keyword → PII multi → quasi → safe."""
    rules = rules or _default_rules()
    tokens = set(_tokenize(col_name))
    if not tokens:
        return "unclear", "low", "column name has no letters"

    # Safe multi-token combinations first — `age_bracket` would otherwise
    # hit the quasi `age` keyword and be misclassified.
    for required, label in rules.safe_multi_token:
        if required.issubset(tokens):
            return "safe", "high", f"safe pattern: {label}"

    # PII — single token.
    pii_hit = tokens & rules.pii_keywords
    if pii_hit:
        return "pii", "high", f"token `{sorted(pii_hit)[0]}` is a known PII keyword"

    # PII — multi-token.
    for required, label in rules.pii_multi_token:
        if required.issubset(tokens):
            return "pii", "high", f"PII pattern: {label}"

    # Quasi.
    quasi_hit = tokens & rules.quasi_keywords
    if quasi_hit:
        return "quasi", "high", f"token `{sorted(quasi_hit)[0]}` is a quasi-identifier"

    # Safe — single token.
    safe_hit = tokens & rules.safe_keywords
    if safe_hit:
        return "safe", "high", f"token `{sorted(safe_hit)[0]}` is a known safe keyword"

    return "unclear", "low", "no name token matched any keyword"


_NON_ANALYTICAL_TYPES = {"json", "jsonb", "bytea", "xml"}

# Type prefixes for date/timestamp columns. Postgres has several variants
# (`date`, `timestamp without time zone`, `timestamp with time zone`,
# `time`). All are analytical by nature — and crucially, their text shape
# (e.g. `2024-01-15`) collides with our phone-number regex, which would
# otherwise flag every `*_date` column as PII.
_TEMPORAL_TYPE_PREFIXES = ("date", "timestamp", "time")


def classify_by_type(pg_type: str) -> Optional[Tuple[Classification, Confidence, str]]:
    """Type-driven exclusion that beats both name and sample classifiers.

    JSON / JSONB / bytea blobs aren't usefully analytical and may carry
    arbitrary nested PII. Even when a column happens to be named
    `score_payload` (where `score` would otherwise hit `safe_keywords`),
    the underlying shape means we can't put it in a flat safe view
    without an explicit jsonb_path projection — a manual step, not
    something the heuristic should silently approve.

    Returns None when the type carries no verdict; the caller then runs
    the usual name+sample classifiers. Note we don't preempt date /
    timestamp columns here — `birth_date` still needs the name check to
    catch its PII signal. The temporal short-circuit lives in
    classify_by_sample so it only fires when name didn't decide first."""
    pg_l = pg_type.lower()
    if pg_l in _NON_ANALYTICAL_TYPES:
        return "pii", "high", (
            f"{pg_type} column — opaque blob, exclude from analytical view"
        )
    return None


def is_temporal_type(pg_type: str) -> bool:
    return any(pg_type.lower().startswith(p) for p in _TEMPORAL_TYPE_PREFIXES)


# -----------------------------------------------------------------------------
# Mixin-based priors (extension-led mode)
# -----------------------------------------------------------------------------
# When an OpenG2P registry-core mixin declares a column, we know quite a bit
# about its character even before looking at the column name:
#   * G2PPerson holds direct identity fields (first_name, foundational_id,
#     phone_numbers, emails, birth_date, …) — lean PII.
#   * G2PGeo holds geographic identifiers (lat/lng, plus_code, postal_code,
#     address lines, …) — lean PII/quasi.
#   * G2PRegister holds workflow + audit columns (created_by, last_approved_at,
#     record_status, …) plus a few register-level personal identifiers
#     (record_name, *_record_id, search_text, record_image_storage_id) —
#     mostly safe with name-driven exceptions.
#
# These are PRIORS, not overrides: a high-confidence name signal still wins
# (e.g. `registration_date` declared in G2PPerson is still safe via the
# temporal-type rule). Mixin priors only fire when name+type didn't decide.
_MIXIN_PRIORS = {
    "G2PPerson":   ("pii",   "extension prior: column declared in G2PPerson mixin (direct identity)"),
    "G2PGeo":     ("quasi", "extension prior: column declared in G2PGeo mixin (geographic — re-id risk)"),
    "G2PRegister": ("safe",  "extension prior: column declared in G2PRegister mixin (audit/workflow metadata)"),
}


def classify_by_mixin(mixin: Optional[str]) -> Optional[Tuple[Classification, Confidence, str]]:
    """Return a medium-confidence prior based on which extension mixin
    declared the column. Returns None for unknown/domain mixins — those
    fall through to the standard heuristic stack."""
    if not mixin:
        return None
    prior = _MIXIN_PRIORS.get(mixin)
    if prior is None:
        return None
    cls, reason = prior
    return cls, "medium", reason


# -----------------------------------------------------------------------------
# Register vs supporting-table classification (OpenG2P convention)
# -----------------------------------------------------------------------------
# Across OpenG2P registries, the schema follows a consistent shape:
#   * a **register** is the main entity table — one row per registered
#     individual or household. Examples: g2p_register_individuals,
#     g2p_register_households.
#   * a **table** is a supporting one-to-many child of a register —
#     `disabilities` per individual, `assets` per household, etc.
#     A satellite's name has the form g2p_register_<entity>_<aspect>.
#
# These are SUGGESTIONS the operator confirms during proposal review —
# the heuristic is intentionally simple and may be wrong for non-standard
# deployments. The operator's role/parent_register edits in the proposal
# YAML are authoritative.
_REGISTRY_PREFIX = "g2p_register_"


def suggest_table_role(table_name: str) -> str:
    """Return "register" or "table" based on token count after stripping
    the `g2p_register_` prefix.

      * `g2p_register_individuals`       → 1 segment  → register
      * `g2p_register_households`        → 1 segment  → register
      * `g2p_register_individual_disabilities` → 2+ segments → table
      * `g2p_register_household_assets`  → 2+ segments → table

    Tables outside the `g2p_register_*` naming convention default to
    "register" (the operator likely promoted them in the proposal)."""
    name = table_name.lower()
    if not name.startswith(_REGISTRY_PREFIX):
        return "register"
    tail = name[len(_REGISTRY_PREFIX):]
    return "register" if "_" not in tail else "table"


def suggest_parent_register(
    table_name: str, register_names: List[str]
) -> Optional[str]:
    """For a satellite table, pick the register whose name's tail is the
    longest prefix of the satellite's tail.

      * satellite `g2p_register_individual_disabilities` tail
        → "individual_disabilities"
      * candidates' tails: "individuals" (from g2p_register_individuals),
        "households" (from g2p_register_households)
      * "individuals" shares "individual" with the satellite (10 chars);
        "households" shares "" → individuals wins → parent
        = g2p_register_individuals.

    Returns None when no register name shares a non-trivial prefix —
    operator will fill in manually."""
    if suggest_table_role(table_name) == "register":
        return None
    name = table_name.lower()
    if not name.startswith(_REGISTRY_PREFIX):
        return None
    satellite_tail = name[len(_REGISTRY_PREFIX):]

    best_register: Optional[str] = None
    best_overlap = 0
    for reg in register_names:
        reg_l = reg.lower()
        if not reg_l.startswith(_REGISTRY_PREFIX):
            continue
        reg_tail = reg_l[len(_REGISTRY_PREFIX):]
        # Compare singular vs plural — "individual" prefix of "individuals".
        # Common prefix length on raw chars.
        common = 0
        for a, b in zip(reg_tail, satellite_tail):
            if a != b:
                break
            common += 1
        # Require at least 4 chars overlap to avoid false matches
        # (e.g. "h" matching "h*" everywhere). For OpenG2P names the
        # minimum meaningful entity name is `household` (9) or `individual` (10).
        if common >= 4 and common > best_overlap:
            best_overlap = common
            best_register = reg
    return best_register


def describe_table(
    table_name: str, role: str, parent_register: Optional[str]
) -> str:
    """Default human-readable description by role. Operator overrides in the
    proposal YAML; this is just the starting point.

    Three roles, per OpenG2P concepts:
      * `register` — standard entity register (Individual, Household, …).
        One row per registered entity. Assigns functional IDs.
      * `programme_register` — application register for a benefit programme,
        managed by PBMS. One row per application. Assigns functional IDs
        but has no version history.
      * `table` — supporting one-to-many child of a register. No functional
        ID. One row per `<aspect>` record."""
    name = table_name.lower()
    tail = name[len(_REGISTRY_PREFIX):] if name.startswith(_REGISTRY_PREFIX) else name

    if role == "register":
        # Singularise: drop trailing 's'. "individuals" → "individual",
        # "households" → "household". Good enough for the common case;
        # operator edits if they want different prose.
        entity = tail.rstrip("s") if tail.endswith("s") else tail
        return (
            f"Main {entity} register. One row per registered {entity}. "
            "Use this for entity-level counts."
        )

    if role == "programme_register":
        return (
            f"Programme application register. One row per application to "
            f"this programme (assigns its own functional ID, no version "
            f"history). Use for programme-level counts; references entity "
            f"registers via foundational_id."
        )

    # Satellite: figure out the aspect by stripping the entity-name prefix
    # from the satellite tail.
    aspect = tail
    if parent_register and parent_register.lower().startswith(_REGISTRY_PREFIX):
        parent_tail = parent_register.lower()[len(_REGISTRY_PREFIX):]
        common = 0
        for a, b in zip(parent_tail, tail):
            if a != b:
                break
            common += 1
        aspect = tail[common:].lstrip("_") or tail
    if parent_register:
        return (
            f"Supporting table for {parent_register} — one row per "
            f"{aspect.replace('_', ' ')} record. NOT one row per entity; "
            f"use {parent_register} for entity-level counts."
        )
    return "Supporting table — see operator notes."


def classify_by_sample(stats: ColumnStats) -> Tuple[Classification, Confidence, str]:
    """Use distinct count / null rate / value shapes to classify columns whose
    name didn't tell us anything."""
    # Date / timestamp / time columns are analytical scalars; short-circuit
    # before the shape heuristics, which would otherwise misread strings
    # like `2024-01-15` as phone numbers.
    if is_temporal_type(stats.pg_type):
        return "safe", "high", f"{stats.pg_type} — temporal scalar, analytical"

    n_samples = len(stats.sample_values)
    if n_samples == 0:
        # Empty columns can hold anything once data flows in. Don't default
        # to safe — force operator review. (If the name carries a high-
        # confidence verdict, combine_classifications already trusts it
        # and this fallback never gets used.)
        return "unclear", "low", "empty column — needs review"

    phone_like = sum(1 for v in stats.sample_values if _PHONE_RE.match(v))
    email_like = sum(1 for v in stats.sample_values if _EMAIL_RE.match(v))
    id_like = sum(1 for v in stats.sample_values if _ID_NUMERIC_RE.match(v))
    uuid_like = sum(1 for v in stats.sample_values if _UUID_RE.match(v))

    if phone_like / n_samples > 0.5:
        return "pii", "high", "majority of samples look like phone numbers"
    if email_like / n_samples > 0.5:
        return "pii", "high", "majority of samples look like email addresses"
    if id_like / n_samples > 0.7 and stats.distinct_count >= 100:
        return "pii", "medium", "long-numeric strings; likely a national-ID-style identifier"
    if uuid_like / n_samples > 0.5:
        return "pii", "medium", "UUIDs — likely internal identifiers; exclude from exports"

    if stats.distinct_count <= 12 and all(isinstance(v, str) for v in stats.sample_values):
        return "safe", "medium", f"low cardinality ({stats.distinct_count}) — likely an enum"

    avg_len = sum(len(v) for v in stats.sample_values) / n_samples
    if avg_len > 30 and stats.distinct_count > 50:
        return "pii", "medium", (
            f"average value length {avg_len:.0f} chars and {stats.distinct_count} distinct "
            "values — looks like free-text notes; lean pii"
        )

    if all(_is_numeric_string(v) for v in stats.sample_values):
        return "safe", "medium", "numeric values"

    return "unclear", "low", "sample values don't match any obvious pattern"


def _is_numeric_string(v: str) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def combine_classifications(
    name_class: Tuple[Classification, Confidence, str],
    sample_class: Tuple[Classification, Confidence, str],
) -> Tuple[Classification, Confidence, str]:
    """If the name pattern is high-confidence, trust it. Otherwise fall back
    to sample-shape classification. Disagreements resolve to the MORE
    restrictive (pii > quasi > safe) — bias toward false positive."""
    n_cls, n_conf, n_reason = name_class
    s_cls, s_conf, s_reason = sample_class

    if n_conf == "high":
        return n_cls, n_conf, n_reason
    if s_conf in ("high", "medium"):
        return s_cls, s_conf, s_reason
    order = {"pii": 3, "quasi": 2, "safe": 1, "unclear": 0, "skip": -1}
    if order[n_cls] >= order[s_cls]:
        return n_cls, "low", n_reason
    return s_cls, "low", s_reason
