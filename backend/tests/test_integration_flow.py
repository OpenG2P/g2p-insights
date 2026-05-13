"""
Integration test for the multi-turn /ask flow.

Skipped unless the test stack is running and INSIGHT_BASE_URL is reachable.
The test stack itself is documented in /repos/g2p-insight/test-stack/README.md.

Run manually:

    cd test-stack && make up && make seed
    INSIGHT_BASE_URL=http://localhost:8000/v1/insight \\
        pytest -m integration -s backend/tests/test_integration_flow.py
"""

from __future__ import annotations

import os

import httpx
import pytest

INSIGHT_BASE_URL = os.environ.get("INSIGHT_BASE_URL", "http://localhost:8000/v1/insight")


def _reachable() -> bool:
    try:
        r = httpx.get(f"{INSIGHT_BASE_URL}/health", timeout=2)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"Insight backend not reachable at {INSIGHT_BASE_URL}"
)


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(base_url=INSIGHT_BASE_URL, timeout=60) as c:
        yield c


def test_health(client: httpx.Client) -> None:
    r = client.get("/health")
    r.raise_for_status()
    assert r.json()["status"] == "ok"


def test_catalog_has_safe_views(client: httpx.Client) -> None:
    r = client.get("/catalog")
    r.raise_for_status()
    data = r.json()
    assert data.get("safe_views"), "catalog has no safe_views — the test stack catalog must be mounted"


def test_simple_aggregate_returns_sql_and_cost(client: httpx.Client) -> None:
    r = client.post("/ask", json={
        "question": "How many individuals are there by region?",
        "auto_run_yellow": True,
    })
    r.raise_for_status()
    data = r.json()

    # Walk through any clarifications the LLM raises (real LLM behaviour
    # is non-deterministic; we just answer once and move on).
    while data["status"] == "needs_clarification":
        sid = data["session_id"]
        r = client.post("/ask/clarify", json={
            "session_id": sid,
            "text": "group by region_code, simple count of individuals",
            "auto_run_yellow": True,
        })
        r.raise_for_status()
        data = r.json()

    assert data["status"] in ("ok", "needs_confirmation"), data
    assert data["sql"], "SQL must be present in the response"
    assert "SELECT" in data["sql"].upper()
    assert data["cost"]["level"] in ("green", "yellow", "red")


def test_pii_redaction_runs_before_llm(client: httpx.Client) -> None:
    r = client.post("/ask", json={
        "question": "Show me income data for John Mayer last year",
        "auto_run_yellow": True,
    })
    r.raise_for_status()
    data = r.json()
    person_redactions = [x for x in data["pii_redactions"] if x["entity_type"] == "PERSON"]
    assert person_redactions, "expected at least one PERSON redaction"
    # The redacted placeholder must not leak the real name through the SQL.
    if data.get("sql"):
        assert "John Mayer" not in data["sql"], "real name must not appear in generated SQL"


def test_session_state_is_preserved_across_clarify(client: httpx.Client) -> None:
    r = client.post("/ask", json={
        "question": "average income in Oromia",
        "auto_run_yellow": True,
    })
    r.raise_for_status()
    first = r.json()
    sid = first["session_id"]

    # If we got an immediate intent, fine — exit early.
    if first["status"] != "needs_clarification":
        pytest.skip("LLM committed without clarifying; behaviour acceptable")

    clar = first["clarification"]
    payload = {"session_id": sid, "auto_run_yellow": True}
    if clar.get("options"):
        payload["option_id"] = clar["options"][0]["id"]
    else:
        payload["text"] = "household monthly income; threshold 1000 ETB"

    r = client.post("/ask/clarify", json=payload)
    r.raise_for_status()
    final = r.json()
    assert final["session_id"] == sid
    assert final["status"] in ("ok", "needs_confirmation", "rejected", "needs_clarification")
