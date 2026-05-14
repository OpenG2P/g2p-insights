"""
Classification heuristics for the insight-onboard tool.

Three layers:
  1. Name patterns (column name regexes)
  2. Sample-value shapes (looks like a phone? a 12-digit ID? free-text?)
  3. Optional LLM classifier (called by caller when this module returns LOW
     confidence). LLM lives in llm_classifier.py.

Each classifier returns (classification, confidence, reason).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple

Classification = Literal["pii", "quasi", "safe", "skip", "unclear"]
Confidence = Literal["high", "medium", "low"]


# -----------------------------------------------------------------------------
# Column-name keyword sets
#
# We tokenise the column name on non-letter boundaries (incl. `_`) and check
# token membership in keyword sets. We don't use `\b...\b` regexes because
# Python regex treats `_` as a word char — `\bgps\b` would NOT match inside
# `gps_latitude`, silently dropping GPS-style PII.
# -----------------------------------------------------------------------------
_PII_KEYWORDS = {
    # direct identifiers
    "name", "fullname", "firstname", "lastname", "middlename", "maidenname", "surname",
    "phone", "mobile", "telephone", "whatsapp",
    "email", "mail",
    "address", "street",
    "gps", "lat", "lng", "latitude", "longitude", "coords", "geolocation",
    # national IDs
    "fayda", "aadhaar", "aadhar", "ssn", "passport", "nrc", "nid", "cnic",
    # birth date
    "dob", "birthdate",
    # banking
    "iban", "mpesa",
    # registry-internal personal identifiers
    "rid",
    # auth / credentials
    "password", "secret",
}
# Multi-token combinations (any token from BOTH sets must be present in the
# column name). Useful for cases like `national_id` where neither token alone
# is PII (`national` could be a flag; `id` is generic).
_PII_MULTI_TOKEN = [
    ({"date", "birth"}, "date-of-birth pair"),
    ({"national", "id"}, "national_id pair"),
    ({"functional", "id"}, "functional_id (internal personal identifier)"),
    ({"registry", "id"}, "registry_id (internal personal identifier)"),
    ({"contact", "person"}, "contact-person name"),
    ({"contact", "number"}, "contact-number/phone"),
    ({"account", "number"}, "bank-account number"),
    ({"exact", "age"}, "exact age (high re-id risk)"),
    ({"api", "key"}, "credentials"),
]

_QUASI_KEYWORDS = {
    "age",                                       # exact age (generalised age handled in multi-token)
    "gender", "sex",
    "marital", "marriage",
    "ethnicity", "caste", "race", "religion", "language",
    "disability", "orphanhood", "chronic", "plw",
    "displacement", "refugee", "idp",
    "kebele", "village",
    "occupation", "employment",
}

_SAFE_KEYWORDS = {
    "region", "zone", "woreda", "district", "province", "state",
    "pmt", "income",
    "livelihood", "livestock", "crop",
    "headship",
    "verified", "status", "level", "severity",
    "created", "updated", "registered", "enrolled",
    "count", "total", "sum", "avg", "min", "max", "score",
}
_SAFE_MULTI_TOKEN = [
    ({"age", "bracket"}, "generalised age bracket"),
    ({"age", "group"},   "generalised age group"),
    ({"pmt", "score"},   "PMT score"),
    ({"monthly", "income"}, "monthly income"),
    ({"household", "size"}, "household-size aggregate"),
    ({"headship", "type"},  "headship type"),
]


def _tokenize(name: str) -> List[str]:
    """Split on any non-letter run — handles snake_case, camelCase, hyphens."""
    # Normalise camelCase first: insert space between lowercase-uppercase
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [t.lower() for t in re.split(r"[^a-zA-Z]+", s) if t]



# -----------------------------------------------------------------------------
# Table-name skip patterns (operational / metadata tables — not analytical)
# -----------------------------------------------------------------------------
_SKIP_TABLE_PATTERNS: List[Tuple[str, str]] = [
    (r"^g2p_intake_form_",
        "raw intake submissions — pre-validation, not the analytical entity"),
    (r"^dedup(?:_|lication_)",
        "deduplication tracking — operational, no analytical value"),
    (r"(?:_|^)queue$",
        "background-job queue table"),
    (r"^g2p_attributes?$|^g2p_attribute_values$",
        "schema metadata — no per-person data"),
    (r"^g2p_input_mechanisms?$",
        "input-channel registry — operational"),
    (r"^g2p_intake_form_definitions$",
        "form-definition metadata"),
    (r"^data_models?$",
        "schema definitions — metadata"),
    (r"^alembic_version$",
        "Alembic migration state"),
    (r"_history$",
        "history table — exact mirror of base, raises PII surface unnecessarily"),
    (r"_log$|_logs$|_audit$",
        "audit/log table — operational"),
]


def classify_skip_table(table_name: str) -> Optional[str]:
    """Return a skip reason if this table looks operational/metadata, else None."""
    for pat, reason in _SKIP_TABLE_PATTERNS:
        if re.search(pat, table_name, re.IGNORECASE):
            return reason
    return None


# -----------------------------------------------------------------------------
# Sample-value shape detection — done LOCALLY, raw values never leave
# -----------------------------------------------------------------------------
_PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,15}\d$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ID_NUMERIC_RE = re.compile(r"^\d{9,16}$")          # national-ID-shaped numeric strings
_UUID_RE = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$",
                      re.IGNORECASE)


@dataclass
class ColumnStats:
    name: str
    table: str
    pg_type: str
    nullable: bool
    distinct_count: int        # approximate is fine
    null_rate: float           # 0..1
    sample_values: List[str]   # local-only; never sent to LLM as raw


def classify_by_name(col_name: str) -> Tuple[Classification, Confidence, str]:
    """Classify by tokenising the column name and checking keyword/token-set
    membership. Order: safe-multi → PII keyword → PII multi → quasi → safe."""
    tokens = set(_tokenize(col_name))
    if not tokens:
        return "unclear", "low", "column name has no letters"

    # Safe multi-token combinations first — `age_bracket` would otherwise hit
    # the quasi `age` keyword and be misclassified.
    for required, label in _SAFE_MULTI_TOKEN:
        if required.issubset(tokens):
            return "safe", "high", f"safe pattern: {label}"

    # PII — single token.
    pii_hit = tokens & _PII_KEYWORDS
    if pii_hit:
        return "pii", "high", f"token `{sorted(pii_hit)[0]}` is a known PII keyword"

    # PII — multi-token.
    for required, label in _PII_MULTI_TOKEN:
        if required.issubset(tokens):
            return "pii", "high", f"PII pattern: {label}"

    # Quasi.
    quasi_hit = tokens & _QUASI_KEYWORDS
    if quasi_hit:
        return "quasi", "high", f"token `{sorted(quasi_hit)[0]}` is a quasi-identifier"

    # Safe — single token.
    safe_hit = tokens & _SAFE_KEYWORDS
    if safe_hit:
        return "safe", "high", f"token `{sorted(safe_hit)[0]}` is a known safe keyword"

    return "unclear", "low", "no name token matched any keyword"


def classify_by_sample(stats: ColumnStats) -> Tuple[Classification, Confidence, str]:
    """Use distinct count / null rate / value shapes to classify columns whose
    name didn't tell us anything. Examples:
      - cardinality 2-12 + text → likely safe enum (gender-like)
      - phone-like sample shape → pii
      - free-text > 30 chars → suspect, lean pii
    """
    n_samples = len(stats.sample_values)
    if n_samples == 0:
        # Empty column — no data leaked or anything risky; mark safe but flag
        return "safe", "low", "empty column"

    # Check sample shapes
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

    # Cardinality-based heuristics
    if stats.distinct_count <= 12 and all(isinstance(v, str) for v in stats.sample_values):
        return "safe", "medium", f"low cardinality ({stats.distinct_count}) — likely an enum"

    # Long free text — high re-id risk regardless of name
    avg_len = sum(len(v) for v in stats.sample_values) / n_samples
    if avg_len > 30 and stats.distinct_count > 50:
        return "pii", "medium", (
            f"average value length {avg_len:.0f} chars and {stats.distinct_count} distinct "
            "values — looks like free-text notes; lean pii"
        )

    # Pure numeric metric → likely safe
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
    # Both low. Pick the more restrictive.
    order = {"pii": 3, "quasi": 2, "safe": 1, "unclear": 0, "skip": -1}
    if order[n_cls] >= order[s_cls]:
        return n_cls, "low", n_reason
    return s_cls, "low", s_reason
