"""
Claude orchestrator — tool-use loop with clarification support.

Takes a sanitized question (plus optional conversation history) and the
relevant catalog slice, then asks Claude to call ONE of two tools:

  * `request_clarification` — Claude needs more info from the user.
  * `propose_intent`        — Claude is confident and emits an Intent JSON.

The service returns a `PlanResult` union — the controller picks the path
based on the `kind` field.
"""

from __future__ import annotations

import logging
import os
from typing import List, Literal, Optional, Union

from openg2p_fastapi_common.service import BaseService
from pydantic import BaseModel, Field

from ..config import Settings
from ..prompts.nl_to_intent import (
    CLARIFY_TOOL_SCHEMA,
    INTENT_TOOL_SCHEMA,
    NL_TO_INTENT_SYSTEM,
)
from .schema_service import TableSpec

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


# -----------------------------------------------------------------------------
# Result types
# -----------------------------------------------------------------------------
class Filter(BaseModel):
    col: str
    op: str
    val: object
    note: Optional[str] = None


class VizSpec(BaseModel):
    type: str = "table"
    x: Optional[str] = None
    y: Optional[str] = None
    series: Optional[str] = None
    title: Optional[str] = None
    extra: dict = Field(default_factory=dict)


class Intent(BaseModel):
    kind: Literal["intent"] = "intent"
    intent: str = "aggregate"           # "aggregate" | "distribution" | "raw"
    metric: str = "count"               # count | sum | avg | min | max
    metric_column: Optional[str] = None  # required for any metric != "count"
    entity: str
    filters: List[Filter] = Field(default_factory=list)
    group_by: List[str] = Field(default_factory=list)
    order_by: List[str] = Field(default_factory=list)
    limit: Optional[int] = None
    viz: VizSpec = VizSpec()
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


PlanResult = Union[Intent, Clarification]


# -----------------------------------------------------------------------------
# Conversation message — used to feed Claude prior turns
# -----------------------------------------------------------------------------
class ConvMessage(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str


# -----------------------------------------------------------------------------
# Service
# -----------------------------------------------------------------------------
class LlmService(BaseService):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self._client = None
        self._init_client()

    def _init_client(self) -> None:
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

    # ---------------------- public API ---------------------------------------

    async def plan(
        self,
        sanitized_question: str,
        tables: List[TableSpec],
        history: Optional[List[ConvMessage]] = None,
    ) -> PlanResult:
        """
        Returns EITHER an Intent (LLM is ready to commit) or a Clarification
        (LLM wants more info from the user).
        """
        if self._client is None:
            return self._fallback_intent(tables)

        messages = self._build_messages(sanitized_question, tables, history or [])

        # NOTE: `temperature` is deprecated for Opus 4.7 (and likely future
        # Claude 4.x models). Don't send it unconditionally. If you need a
        # specific temperature, set it via INSIGHT_LLM__TEMPERATURE and we'll
        # forward it on supported models — for now we just don't pass it.
        response = await self._client.messages.create(
            model=_config.llm.model,
            max_tokens=_config.llm.max_tokens,
            system=NL_TO_INTENT_SYSTEM,
            tools=[INTENT_TOOL_SCHEMA, CLARIFY_TOOL_SCHEMA],
            messages=messages,
        )

        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "request_clarification":
                return Clarification.model_validate(block.input)
            if block.name == "propose_intent":
                return Intent.model_validate(block.input)

        # Model didn't pick a tool — clarify generically rather than guess.
        return Clarification(
            question="Could you tell me which table or metric you have in mind?",
            context="model_did_not_select_tool",
            options=[],
        )

    async def plan_chart(
        self, sanitized_question: str, tables: List[TableSpec]
    ) -> PlanResult:
        """Same as `plan`, but the chart-authoring controller calls this. Today
        it shares the same tool surface; we can specialize the prompt later."""
        return await self.plan(sanitized_question, tables)

    # ---------------------- internals ----------------------------------------

    @staticmethod
    def _build_messages(
        sanitized_question: str,
        tables: List[TableSpec],
        history: List[ConvMessage],
    ) -> list[dict]:
        """Build the message list for the Anthropic API.

        The first user message carries the schema; subsequent turns are
        user / assistant pairs that capture clarifications already exchanged.
        """
        schema_blob = LlmService._schema_blob(tables)
        msgs: list[dict] = [
            {
                "role": "user",
                "content": (
                    f"Available tables:\n{schema_blob}\n\n"
                    f"User question (PII-sanitized):\n{sanitized_question}"
                ),
            }
        ]
        for m in history:
            msgs.append({"role": m.role, "content": m.content})
        msgs.append(
            {
                "role": "user",
                "content": (
                    "Call exactly one tool: `propose_intent` if you can commit, "
                    "otherwise `request_clarification`."
                ),
            }
        )
        return msgs

    @staticmethod
    def _schema_blob(tables: List[TableSpec]) -> str:
        """Render the schema for the LLM prompt.

        IMPORTANT: include enum values inline. Without them the model
        hallucinates filter values (e.g. emitting `gender = 'FEMALE'` when the
        underlying Postgres enum only accepts 'M'/'F'/'O'), which then fails
        at EXPLAIN time. Units and descriptions are included where present —
        all of these meaningfully reduce wrong-SQL rates.
        """
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
    def _fallback_intent(tables: List[TableSpec]) -> Intent:
        entity = tables[0].name if tables else "v_individuals_safe"
        return Intent(
            entity=entity,
            viz=VizSpec(type="table"),
            assumptions=["LLM unavailable — deterministic fallback intent"],
        )
