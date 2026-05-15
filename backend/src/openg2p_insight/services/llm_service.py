"""
Claude / OpenAI-compatible orchestrator with tool-use + clarification support.

Two provider paths share the same Pydantic result types (`SqlPlan` /
`Clarification` / `DirectAnswer` / `ExpertRedirect`):

  * `anthropic`         — native Claude SDK
  * `openai_compatible` — any provider with an OpenAI-compatible
                          chat-completions API: OpenRouter, Groq, Together,
                          self-hosted vLLM, etc.

Switching providers is a config change (`INSIGHT_LLM__PROVIDER`,
`INSIGHT_LLM__BASE_URL`, `INSIGHT_LLM__MODEL`, `INSIGHT_LLM__API_KEY`).
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

_ANTHROPIC_TOOLS = [
    PROPOSE_SQL_TOOL_SCHEMA,
    CLARIFY_TOOL_SCHEMA,
    EXPERT_REDIRECT_TOOL_SCHEMA,
    DIRECT_ANSWER_TOOL_SCHEMA,
]


# -----------------------------------------------------------------------------
# Service
# -----------------------------------------------------------------------------
class LlmService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._client = None
        self._provider = _config.llm.provider
        self._init_client()

    def _init_client(self) -> None:
        if self._provider == "openai_compatible":
            self._init_openai_client()
        else:
            self._init_anthropic_client()

    def _init_anthropic_client(self) -> None:
        try:
            from anthropic import AsyncAnthropic
            api_key = _config.llm.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                _logger.warning(
                    "ANTHROPIC_API_KEY not set; LLM calls will fall back to a deterministic stub"
                )
                return
            self._client = AsyncAnthropic(api_key=api_key)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Anthropic client init failed: %s", exc)

    def _init_openai_client(self) -> None:
        try:
            from openai import AsyncOpenAI
            api_key = _config.llm.api_key or os.environ.get("OPENAI_API_KEY", "")
            base_url = _config.llm.base_url
            if not api_key:
                _logger.warning(
                    "LLM provider is openai_compatible but INSIGHT_LLM__API_KEY is not set; "
                    "falling back to deterministic stub"
                )
                return
            if not base_url:
                _logger.warning(
                    "openai_compatible provider requires INSIGHT_LLM__BASE_URL (e.g. "
                    "https://openrouter.ai/api/v1); falling back to stub"
                )
                return
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                default_headers=_config.llm.extra_headers or None,
            )
            _logger.info("LLM client: openai_compatible @ %s model=%s", base_url, _config.llm.model)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("OpenAI-compatible client init failed: %s", exc)

    # ---------------------- public API ---------------------------------------

    async def plan(
        self,
        sanitized_question: str,
        tables: List[TableSpec],
        history: Optional[List[ConvMessage]] = None,
    ) -> PlanResult:
        if self._client is None:
            return self._fallback_plan(tables)

        if self._provider == "openai_compatible":
            return await self._plan_openai(sanitized_question, tables, history or [])
        return await self._plan_anthropic(sanitized_question, tables, history or [])

    # ---------------------- Anthropic path ----------------------------------

    async def _plan_anthropic(
        self,
        sanitized_question: str,
        tables: List[TableSpec],
        history: List[ConvMessage],
    ) -> PlanResult:
        messages = self._anthropic_messages(sanitized_question, tables, history)
        response = await self._client.messages.create(
            model=_config.llm.model,
            max_tokens=_config.llm.max_tokens,
            system=NL_TO_SQL_SYSTEM,
            tools=_ANTHROPIC_TOOLS,
            messages=messages,
        )
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "request_clarification":
                return Clarification.model_validate(block.input)
            if block.name == "propose_sql":
                return SqlPlan.model_validate(block.input)
            if block.name == "propose_expert_redirect":
                return ExpertRedirect.model_validate(block.input)
            if block.name == "propose_direct_answer":
                return DirectAnswer.model_validate(block.input)
        return Clarification(
            question="Could you rephrase or be more specific about which data you're after?",
            context="model_did_not_select_tool",
            options=[],
        )

    # ---------------------- OpenAI-compatible path --------------------------

    async def _plan_openai(
        self,
        sanitized_question: str,
        tables: List[TableSpec],
        history: List[ConvMessage],
    ) -> PlanResult:
        messages = self._openai_messages(sanitized_question, tables, history)
        response = await self._client.chat.completions.create(
            model=_config.llm.model,
            max_tokens=_config.llm.max_tokens,
            tools=_OPENAI_TOOLS,
            tool_choice="auto",
            messages=messages,
        )

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
            "openai_compatible LLM did not call a tool; raw content: %r",
            getattr(msg, "content", None),
        )
        return Clarification(
            question="Could you rephrase or be more specific about which data you're after?",
            context="model_did_not_select_tool",
            options=[],
        )

    # ---------------------- message builders --------------------------------

    def _anthropic_messages(
        self,
        sanitized_question: str,
        tables: List[TableSpec],
        history: List[ConvMessage],
    ) -> list[dict]:
        schema_blob = self._schema_blob(tables)
        user_first = (
            f"Available tables:\n{schema_blob}\n\n"
            f"User question (PII-sanitized):\n{self._apply_thinking_directive(sanitized_question)}"
        )
        msgs: list[dict] = [{"role": "user", "content": user_first}]
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
        """Render the schema for the LLM prompt with enum values + units inline.
        Stripping these regressed the LLM into hallucinating filter values like
        `gender = 'FEMALE'` when the enum was `{M, F, O}`."""
        rows = []
        for t in tables:
            cols: list[str] = []
            for c in t.safe_columns():
                bits = [f"{c.name}:{c.type}"]
                if c.enum:
                    bits.append(f"enum={{{','.join(c.enum)}}}")
                if c.unit:
                    bits.append(f"unit={c.unit}")
                cols.append(" ".join(bits))
            line = f"- {t.name} ({'; '.join(cols)})"
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
