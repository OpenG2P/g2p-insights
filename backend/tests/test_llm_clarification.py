"""
Test the LLM service's tool-dispatch contract with a mocked Anthropic client.

The point is to lock in the routing:
  * `request_clarification` → returns `Clarification`.
  * `propose_sql`           → returns `SqlPlan`.
  * No tool call            → returns a generic `Clarification` rather than
                              guessing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openg2p_insight.services.llm_service import (
    Clarification,
    LlmService,
    SqlPlan,
)
from openg2p_insight.services.schema_service import ColumnSpec, TableSpec


class _Block:
    def __init__(self, type_: str, name: str | None = None, input_: dict | None = None):
        self.type = type_
        self.name = name
        self.input = input_ or {}


class _StubClient:
    """Async Anthropic-shaped stub."""

    def __init__(self, response_blocks: list[_Block]):
        self.response_blocks = response_blocks
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **_):
        return SimpleNamespace(content=self.response_blocks)


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
    svc._provider = "anthropic"
    return svc


@pytest.mark.asyncio
async def test_clarification_path():
    client = _StubClient(
        [
            _Block(
                "tool_use",
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
            _Block(
                "tool_use",
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
    client = _StubClient([_Block("text")])
    svc = _svc_with_client(client)
    result = await svc.plan("Something vague", [_table()], history=[])
    assert isinstance(result, Clarification)
    assert "model_did_not_select_tool" in (result.context or "")
