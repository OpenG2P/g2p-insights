"""
Optional LLM-assisted classifier for columns that the local heuristics
couldn't classify with confidence.

The LLM ONLY receives column metadata + aggregated stats. Raw sample values
are summarised into shape descriptions (length range, contains-digits, etc.)
locally first, so individual values never leave the host. This preserves
the same PII guarantee as the main app.

Uses the same INSIGHT_LLM__* env vars as the backend. Opt-in via
--use-llm on the CLI.
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Tuple

from .heuristics import Classification, ColumnStats, Confidence

_LLM_SYSTEM = """You are classifying database columns for PII risk in a social-protection registry.

For each column you are given:
  - table name
  - column name
  - SQL type
  - cardinality (distinct value count)
  - null rate
  - value SHAPE summary (length range, contains digits/letters/special chars)
    — NEVER raw values

Classify the column into exactly one of:
  pii    — directly identifies a person (name, phone, exact ID, address)
  quasi  — contributes to re-identification when combined (age, gender,
           fine-grained geo, ethnicity)
  safe   — pure analytical with no re-identification risk (region codes,
           pre-aggregated scores, enums of 2-10 values)

Lean conservative — when in doubt, prefer pii > quasi > safe.
Return ONLY a JSON object: {"classification": "...", "confidence": "high"|"medium"|"low", "reason": "..."}
"""


def summarise_samples(samples: List[str]) -> str:
    """Reduce raw sample values to non-leaky shape descriptors."""
    if not samples:
        return "(no samples)"
    lengths = [len(s) for s in samples]
    has_digit = sum(1 for s in samples if any(c.isdigit() for c in s))
    has_alpha = sum(1 for s in samples if any(c.isalpha() for c in s))
    has_at = sum(1 for s in samples if "@" in s)
    has_space = sum(1 for s in samples if " " in s)
    return (
        f"{len(samples)} samples; "
        f"length min={min(lengths)} max={max(lengths)} mean={sum(lengths) // len(lengths)}; "
        f"contain digits: {has_digit}/{len(samples)}; "
        f"contain letters: {has_alpha}/{len(samples)}; "
        f"contain '@': {has_at}/{len(samples)}; "
        f"contain spaces: {has_space}/{len(samples)}"
    )


async def classify_via_llm(
    stats: ColumnStats,
) -> Tuple[Classification, Confidence, str]:
    """Call the configured LLM. Returns ('unclear', 'low', reason) on any
    failure — the caller falls back to its existing best guess."""
    try:
        return await _classify_via_llm_impl(stats)
    except Exception as exc:  # noqa: BLE001
        return "unclear", "low", f"LLM classification failed: {exc!s}"


# Kept in step with LlmConfig.model in openg2p_insight/config.py.
DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"


async def _classify_via_llm_impl(stats: ColumnStats):
    # OpenRouter only, matching LlmService. This script used to default to
    # provider="anthropic" and import the Anthropic SDK, which stopped being a
    # dependency when Insight moved to OpenRouter — so its default path raised
    # ImportError rather than classifying anything.
    api_key = os.environ.get("INSIGHT_LLM__API_KEY") or os.environ.get(
        "OPENROUTER_API_KEY", ""
    )
    if not api_key:
        return "unclear", "low", "no LLM API key configured"

    summary = summarise_samples(stats.sample_values)
    user_msg = (
        f"Table: {stats.table}\n"
        f"Column: {stats.name}\n"
        f"Type: {stats.pg_type}\n"
        f"Cardinality: {stats.distinct_count}\n"
        f"Null rate: {stats.null_rate:.2%}\n"
        f"Sample shapes: {summary}\n"
        f"\nReturn JSON only."
    )

    body = await _call_openai(user_msg, api_key)

    parsed = _extract_json(body)
    if not parsed:
        return "unclear", "low", "LLM returned non-JSON"
    cls = parsed.get("classification", "unclear")
    conf = parsed.get("confidence", "low")
    reason = parsed.get("reason") or "LLM classification"
    return cls, conf, f"LLM: {reason}"


async def _call_openai(user_msg: str, api_key: str) -> str:
    from openai import AsyncOpenAI
    base_url = os.environ.get("INSIGHT_LLM__BASE_URL", "https://openrouter.ai/api/v1")
    model = os.environ.get("INSIGHT_LLM__MODEL", DEFAULT_MODEL)
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    r = await client.chat.completions.create(
        model=model,
        max_tokens=200,
        messages=[
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
    )
    return r.choices[0].message.content or ""


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
