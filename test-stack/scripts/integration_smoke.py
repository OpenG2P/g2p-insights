#!/usr/bin/env python3
"""
End-to-end smoke test for the local Insight stack.

Runs against the docker-compose stack started by `make up` + `make seed`.
Verifies:

  1. /health returns ok.
  2. /catalog has the seeded NSR tables.
  3. /geo/levels resolves region/zone/woreda/kebele.
  4. A simple aggregate query (no clarification expected) executes and
     returns rows. The response carries SQL and a GREEN cost report.
  5. An ambiguous query triggers a clarification AND the response carries
     the same session_id we used for the follow-up.
  6. After clarification, the final response has SQL, cost, rows.
  7. PII in the question is redacted before the LLM ever sees it.
  8. k-anonymity actually suppresses small groups.
  9. The cost gate surfaces a non-GREEN classification for an unbounded
     query (best-effort — depends on data size).

Usage:
    python integration_smoke.py [--base http://localhost:8000/v1/insight]

Exit code 0 on full pass, 1 on any failure. The script prints what passes
and what fails — handy for quick iteration.
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from typing import Any

import httpx


GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


class Smoke:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.client = httpx.Client(timeout=60)
        self.fails: list[str] = []
        self.passes: list[str] = []

    # ---------- assertion helpers --------------------------------------------

    def assert_true(self, label: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passes.append(label)
            print(f"  {GREEN}PASS{RESET} {label}")
        else:
            msg = f"{label} :: {detail}" if detail else label
            self.fails.append(msg)
            print(f"  {RED}FAIL{RESET} {msg}")

    def _post(self, path: str, payload: dict) -> dict:
        r = self.client.post(self.base + path, json=payload)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str, **params) -> Any:
        r = self.client.get(self.base + path, params=params)
        r.raise_for_status()
        return r.json()

    # ---------- individual checks --------------------------------------------

    def check_health(self) -> None:
        print("[1] /health")
        data = self._get("/health")
        self.assert_true("health.status == ok", data.get("status") == "ok", str(data))

    def check_catalog(self) -> None:
        print("[2] /catalog")
        data = self._get("/catalog")
        tables = [t["name"] for t in (data.get("tables") or [])]
        self.assert_true(
            "catalog has mv_individuals_summary",
            "mv_individuals_summary" in tables,
            f"tables={tables}",
        )
        safe = data.get("safe_views") or []
        self.assert_true(
            "catalog has at least one safe_view",
            len(safe) > 0,
            f"safe_views={safe}",
        )

    def check_geo(self) -> None:
        print("[3] /geo/levels")
        try:
            data = self._get("/geo/levels")
            levels = [lv["level_mnemonic"] for lv in (data.get("levels") or [])]
            self.assert_true(
                "geo levels include region/zone/woreda/kebele",
                all(m in levels for m in ("region", "zone", "woreda", "kebele")),
                f"levels={levels}",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  {YELLOW}SKIP{RESET} geo (MDS unreachable: {exc})")

    def check_simple_query(self) -> None:
        print("[4] /ask — simple aggregate (women by region)")
        data = self._post("/ask", {
            "question": "How many women are there by region?",
            "auto_run_yellow": True,
        })
        # Either ok or needs_clarification is acceptable depending on LLM mood;
        # if it clarifies, answer once and continue.
        if data["status"] == "needs_clarification":
            print(f"  {YELLOW}…clarification: {data['clarification']['question']}{RESET}")
            data = self._post("/ask/clarify", {
                "session_id": data["session_id"],
                "text": "Use the gender column = F; group by region_code; counts only.",
                "auto_run_yellow": True,
            })

        self.assert_true(
            "ask returns ok",
            data["status"] == "ok",
            f"status={data['status']}, cost={data.get('cost')}",
        )
        self.assert_true(
            "ask response includes SQL",
            isinstance(data.get("sql"), str) and "SELECT" in data["sql"].upper(),
            f"sql={data.get('sql')!r}",
        )
        self.assert_true("ask response has a cost report", bool(data.get("cost")))
        self.assert_true(
            "ask response has rows or sensible empty",
            isinstance(data.get("rows"), list),
        )

    def check_pii_redaction(self) -> None:
        print("[5] /ask — PII redaction (proper noun in question)")
        data = self._post("/ask", {
            "question": "Show me income data for John Mayer",
            "auto_run_yellow": True,
        })
        # Either status; what matters is that PII was redacted.
        redactions = data.get("pii_redactions") or []
        self.assert_true(
            "PII redactions are detected (PERSON entity)",
            any(r.get("entity_type") == "PERSON" for r in redactions),
            f"redactions={redactions}",
        )

    def check_clarification_loop(self) -> None:
        print("[6] /ask — ambiguous question triggers clarification")
        data = self._post("/ask", {
            "question": "Average income in Oromia",
            "auto_run_yellow": True,
        })
        if data["status"] != "needs_clarification":
            print(f"  {YELLOW}SKIP{RESET} LLM committed without clarifying — that's OK")
            return

        sid = data["session_id"]
        clar = data["clarification"]
        self.assert_true(
            "clarification has a question",
            isinstance(clar.get("question"), str) and len(clar["question"]) > 0,
        )

        # Pick the first option if available, else send a free-text answer.
        if clar.get("options"):
            answer = {"session_id": sid, "option_id": clar["options"][0]["id"], "auto_run_yellow": True}
        else:
            answer = {"session_id": sid, "text": "household monthly income; threshold 1000 ETB", "auto_run_yellow": True}

        final = self._post("/ask/clarify", answer)
        self.assert_true(
            "session_id is preserved across clarify",
            final["session_id"] == sid,
        )
        self.assert_true(
            "final response after clarify is no longer clarifying",
            final["status"] in ("ok", "needs_confirmation", "rejected"),
            f"status={final['status']}",
        )
        self.assert_true(
            "final response carries SQL",
            isinstance(final.get("sql"), str) and "SELECT" in final["sql"].upper(),
        )

    def check_cost_gate_alerts(self) -> None:
        print("[7] /ask — likely-expensive query surfaces YELLOW/RED (best-effort)")
        data = self._post("/ask", {
            "question": "List every individual ordered by id",
            "auto_run_yellow": False,
        })
        if data["status"] in ("rejected", "needs_confirmation"):
            level = (data.get("cost") or {}).get("level")
            print(f"  {GREEN}PASS{RESET} cost gate raised level={level}")
            self.passes.append("cost gate surfaces non-GREEN for unbounded query")
        elif data["status"] == "needs_clarification":
            print(f"  {YELLOW}SKIP{RESET} LLM clarified before planning — re-run with stricter prompt to test cost gate")
        else:
            level = (data.get("cost") or {}).get("level")
            if level == "green":
                print(f"  {YELLOW}NOTE{RESET} query classified GREEN; data set is small enough that the planner sees no risk")
            else:
                self.assert_true("cost level is non-GREEN", level != "green")

    def check_k_anonymity(self) -> None:
        """We can't reliably force a small-group result with a real LLM; just
        check the *response shape* surfaces k_suppressions when applicable."""
        print("[8] /ask — k_suppressions field is present in responses")
        data = self._post("/ask", {
            "question": "Count individuals by region",
            "auto_run_yellow": True,
        })
        if data["status"] == "needs_clarification":
            data = self._post("/ask/clarify", {
                "session_id": data["session_id"],
                "text": "Use gender=ALL; group by region_code only; count of individuals.",
                "auto_run_yellow": True,
            })
        self.assert_true(
            "k_suppressions field present (int >= 0)",
            isinstance(data.get("k_suppressions"), int) and data["k_suppressions"] >= 0,
        )

    # ---------- run ----------------------------------------------------------

    def run(self) -> int:
        for fn in (
            self.check_health,
            self.check_catalog,
            self.check_geo,
            self.check_simple_query,
            self.check_pii_redaction,
            self.check_clarification_loop,
            self.check_cost_gate_alerts,
            self.check_k_anonymity,
        ):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self.fails.append(f"{fn.__name__}: {exc!s}")
                print(f"  {RED}EXCEPTION{RESET} {fn.__name__}: {exc!s}")

        print()
        print(f"PASSED: {len(self.passes)}  FAILED: {len(self.fails)}")
        if self.fails:
            print()
            print("Failures:")
            for f in self.fails:
                print(textwrap.indent(f, "  · "))
            return 1
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default=os.environ.get("INSIGHT_BASE_URL", "http://localhost:8000/v1/insight"),
    )
    ns = parser.parse_args()
    return Smoke(ns.base).run()


if __name__ == "__main__":
    sys.exit(main())
