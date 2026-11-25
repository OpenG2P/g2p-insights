"""
Test the LLM service's tool-dispatch contract with a mocked OpenRouter client.

Insight talks to OpenRouter via the OpenAI-compatible chat-completions API, so
the stub mimics that response shape (`choices[0].message.tool_calls`).

The point is to lock in the routing:
  * `request_clarification` → returns `Clarification`.
  * `propose_sql`           → returns `SqlPlan`.
  * No tool call            → returns a generic `Clarification` rather than
                              guessing.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openg2p_insight.services.llm_service import (
    Clarification,
    LlmService,
    SqlPlan,
)
from openg2p_insight.services.schema_service import ColumnSpec, TableSpec


def _tool_call(name: str, args: dict) -> SimpleNamespace:
    # Mirrors openai's response: tc.function.name / tc.function.arguments (JSON string)
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class _StubClient:
    """Async OpenAI-shaped stub: client.chat.completions.create(...)."""

    def __init__(self, tool_calls: list[SimpleNamespace], content: str | None = None):
        message = SimpleNamespace(tool_calls=tool_calls, content=content)
        choice = SimpleNamespace(message=message)
        self._response = SimpleNamespace(choices=[choice])
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(self, **_):
        return self._response


def _table() -> TableSpec:
    return TableSpec(
        name="mv_individuals_summary",
        description="Pre-joined individuals",
        columns=[
            ColumnSpec(name="region_code", type="string"),
            ColumnSpec(name="gender", type="enum"),
            ColumnSpec(name="monthly_income", type="float"),
            ColumnSpec(name="household_monthly_income", type="float"),
        ],
    )


def _svc_with_client(client) -> LlmService:
    svc = LlmService.__new__(LlmService)
    svc._client = client
    svc._enabled = True
    return svc


@pytest.mark.asyncio
async def test_clarification_path():
    client = _StubClient(
        [
            _tool_call(
                "request_clarification",
                {
                    "question": "By income do you mean individual or household?",
                    "context": "metric_definition",
                    "options": [
                        {"id": "ind", "label": "Individual monthly income"},
                        {"id": "hh", "label": "Household monthly income"},
                    ],
                    "free_text_allowed": False,
                },
            )
        ]
    )
    svc = _svc_with_client(client)
    result = await svc.plan("Show me women with low income", [_table()], history=[])
    assert isinstance(result, Clarification)
    assert "income" in result.question.lower()
    assert {o.id for o in result.options} == {"ind", "hh"}


@pytest.mark.asyncio
async def test_sql_plan_path():
    client = _StubClient(
        [
            _tool_call(
                "propose_sql",
                {
                    "sql": (
                        "SELECT region_code, COUNT(*) AS total "
                        "FROM mv_individuals_summary "
                        "WHERE gender = 'F' GROUP BY region_code"
                    ),
                    "viz": {"type": "bar", "x": "region_code", "y": "total"},
                    "assumptions": ["Used individual gender column"],
                },
            )
        ]
    )
    svc = _svc_with_client(client)
    result = await svc.plan("Count women by region", [_table()], history=[])
    assert isinstance(result, SqlPlan)
    assert "mv_individuals_summary" in result.sql
    assert result.viz.type == "bar"


@pytest.mark.asyncio
async def test_no_tool_call_falls_back_to_clarification():
    # No tool_calls at all — the model just returned prose.
    client = _StubClient([], content="I'm not sure what you mean.")
    svc = _svc_with_client(client)
    result = await svc.plan("Something vague", [_table()], history=[])
    assert isinstance(result, Clarification)
    assert "model_did_not_select_tool" in (result.context or "")
