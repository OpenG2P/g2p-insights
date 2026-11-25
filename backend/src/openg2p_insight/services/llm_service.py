"""
LLM orchestrator with tool-use + clarification support.

Insight talks to a single provider — **OpenRouter** — via its OpenAI-compatible
chat-completions API. One key, one endpoint, and any model OpenRouter offers
(`anthropic/claude-sonnet-4.5`, `openai/gpt-4o`, `qwen/...`, `meta-llama/...`).
No per-vendor SDKs or keys.

Configure with `INSIGHT_LLM__MODEL` and `INSIGHT_LLM__API_KEY` (or the
`OPENROUTER_API_KEY` environment variable). `INSIGHT_LLM__ENABLED=false`
switches AI off entirely.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Literal, Optional, Union

from openg2p_fastapi_common.service import BaseService
from pydantic import BaseModel, Field

from ..config import Settings
from ..prompts.nl_to_sql import (
    CLARIFY_TOOL_SCHEMA,
    DIRECT_ANSWER_TOOL_SCHEMA,
    EXPERT_REDIRECT_TOOL_SCHEMA,
    NL_TO_SQL_SYSTEM,
    PROPOSE_SQL_TOOL_SCHEMA,
)
from .schema_service import TableSpec

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)



def _log_llm_usage(response, table_count: int) -> None:
    """Record what each question cost in tokens.

    Without this the only cost signal is the OpenRouter bill at the end of the
    month, by which point there is nothing to attribute it to. Cost here is
    dominated by the PROMPT, not the answer — the schema blob is sent on every
    turn and grows with the catalog — so the input/output split is the number
    that tells you whether to trim the catalog, tighten retrieval or change
    model. Logged rather than metered because the useful unit is one question.

    Never raises: an accounting miss must not fail a user's query.
    """
    try:
        u = getattr(response, "usage", None)
        if not u:
            return
        prompt = getattr(u, "prompt_tokens", None)
        completion = getattr(u, "completion_tokens", None)
        cached = getattr(u, "prompt_tokens_details", None)
        cached = getattr(cached, "cached_tokens", None) if cached else None
        _logger.info(
            "llm usage model=%s prompt_tokens=%s completion_tokens=%s "
            "cached_tokens=%s tables_in_prompt=%s",
            _config.llm.model, prompt, completion, cached, table_count,
        )
    except Exception:  # noqa: BLE001
        pass


def _log_llm_input(provider: str, tables, history, messages) -> None:
    """Debug instrumentation — logs exactly what the LLM receives so we can
    diagnose wrong-table picks. The summary (table names + history turn
    count) logs at INFO so it shows in normal dev logs; the full rendered
    message list logs at DEBUG. TEMPORARY — remove once table-selection is
    stable."""
    table_names = [getattr(t, "name", "?") for t in tables]
    _logger.info("LLM-INPUT [%s] %d tables sent: %s", provider, len(table_names), table_names)
    hist_preview = [
        {"role": m.role, "content": (m.content[:160] + "…") if len(m.content) > 160 else m.content}
        for m in history
    ]
    _logger.info("LLM-INPUT [%s] %d history turns: %s", provider, len(history), hist_preview)
    if _logger.isEnabledFor(logging.DEBUG):
        _logger.debug("LLM-INPUT [%s] full messages: %s", provider, messages)


# -----------------------------------------------------------------------------
# Result types
# -----------------------------------------------------------------------------
class VizSpec(BaseModel):
    type: str = "table"
    x: Optional[str] = None
    y: Optional[str] = None
    series: Optional[str] = None
    title: Optional[str] = None
    extra: dict = Field(default_factory=dict)

    user_requested: bool = False
    applies: bool = True
    applies_reason: Optional[str] = None


class SqlPlan(BaseModel):
    """LLM-emitted SQL plan. The backend validates the SQL against the
    allow-listed catalog before running it."""

    kind: Literal["sql_plan"] = "sql_plan"
    sql: str
    viz: VizSpec = Field(default_factory=VizSpec)
    assumptions: List[str] = Field(default_factory=list)


class ClarifyOption(BaseModel):
    id: str
    label: str
    description: Optional[str] = None


class Clarification(BaseModel):
    kind: Literal["clarification"] = "clarification"
    question: str
    context: str = ""
    options: List[ClarifyOption] = Field(default_factory=list)
    free_text_allowed: bool = True


class ExpertRedirect(BaseModel):
    """The LLM has detected that the user's question needs statistical
    analysis that the simple Intent shape (aggregates only) can't express.
    The frontend renders a card with a 'Run in Expert Lab' button that
    deep-links into the Expert Lab page with the suggested function +
    parameters pre-filled."""

    kind: Literal["expert_redirect"] = "expert_redirect"
    function: str
    suggested_params: dict = Field(default_factory=dict)
    explanation: str = ""


class DirectAnswer(BaseModel):
    """The LLM is answering a meta-question (about the schema, available
    tables, what the system can do, what enum values a column takes, etc.)
    directly from information already in its prompt. No SQL, no executor,
    no cost gate."""

    kind: Literal["direct_answer"] = "direct_answer"
    answer: str
    source: Literal["catalog", "system_info", "other"] = "catalog"


PlanResult = Union[SqlPlan, Clarification, ExpertRedirect, DirectAnswer]


class LlmDisabledError(RuntimeError):
    """Raised when an AI code path is reached while the LLM is switched off
    via `INSIGHT_LLM__ENABLED=false`. Controllers translate this into a 503
    so the caller gets an explicit, actionable answer rather than a stub."""


class ConvMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str


# -----------------------------------------------------------------------------
# Tool-schema translation helpers
# -----------------------------------------------------------------------------
def _anthropic_to_openai_tool(t: dict) -> dict:
    """Anthropic's tool format → OpenAI's `tools[]` shape."""
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t["input_schema"],
        },
    }


_OPENAI_TOOLS = [
    _anthropic_to_openai_tool(PROPOSE_SQL_TOOL_SCHEMA),
    _anthropic_to_openai_tool(CLARIFY_TOOL_SCHEMA),
    _anthropic_to_openai_tool(EXPERT_REDIRECT_TOOL_SCHEMA),
    _anthropic_to_openai_tool(DIRECT_ANSWER_TOOL_SCHEMA),
]

# -----------------------------------------------------------------------------
# Service
# -----------------------------------------------------------------------------
class LlmService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._client = None
        self._enabled = bool(_config.llm.enabled)
        self._init_client()

    @property
    def enabled(self) -> bool:
        """False when the AI features are switched off for this deployment
        (`INSIGHT_LLM__ENABLED=false`). No outbound LLM call is ever made."""
        return self._enabled

    def _init_client(self) -> None:
        if not self._enabled:
            _logger.info(
                "LLM disabled by configuration (INSIGHT_LLM__ENABLED=false); "
                "no client constructed and no outbound AI calls will be made."
            )
            return
        try:
            from openai import AsyncOpenAI
            api_key = _config.llm.api_key or os.environ.get("OPENROUTER_API_KEY", "")
            base_url = _config.llm.base_url
            if not api_key:
                _logger.warning(
                    "OpenRouter API key not set (INSIGHT_LLM__API_KEY / "
                    "OPENROUTER_API_KEY); LLM calls fall back to a deterministic stub"
                )
                return
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers=_config.llm.extra_headers or None,
            )
            _logger.info("LLM client: OpenRouter @ %s model=%s", base_url, _config.llm.model)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("OpenRouter client init failed: %s", exc)

    # ---------------------- public API ---------------------------------------

    async def plan(
        self,
        sanitized_question: str,
        tables: List[TableSpec],
        history: Optional[List[ConvMessage]] = None,
    ) -> PlanResult:
        if not self._enabled:
            # Hard stop — never reach a provider when AI is switched off.
            raise LlmDisabledError(
                "AI features are disabled for this deployment "
                "(INSIGHT_LLM__ENABLED=false)."
            )
        if self._client is None:
            return self._fallback_plan(tables)
        return await self._plan_openai(sanitized_question, tables, history or [])

    # ---------------------- OpenRouter (OpenAI-compatible) path --------------

    async def _plan_openai(
        self,
        sanitized_question: str,
        tables: List[TableSpec],
        history: List[ConvMessage],
    ) -> PlanResult:
        messages = self._openai_messages(sanitized_question, tables, history)
        _log_llm_input("openrouter", tables, history, messages)
        response = await self._client.chat.completions.create(
            model=_config.llm.model,
            max_tokens=_config.llm.max_tokens,
            tools=_OPENAI_TOOLS,
            tool_choice="auto",
            messages=messages,
        )

        _log_llm_usage(response, len(tables))

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception as exc:  # noqa: BLE001
                _logger.warning("LLM returned non-JSON tool args (%s): %s", exc, tc.function.arguments)
                continue
            if name == "request_clarification":
                return Clarification.model_validate(args)
            if name == "propose_sql":
                return SqlPlan.model_validate(args)
            if name == "propose_expert_redirect":
                return ExpertRedirect.model_validate(args)
            if name == "propose_direct_answer":
                return DirectAnswer.model_validate(args)

        # No tool call — surface a generic clarification rather than guess.
        _logger.warning(
            "OpenRouter LLM did not call a tool; raw content: %r",
            getattr(msg, "content", None),
        )
        return Clarification(
            question="Could you rephrase or be more specific about which data you're after?",
            context="model_did_not_select_tool",
            options=[],
        )

    # ---------------------- message builder ---------------------------------

    def _openai_messages(
        self,
        sanitized_question: str,
        tables: List[TableSpec],
        history: List[ConvMessage],
    ) -> list[dict]:
        schema_blob = self._schema_blob(tables)
        msgs: list[dict] = [
            {"role": "system", "content": NL_TO_SQL_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Available tables:\n{schema_blob}\n\n"
                    f"User question (PII-sanitized):\n{self._apply_thinking_directive(sanitized_question)}"
                ),
            },
        ]
        for m in history:
            msgs.append({"role": m.role, "content": m.content})
        msgs.append({
            "role": "user",
            "content": (
                "Call exactly one tool: `propose_sql` if you can write a "
                "safe SELECT, `propose_direct_answer` for a meta-question, "
                "`propose_expert_redirect` for statistical analysis, "
                "or `request_clarification`."
            ),
        })
        return msgs

    @staticmethod
    def _apply_thinking_directive(text: str) -> str:
        """Qwen3 honours `/think` and `/no_think` prefixes to toggle hybrid
        thinking mode. Other models treat the prefix as plain text and ignore
        it, so this is safe to apply unconditionally."""
        prefix = "/think " if _config.llm.enable_thinking else "/no_think "
        return prefix + text

    @staticmethod
    def _schema_blob(tables: List[TableSpec]) -> str:
        """Render the schema for the LLM prompt with enum values, role tags,
        and descriptions inline. Three things the model relies on heavily:
          * **enum values** — stripping these regressed the LLM into
            hallucinating filter values like `gender = 'FEMALE'` when the
            enum was `{M, F, O}`.
          * **role tag** ([REGISTER] / [PROGRAMME_REGISTER] / [TABLE]) —
            without this the LLM picks satellite tables for entity-level
            counts (e.g. counting disability records instead of individuals).
          * **description** — both the TABLE's (what one row means, and which
            register a satellite belongs to) and each COLUMN's, which is what
            distinguishes geo_1 from geo_3 or one date from another.
        Registers are listed first so the model sees them before any
        satellites — gives even weak retrievers a fair chance of picking
        the right one."""
        # Stable role-then-name sort: registers, then programme_registers,
        # then tables. Within each bucket, alphabetical.
        role_order = {"register": 0, "programme_register": 1, "table": 2}
        sorted_tables = sorted(
            tables,
            key=lambda t: (role_order.get(t.role or "table", 3), t.name),
        )
        rows = []
        for t in sorted_tables:
            cols: list[str] = []
            for c in t.safe_columns():
                bits = [f"{c.name}:{c.type}"]
                if c.enum:
                    bits.append(f"enum={{{','.join(c.enum)}}}")
                if c.unit:
                    bits.append(f"unit={c.unit}")
                # The column's own description. It was carried all the way from
                # the catalog into ColumnSpec and then dropped here, so the model
                # saw a bare name and had to guess what it meant. That is how
                # "farmers in each region" became a GROUP BY geo_1 — the country
                # column — returning a single row that answered nothing.
                if c.description:
                    bits.append(f"-- {c.description}")
                cols.append(" ".join(bits))
            role_tag = f"[{t.role.upper()}] " if t.role else ""
            line = f"- {role_tag}{t.name} ({'; '.join(cols)})"
            if t.description:
                line += f"  -- {t.description}"
            rows.append(line)
        return "\n".join(rows)

    @staticmethod
    def _fallback_plan(tables: List[TableSpec]) -> SqlPlan:
        """When the LLM client isn't configured, return a trivial COUNT against
        the first allow-listed table so the rest of the pipeline keeps working
        in dev / CI without an API key."""
        entity = tables[0].name if tables else "v_individuals_safe"
        return SqlPlan(
            sql=f"SELECT COUNT(*) AS total FROM {entity}",
            viz=VizSpec(type="metric_card", title="Row count"),
            assumptions=["LLM unavailable — deterministic fallback SQL"],
        )
